from __future__ import annotations

from functools import partial
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from fastapi import HTTPException
from services.control_api.app.session_termination import (
    AccountSessionTerminator,
    RealtimeConnectionRegistry,
)
from services.control_api.tests.test_lifecycle_authority_postgres import (
    KEY,
    _current_payload,
    _rewrite_current_profile,
    grant,
    stream,
)
from services.control_api.tests.test_lifecycle_authority_postgres import authority as authority
from services.governance.subject_deletion import SubjectDeletionLedger
from services.governance.subject_ports import SubjectScope
from services.governance.tests.test_subject_deletion import _service
from services.session_runtime.postgres_store import (
    SESSION_RUNTIME_SCHEMA_SQL,
    PostgresSessionRuntimeStore,
    SessionRuntimeAuthorityUnavailable,
)
from services.session_runtime.profile_service import sign_runtime_profile_payload
from services.session_runtime.service import build_postgres_session_runtime_service
from testing.postgres_harness import build_template, cloned_database, drop_template

pytestmark = pytest.mark.asyncio


def _terminator(f, store):
    close = partial(f.close.func, **{**f.close.keywords,
        "runtime_service": build_postgres_session_runtime_service(store=store, signing_key=KEY)})
    return AccountSessionTerminator(
        store=f.store, connections=RealtimeConnectionRegistry(), close_subject_session=close,
    )


def _saga(f, tmp_path, terminator):
    ledger = SubjectDeletionLedger.beside(f.store, tmp_path / "unused")
    ledger.initialize()
    service, _ = _service(tmp_path, ledger=ledger)

    async def terminate(scope):
        return await terminator.terminate_subject(scope.subject_id)

    service._terminate_sessions = terminate
    return service, ledger


def _step(ledger):
    with ledger._connect() as connection:
        return connection.execute("SELECT step FROM subject_deletions").fetchone()[0]


async def _assert_pending(f, service, ledger):
    with pytest.raises(HTTPException) as exc:
        await service.delete_subject(SubjectScope("parent", f.child))
    assert exc.value.status_code == 503
    assert exc.value.detail == {"code": "session_runtime_authority_unavailable"}
    assert _step(ledger) == "started"
    assert await f.admin.fetchval("SELECT state FROM session_runtime_contexts WHERE session_id='target'") == "active"
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is None
    assert f.requests == []


@pytest.mark.parametrize("wrong", ["projector", "api", "action"])
async def test_wrong_database_blocks_startup_readiness_and_erasure_then_retries(authority, tmp_path, wrong):
    f = authority
    await grant(f)
    await stream(f, "target")
    await f.identity.revoke_binding(device_id="device-1", actor_person_id="parent", reason="unbind")
    template = build_template(f.database.admin_dsn)
    try:
        with cloned_database(template) as other:
            dsns = {name: (other if name == wrong else f.database).role_dsn(role)
                for name, role in (("api", "memoria_session_api"),
                                   ("action", "memoria_action_executor"),
                                   ("projector", "memoria_session_projector"))}
            store = PostgresSessionRuntimeStore(
                dsn=dsns["api"], action_dsn=dsns["action"], projector_dsn=dsns["projector"],
            )
            for check in (store.initialize, store.readiness):
                with pytest.raises(SessionRuntimeAuthorityUnavailable):
                    await check()
            terminator = _terminator(f, store)
            service, ledger = _saga(f, tmp_path, terminator)
            await _assert_pending(f, service, ledger)
            store._dsn = f.database.role_dsn("memoria_session_api")
            store._action_dsn = f.database.role_dsn("memoria_action_executor")
            store._projector_dsn = f.database.role_dsn("memoria_session_projector")
            assert await service.retry_pending_deletions() == 1
            assert _step(ledger) == "completed"
            assert f.requests == [{"device_id": "device-1", "session_id": "target"}]
            assert await f.admin.fetchval("SELECT state FROM session_runtime_contexts WHERE session_id='target'") == "closed"
    finally:
        drop_template(template)


