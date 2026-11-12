from __future__ import annotations

import uuid
from types import SimpleNamespace

import asyncpg
import pytest
from cryptography.fernet import Fernet
from services.archive.object_store import EncryptedLocalObjectStore
from services.control_api.app.database import MemoryStore
from services.control_api.app.database.chat_usage import delete_chat_usage
from services.control_api.app.governance_wiring import _IdentityAccountEraser
from services.governance.account_data import AccountDataGovernance
from services.governance.subject_archive import PostgresSubjectArchive
from services.governance.subject_deletion import SubjectDeletionLedger, SubjectDeletionService
from services.governance.tests.test_subject_release_postgres import seed_layers
from services.guardian.corpus import CorpusRetentionService
from services.guardian.postgres_store import PostgresGuardianStore
from services.identity.tests.test_postgres_revoke_classify import bind, setup
from services.identity.tests.test_redact_bound_subject import NOW
from services.memory_scope.subject_erasure import PostgresSubjectMemoryScope

pytestmark = pytest.mark.asyncio


class EmptyRepository:
    async def delete_account(self, account_id):
        return {}

    async def remaining_account_rows(self, account_id):
        return {}


class NoVoices:
    async def profiles(self, *, account_id):
        return ()


async def fixture(database, tmp_path):
    store, identity, child = await setup(database, tmp_path)
    layers = await seed_layers(database, child)
    memory = PostgresSubjectMemoryScope(database.role_dsn('memoria_memory_maintenance'))
    control = MemoryStore(dsn=database.role_dsn('memoria_control'), initialize_schema=False)
    control.register_account(user_id='parent', username='parent', username_normalized='parent', password_hash='fixture', now=NOW.isoformat())
    guardian = PostgresGuardianStore(database.role_dsn('memoria_guardian'),
        maintenance_dsn=database.role_dsn('memoria_guardian_maintenance'), initialize_schema=False)
    objects = EncryptedLocalObjectStore(root=tmp_path / 'objects', key=Fernet.generate_key().decode(), key_version='v1')
    corpus = CorpusRetentionService(guardian, objects)
    archive = PostgresSubjectArchive(database.role_dsn('memoria_app'))
    ledger = SubjectDeletionLedger.beside(control, tmp_path / 'unused')
    ledger.initialize()

    async def purge_usage(subject_id):
        with control.connection() as conn:
            return delete_chat_usage(conn, subject_id=subject_id)

    async def purge_corpus(subject_id):
        return await corpus.purge_minor(minor_user_id=subject_id)

    subject = SubjectDeletionService(ledger=ledger, archive=archive, object_store=objects,
        guardian=guardian, memory_scope=memory, purge_chat_usage=purge_usage, purge_corpus=purge_corpus)
    governance = AccountDataGovernance(memory_store=control, archive_repository=archive,
        speaker_repository=EmptyRepository(), evolution_repository=EmptyRepository(), voice_profiles=NoVoices(),
        guardian_repository=guardian, corpus_retention_service=corpus,
        identity_repository=_IdentityAccountEraser(identity))
    governance._subject_deletion = subject
    return SimpleNamespace(**locals())


