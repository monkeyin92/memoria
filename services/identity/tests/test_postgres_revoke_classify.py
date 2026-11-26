from __future__ import annotations

import asyncio
from datetime import timedelta

import asyncpg
import pytest
from services.identity.domain import BindingVersionConflictError, IdentityAccessDeniedError
from services.identity.postgres_store import PostgresIdentityStore
from services.identity.tests.test_redact_bound_subject import NOW, _bound_child

pytestmark = pytest.mark.asyncio


async def setup(database, tmp_path):
    store = PostgresIdentityStore(
        database.role_dsn("memoria_identity"), database.role_dsn("memoria_identity_registration")
    )
    service, child, _ = await _bound_child(tmp_path, store)
    return store, service, child


async def bind(service, child, *, owner="parent", device="device-2", now=NOW):
    return await service.create_binding(
        device_id=device,
        declared_mode="parent_for_child",
        account_owner_person_id=owner,
        primary_subject_ids=(child,),
        roles=((owner, "guardian"), (owner, "device_admin")),
        service_profile_version="parent_for_child-v1",
        policy_bundle_version="multi-subject-v1",
        consent_offer_ids=("offer_minor_voice_session_v1",),
        actor_person_id=owner,
        now=now,
    )


@pytest.mark.parametrize("other_owner", [False, True])
async def test_revoke_preserves_subject_served_by_another_device(
    postgres_database, tmp_path, other_owner
):
    store, service, child = await setup(postgres_database, tmp_path)
    try:
        owner = "parent"
        if other_owner:
            owner = "parent-2"
            await service.register_person(
                person_id=owner,
                display_name="Other",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="fixture-other",
                now=NOW,
            )
            await service.attest_binding_relationship(
                source_person_id=owner,
                target_person_id=child,
                relation_type="guardian_of",
                actor_person_id=owner,
                now=NOW,
            )
        await bind(service, child, owner=owner)
        if other_owner:
            visible = await service.list_active_manifests_for_person(
                child, actor_person_id="parent"
            )
            assert {manifest.device_id for manifest in visible} == {"device-1"}
        result = await service.revoke_binding(
            device_id="device-1", actor_person_id="parent", now=NOW + timedelta(seconds=1)
        )
        assert [(s.subject_id, s.outcome) for s in result.subjects] == [(child, "still_served")]
        assert (await service.get_person(child, actor_person_id="parent")).status == "active"
        assert await store.pending_subject_releases() == ()
    finally:
        await store.close()


async def test_last_unbind_has_durable_release_and_rebinding_fence(postgres_database, tmp_path):
    store, service, child = await setup(postgres_database, tmp_path)
    try:
        manifest = await service.get_active_manifest("device-1", actor_person_id="parent")
        with pytest.raises(IdentityAccessDeniedError):
            await store.revoke_binding(manifest.binding_id, actor_person_id="stranger", at=NOW)
        result = await service.revoke_binding(
            device_id="device-1", actor_person_id="parent", now=NOW + timedelta(seconds=1)
        )
        assert result.subjects[0].outcome == "released"
        assert (await service.get_person(child, actor_person_id="parent")).status == "disabled"
        assert await store.pending_subject_releases() == (("parent", child),)
        with pytest.raises(BindingVersionConflictError):
            await bind(service, child)
    finally:
        await store.close()


@pytest.mark.parametrize("first", ["unbind", "bind"])
async def test_subject_lock_serializes_new_binding_and_last_unbind(
    postgres_database, tmp_path, first
):
    store, service, child = await setup(postgres_database, tmp_path)
    gate = await asyncpg.connect(postgres_database.owner_dsn())
    observer = await asyncpg.connect(postgres_database.owner_dsn())
    tx = gate.transaction()
    await tx.start()
    await gate.execute("SELECT pg_advisory_xact_lock(hashtextextended('subject:' || $1, 0))", child)
    tasks = {}
    try:
        for operation in (first, "bind" if first == "unbind" else "unbind"):
            before = await observer.fetchval(
                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"
            )
            tasks[operation] = asyncio.create_task(
                service.revoke_binding(
                    device_id="device-1", actor_person_id="parent", now=NOW + timedelta(seconds=1)
                )
                if operation == "unbind"
                else bind(service, child)
            )
            async with asyncio.timeout(10):
                while (
                    await observer.fetchval(
                        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"
                    )
                    <= before
                ):
                    await asyncio.sleep(0.01)
        await tx.commit()
        unbind, new = await asyncio.gather(tasks["unbind"], tasks["bind"], return_exceptions=True)
        if first == "unbind":
            assert unbind.subjects[0].outcome == "released"
            assert isinstance(new, BindingVersionConflictError)
        else:
            assert unbind.subjects[0].outcome == "still_served"
            assert not isinstance(new, BaseException)
    finally:
        if not tx._state.name == "COMMITTED":
            await tx.rollback()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await gate.close()
        await observer.close()
        await store.close()


async def test_release_completion_requires_account_context_and_preserves_pending_capture(postgres_database, tmp_path):
    store, service, child = await setup(postgres_database, tmp_path)
    connection = await asyncpg.connect(postgres_database.role_dsn('memoria_identity'))
    try:
        await service.revoke_binding(device_id='device-1', actor_person_id='parent')
        with pytest.raises(asyncpg.PostgresError, match='release actor context mismatch'):
            await connection.execute('SELECT identity_complete_subject_release($1,$2)', 'parent', child)
        async with connection.transaction():
            await connection.execute("SELECT set_config('app.identity_actor','stranger',true)")
            with pytest.raises(asyncpg.PostgresError, match='release actor context mismatch'):
                async with connection.transaction():
                    await connection.execute('SELECT identity_complete_subject_release($1,$2)', 'parent', child)
        assert await store.pending_subject_releases() == (('parent', child),)
        assert not await store.released_subject_identity_absent('parent', child)
        await store.complete_subject_release('parent', child)
        assert await store.pending_subject_releases() == ()
    finally:
        await connection.close()
        await store.close()


async def test_release_replay_does_not_hide_invisible_or_unrecorded_identity(postgres_database, tmp_path):
    store, service, child = await setup(postgres_database, tmp_path)
    try:
        from services.identity.domain import IdentityNotFoundError
        with pytest.raises(IdentityNotFoundError):
            await service.redact_released_subject(person_id=child, actor_person_id='stranger')
        with pytest.raises(IdentityNotFoundError):
            await service.redact_released_subject(person_id='never-recorded', actor_person_id='parent')
    finally:
        await store.close()