async def test_equal_database_with_different_roles_and_hostname_alias_is_accepted(authority):
    f = authority
    await grant(f)
    await stream(f, "target")
    parts = urlsplit(f.database.role_dsn("memoria_session_projector"))
    alias = urlunsplit(("postgres", parts.netloc.replace("127.0.0.1", "localhost"), parts.path,
                       "application_name=projector-alias", ""))
    store = PostgresSessionRuntimeStore(
        dsn=f.database.role_dsn("memoria_session_api"),
        action_dsn=f.database.role_dsn("memoria_action_executor"), projector_dsn=alias,
    )
    await store.initialize()
    assert (await store.readiness())["session_runtime_read_role"] == "ready"
    assert await _terminator(f, store).terminate_subject(f.child) == 1
    assert f.requests == [{"device_id": "device-1", "session_id": "target"}]
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is not None


@pytest.mark.parametrize("role", ["memoria_session_api", "memoria_action_executor", "memoria_session_projector"])
async def test_unreadable_database_identity_never_becomes_absence(authority, tmp_path, role):
    f = authority
    await grant(f)
    await stream(f, "target")
    await f.admin.execute("REVOKE EXECUTE ON FUNCTION pg_control_system() FROM PUBLIC")
    for readable in ("memoria_session_api", "memoria_action_executor", "memoria_session_projector"):
        if readable != role:
            await f.admin.execute(f"GRANT EXECUTE ON FUNCTION pg_control_system() TO {readable}")
    service, ledger = _saga(f, tmp_path, f.terminator)
    await _assert_pending(f, service, ledger)
    await f.admin.execute(f"GRANT EXECUTE ON FUNCTION pg_control_system() TO {role}")
    assert await service.retry_pending_deletions() == 1
    assert _step(ledger) == "completed"


async def test_backend_route_is_checked_again_after_successful_initialization(authority, tmp_path, monkeypatch):
    f = authority
    await grant(f)
    await stream(f, "target")
    template = build_template(f.database.admin_dsn)
    try:
        with cloned_database(template) as other:
            connect = f.runtime_store._connect

            async def reroute(dsn, *, application_name):
                if application_name == "memoria-session-projector":
                    dsn = other.role_dsn("memoria_session_projector")
                return await connect(dsn, application_name=application_name)

            monkeypatch.setattr(f.runtime_store, "_connect", reroute)
            service, ledger = _saga(f, tmp_path, f.terminator)
            await _assert_pending(f, service, ledger)
    finally:
        drop_template(template)


async def test_unreachable_database_never_becomes_absence(authority, tmp_path):
    import socket

    f = authority
    await grant(f)
    await stream(f, "target")
    parts = urlsplit(f.runtime_store._projector_dsn)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        f.runtime_store._projector_dsn = urlunsplit((
            parts.scheme, parts.netloc.rsplit(":", 1)[0] + f":{port}", parts.path, "", ""))
        service, ledger = _saga(f, tmp_path, f.terminator)
        await _assert_pending(f, service, ledger)


async def test_database_identity_reads_need_no_new_grants_and_keep_projector_read_only(authority):
    f = authority
    before = await f.admin.fetchval("SELECT proacl::text FROM pg_proc WHERE oid='pg_control_system()'::regprocedure")
    await f.admin.execute(SESSION_RUNTIME_SCHEMA_SQL)
    await f.admin.execute(SESSION_RUNTIME_SCHEMA_SQL)
    assert await f.admin.fetchval("SELECT proacl::text FROM pg_proc WHERE oid='pg_control_system()'::regprocedure") == before
    await f.runtime_store.initialize()
    identity = None
    for role in ("memoria_session_api", "memoria_action_executor", "memoria_session_projector"):
        connection = await asyncpg.connect(f.database.role_dsn(role))
        try:
            actual = await connection.fetchval("SELECT system_identifier::text FROM pg_control_system()")
            assert actual is not None
            if identity is not None:
                assert actual == identity
            identity = actual
            assert await connection.fetchval("SELECT NOT rolsuper AND NOT rolbypassrls FROM pg_roles WHERE rolname=current_user")
            assert not await connection.fetchval("SELECT pg_has_role(current_user, 'pg_monitor', 'MEMBER')")
            assert not await connection.fetchval("SELECT has_table_privilege(current_user, 'session_runtime_contexts', 'UPDATE')")
        finally:
            await connection.close()
    async with f.runtime_store.projector_transaction() as connection:
        assert await connection.fetchval("SHOW transaction_read_only") == "on"
        assert await connection.fetchval("SHOW transaction_isolation") == "repeatable read"


