from __future__ import annotations

from datetime import timedelta

import asyncpg
import pytest
from services.control_api.app.database import MemoryStore
from services.governance.subject_archive import PostgresSubjectArchive
from services.governance.subject_deletion import SubjectDeletionLedger, SubjectDeletionService
from services.guardian.child_memory import ArchiveFact, DialogueSummary
from services.identity.tests.test_postgres_revoke_classify import setup
from services.identity.tests.test_redact_bound_subject import NOW
from services.memory_scope.child_layers import ChildLayerStore
from services.memory_scope.subject_erasure import PostgresSubjectMemoryScope

pytestmark = pytest.mark.asyncio


async def seed_layers(database, child):
    layers = ChildLayerStore(api_dsn=database.role_dsn('memoria_memory_api'), maintenance_dsn=database.role_dsn('memoria_memory_maintenance'))
    await layers.initialize()
    await layers.write_summary(DialogueSummary(subject_id=child, text='孩子聊了小狗和画画', source_event_id='evt-1', session_id='session-1'),
        resource_owner_id='parent', actor_id='parent', consent_snapshot_id='fixture-memory')
    await layers.write_archive_fact(ArchiveFact(subject_id=child, field='pet_name', text='宠物叫豆豆'),
        resource_owner_id='parent', actor_id='parent', consent_snapshot_id='fixture-memory')
    return layers


@pytest.mark.parametrize('crash', ['before_commit', 'after_commit', 'after_ledger'])
async def test_release_crash_replays_without_fencing_still_active_subject(postgres_database, tmp_path, crash):
    database = postgres_database
    store, identity, child = await setup(database, tmp_path)
    layers = await seed_layers(database, child)
    memory = PostgresSubjectMemoryScope(database.role_dsn('memoria_memory_maintenance'))
    control = MemoryStore(dsn=database.role_dsn('memoria_control'), initialize_schema=False)
    ledger = SubjectDeletionLedger.beside(control, tmp_path / 'unused')
    ledger.initialize()
    archive = PostgresSubjectArchive(database.role_dsn('memoria_app'))
    fail = crash == 'after_ledger'

    async def terminate(subject_id):
        nonlocal fail
        if fail:
            fail = False
            raise RuntimeError('crash after ledger begin')
        return 0

    async def redact(subject_id, account_id):
        await identity.redact_bound_subject(person_id=subject_id, actor_person_id=account_id)

    def service():
        return SubjectDeletionService(ledger=ledger, archive=archive, object_store=None,
            memory_scope=memory, terminate_sessions=terminate, redact_identity=redact,
            pending_releases=store.pending_subject_releases, complete_release=store.complete_subject_release)

    try:
        admin = await asyncpg.connect(database.owner_dsn())
        try:
            assert await admin.fetchval('SELECT count(*) FROM memory_records WHERE subject_id=$1', child) == 2
            if crash == 'before_commit':
                tx = admin.transaction()
                await tx.start()
                await admin.execute("SELECT set_config('app.identity_actor','parent',true)")
                binding = await identity.get_active_manifest('device-1', actor_person_id='parent')
                await admin.execute('SELECT identity_revoke_binding($1,$2,$3)', binding.binding_id, 'parent', NOW + timedelta(seconds=1))
                await tx.rollback()
            else:
                await identity.revoke_binding(device_id='device-1', actor_person_id='parent', now=NOW + timedelta(seconds=1))
            if crash == 'after_ledger':
                from services.governance.subject_ports import SubjectScope
                with pytest.raises(RuntimeError, match='crash after ledger'):
                    await service().delete_subject(SubjectScope('parent', child), redact_identity=True)
                assert ledger.is_deleting(account_id='parent', subject_id=child)
            await service().retry_pending_deletions()
            expected = 2 if crash == 'before_commit' else 0
            assert await admin.fetchval('SELECT count(*) FROM memory_records WHERE subject_id=$1', child) == expected
            assert not ledger.is_deleting(account_id='parent', subject_id=child)
            assert await store.pending_subject_releases() == ()
            assert (await identity.get_person(child, actor_person_id='parent')).status == ('active' if crash == 'before_commit' else 'disabled')
            if crash != 'before_commit':
                assert ledger.completed() == (('parent', child, True),)
        finally:
            await admin.close()
    finally:
        await layers.close()
        await memory.close()
        control.close()
        await store.close()