async def seed_child_metadata(f):
    admin = await asyncpg.connect(f.database.owner_dsn())
    try:
        await admin.execute("INSERT INTO guardian_crisis_events(crisis_event_id,evidence_event_id,minor_user_id,occurred_at,script_version) VALUES($1,'crisis-event',$2,now(),'v1')", uuid.uuid4(), f.child)
        await admin.execute("INSERT INTO daily_chat_usage VALUES($1,$2,'2026-11-12',10,10,$3)", 'a' * 32, f.child, NOW.isoformat())
        f.reference = await f.objects.put(account_id=f.child, purpose='corpus', data=b'legacy-corpus', media_type='audio/wav')
        link, consent = uuid.uuid4(), uuid.uuid4()
        await admin.execute("INSERT INTO guardian_links VALUES($1,'legacy-grantor','legacy-minor','parent','active','manual_review',$2,now()+interval '1 day',now(),now(),NULL)", link, 'b' * 64)
        await admin.execute("INSERT INTO guardian_consents(consent_id,link_id,consent_kind,policy_version,granted_at,expires_at,evidence_event_id) VALUES($1,$2,'corpus_recording','v1',now(),now()+interval '1 day','legacy-corpus-consent')", consent, link)
        ref = f.reference
        await admin.execute("INSERT INTO guardian_corpus_samples VALUES($1,$2,$3,'legacy-corpus-event',$4,$5,$6,$7,$8,$9,now(),now()+interval '1 day',NULL)",
            uuid.uuid4(), f.child, consent, ref.object_key, ref.media_type, ref.byte_count, ref.content_sha256, ref.encryption_key_version, ref.backend)

    finally:
        await admin.close()


async def close(f):
    await f.layers.close()
    await f.memory.close()
    await f.guardian.close()
    f.control.close()
    await f.store.close()


@pytest.mark.parametrize('shared', [False, True])
async def test_account_deletion_erases_accountless_child_and_preserves_other_owner(postgres_database, tmp_path, shared):
    f = await fixture(postgres_database, tmp_path)
    try:
        if shared:
            await f.identity.register_person(person_id='parent-2', display_name='Other', timezone='Asia/Shanghai',
                subject_category='adult', age_band='adult', age_evidence_status='verified', age_evidence_id='fixture', now=NOW)
            await f.identity.attest_binding_relationship(source_person_id='parent-2', target_person_id=f.child,
                relation_type='guardian_of', actor_person_id='parent-2', now=NOW)
            await bind(f.identity, f.child, owner='parent-2')
        await seed_child_metadata(f)
        result = await f.governance.delete_account('parent')
        assert result['status'] == 'completed'
        admin = await asyncpg.connect(postgres_database.owner_dsn())
        try:
            for table, key, expected in [('memory_records','subject_id',2 if shared else 0),
                ('guardian_crisis_events','minor_user_id',1 if shared else 0), ('daily_chat_usage','subject_id',1 if shared else 0)]:
                assert await admin.fetchval(f'SELECT count(*) FROM {table} WHERE {key}=$1', f.child) == expected
            assert await admin.fetchval('SELECT count(*) FROM identity_persons WHERE person_id=$1', f.child) == int(shared)
            assert len(f.ledger.completed()) == int(not shared)
            assert await admin.fetchval('SELECT count(*) FROM guardian_corpus_samples WHERE minor_user_id=$1 AND deleted_at IS NULL', f.child) == int(shared)
            if shared:
                assert await f.objects.get(f.reference) == b'legacy-corpus'
            else:
                with pytest.raises(FileNotFoundError):
                    await f.objects.get(f.reference)
        finally:
            await admin.close()
    finally:
        await close(f)


async def test_captured_children_survive_account_retry_after_archive_and_erase_failure(postgres_database, tmp_path):
    f = await fixture(postgres_database, tmp_path)
    try:
        await seed_child_metadata(f)
        original = f.governance._subject_deletion.delete_subject
        async def fail(scope):
            raise RuntimeError('subject store unavailable')
        f.governance._subject_deletion.delete_subject = fail
        with pytest.raises(RuntimeError, match='subject store unavailable'):
            await f.governance.delete_account('parent')
        assert (await f.identity.get_person(f.child, actor_person_id='parent')).status == 'disabled'
        assert await f.store.prepare_account_subject_erasure('parent') == (f.child,)
        assert await f.identity.get_active_manifest('device-1', actor_person_id='parent') is not None
        f.governance._subject_deletion.delete_subject = original
        assert (await f.governance.delete_account('parent'))['status'] == 'completed'
        assert f.ledger.completed() == (('parent', f.child, False),)
    finally:
        await close(f)