async def test_foreign_signed_unknown_profile_head_blocks_erasure(authority, tmp_path):
    f = authority
    await grant(f)
    target = await stream(f, "target")
    unrelated = await stream(f, "unrelated", confirmed=False)
    await f.admin.execute("UPDATE session_runtime_contexts SET current_runtime_profile_id=$1 WHERE session_id=$2",
                          unrelated.runtime_profile_id, target.session_id)
    service, ledger = _saga(f, tmp_path, f.terminator)
    await _assert_pending(f, service, ledger)


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("selection", ["target", "other_subject", "other_binding", "unknown_safe"])
@pytest.mark.parametrize("field", ["session_id", "runtime_profile_id", "actor_id", "device_id",
                                  "binding_id", "binding_version", "session_epoch", "subject_revision"])
async def test_profile_identity_is_validated_before_any_scope_skip(authority, field, selection, signed):
    f = authority
    await grant(f)
    await stream(f, "target", confirmed=selection != "unknown_safe")
    payload = await _current_payload(f, "target")
    payload[field] = payload[field] + 1 if isinstance(payload[field], int) else "different-identity"
    if signed:
        payload.pop("signature")
        payload["signature"] = sign_runtime_profile_payload(payload, signing_key=KEY)
    await _rewrite_current_profile(f, "target", payload)
    with pytest.raises(HTTPException) as exc:
        await f.close(device_id="device-1", session_id="target", reason="erase",
                      subject_id="other-subject" if selection == "other_subject" else f.child,
                      binding_ids=frozenset({"other-binding"}) if selection == "other_binding" else None)
    assert exc.value.status_code == (503 if signed else 403)
    assert f.requests == []
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is None
    assert await f.admin.fetchval("SELECT state FROM session_runtime_contexts WHERE session_id='target'") == "active"


async def test_active_context_subject_must_match_even_when_profile_would_skip(authority):
    f = authority
    await grant(f)
    await stream(f, "target", confirmed=False)
    await f.admin.execute("UPDATE session_runtime_contexts SET active_subject_id=$1 WHERE session_id='target'", f.child)
    with pytest.raises(HTTPException) as exc:
        await f.terminator.terminate_subject(f.child)
    assert exc.value.status_code == 503
    assert f.requests == []


async def test_closed_authority_advanced_epoch_remains_retryable_after_edge_failure(authority):
    f = authority
    await grant(f)
    profile = await stream(f, "target")
    f.edge_status[0] = 503
    with pytest.raises(HTTPException):
        await f.terminator.terminate_subject(f.child)
    row = await f.admin.fetchrow("SELECT state, session_epoch, active_subject_id FROM session_runtime_contexts WHERE session_id='target'")
    assert tuple(row) == ("closed", profile.session_epoch + 1, None)
    f.edge_status[0] = 200
    assert await f.terminator.terminate_subject(f.child) == 1
    assert len(f.requests) == 2
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is not None


@pytest.mark.parametrize("selection", ["other_subject", "other_binding", "unknown_safe"])
async def test_consistent_unrelated_authority_stays_open(authority, selection):
    f = authority
    manifest = await grant(f)
    await stream(f, "target", confirmed=selection != "unknown_safe")
    assert await f.close(device_id="device-1", session_id="target", reason="erase",
        subject_id="other-subject" if selection == "other_subject" else f.child,
        binding_ids=frozenset({"other-binding"}) if selection == "other_binding" else frozenset({manifest.binding_id})) is False
    assert f.requests == []
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is None


async def test_proven_global_absence_is_a_safe_skip(authority):
    f = authority
    assert await f.close(device_id="device-1", session_id="absent", reason="erase",
                         subject_id=f.child, binding_ids=None) is False
    assert f.requests == []

async def test_database_identity_accepts_ipv4_and_ipv6_loopback_but_not_other_endpoints(authority):
    from services.session_runtime.postgres_connections import _database_identity

    class AddressReadback:
        def __init__(self, connection, address):
            self.connection = connection
            self.address = address

        async def fetchval(self, query):
            return await self.connection.fetchval(
                query.replace("pg_catalog.inet_server_addr()", f"'{self.address}'::inet")
            )

    async with authority.runtime_store.projector_transaction() as connection:
        ipv4 = await _database_identity(AddressReadback(connection, "127.0.0.1"), "memoria_session_projector")
        ipv6 = await _database_identity(AddressReadback(connection, "::1"), "memoria_session_projector")
        remote = await _database_identity(AddressReadback(connection, "192.0.2.1"), "memoria_session_projector")
        assert ipv4 == ipv6
        assert ipv4 is not None
        assert ipv4 != remote