async def test_account_erasure_removes_completed_release_mappings(postgres_database, tmp_path):
    f = await fixture(postgres_database, tmp_path)
    admin = await asyncpg.connect(postgres_database.owner_dsn())
    try:
        assert (await f.governance.delete_account('parent'))['status'] == 'completed'
        assert await admin.fetchval('SELECT count(*) FROM identity_subject_releases WHERE account_id=$1', 'parent') == 0
    finally:
        await admin.close()
        await close(f)


async def test_other_owners_pending_release_converges_after_account_erases_identity(postgres_database, tmp_path):
    f = await fixture(postgres_database, tmp_path)
    admin = await asyncpg.connect(postgres_database.owner_dsn())
    try:
        await f.identity.register_person(person_id='other', display_name='Other', timezone='Asia/Shanghai',
            subject_category='adult', age_band='adult', age_evidence_status='verified', age_evidence_id='fixture', now=NOW)
        await f.identity.attest_binding_relationship(source_person_id='other', target_person_id=f.child,
            relation_type='guardian_of', actor_person_id='other', now=NOW)
        await bind(f.identity, f.child, owner='other')
        await f.identity.revoke_binding(device_id='device-1', actor_person_id='parent')
        await f.identity.revoke_binding(device_id='device-2', actor_person_id='other')
        assert await f.store.pending_subject_releases() == (('other', f.child),)
        await admin.execute("INSERT INTO archive_evidence_events(event_id,account_id,event_type,schema_version,occurred_at,subject_id,speaker_class,source,payload,content_sha256) VALUES('other-child-event','other','speech.utterance_finalized',1,now(),$1,'owner','asr','{}',repeat('a',64))", f.child)
        assert (await f.governance.delete_account('parent'))['status'] == 'completed'
        assert await admin.fetchval('SELECT count(*) FROM identity_persons WHERE person_id=$1', f.child) == 0
        assert await f.store.pending_subject_releases() == (('other', f.child),)
        async def redact(subject_id, account_id):
            await f.identity.redact_released_subject(person_id=subject_id, actor_person_id=account_id)
        f.subject._redact_identity = redact
        f.subject._pending_releases = f.store.pending_subject_releases
        f.subject._complete_release = f.store.complete_subject_release
        assert await f.subject.retry_pending_deletions() == 1
        assert await admin.fetchval("SELECT count(*) FROM archive_evidence_events WHERE event_id='other-child-event'") == 0
        assert await f.store.pending_subject_releases() == ()
        assert await admin.fetchval('SELECT count(*) FROM identity_subject_releases WHERE subject_id=$1', f.child) == 0
    finally:
        await admin.close()
        await close(f)


async def test_identity_delete_replay_cleans_completed_capture_but_preserves_pending(postgres_database, tmp_path):
    f = await fixture(postgres_database, tmp_path)
    admin = await asyncpg.connect(postgres_database.owner_dsn())
    try:
        assert (await f.governance.delete_account('parent'))['status'] == 'completed'
        await admin.execute("INSERT INTO identity_subject_releases(account_id,subject_id,released_at,completed_at,reason) VALUES('parent','historical-child',now(),now(),'account')")
        await f.identity.delete_account_identity(account_person_id='parent')
        assert await admin.fetchval("SELECT count(*) FROM identity_subject_releases WHERE account_id='parent'") == 0
        await admin.execute("INSERT INTO identity_subject_releases(account_id,subject_id,released_at,reason) VALUES('parent','pending-child',now(),'account')")
        with pytest.raises(asyncpg.PostgresError, match='subject erasure is incomplete'):
            await f.identity.delete_account_identity(account_person_id='parent')
        assert await admin.fetchval("SELECT count(*) FROM identity_subject_releases WHERE account_id='parent' AND completed_at IS NULL") == 1
    finally:
        await admin.close()
        await close(f)
