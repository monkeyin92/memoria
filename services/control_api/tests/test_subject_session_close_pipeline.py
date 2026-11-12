from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from services.control_api.app import device_session_close
from services.control_api.app.session_termination import (
    AccountSessionTerminator,
    RealtimeConnectionRegistry,
)
from services.control_api.tests.test_device_control_api import _close_report_fixture
from services.governance.subject_ports import SubjectScope
from services.governance.tests.test_subject_deletion import _service

pytestmark = pytest.mark.asyncio


class Directory:
    def __init__(self):
        self.expired = []

    async def expire(self, session_id):
        self.expired.append(session_id)


class ScopedAuthority:
    def __init__(self, authority, subjects):
        self.authority, self.subjects = authority, subjects

    async def close_session(self, *, expected_subject_id=None, **kwargs):
        from services.session_runtime.errors import PersistentSessionSubjectChanged

        if (
            expected_subject_id is not None
            and self.subjects.get(kwargs["session_id"]) != expected_subject_id
        ):
            raise PersistentSessionSubjectChanged("subject changed")
        return await self.authority.close_session(**kwargs)


def seed(store, session_id, active_subject):
    now = datetime.now(UTC)
    store.create_device_media_session(
        session_id=session_id,
        device_id="dev_test_01",
        binding_id="fixture-binding",
        binding_version=1,
        subject_id="person_a",
        active_subject_id=active_subject,
        client_id="fixture-client",
        runtime="direct_voice_core",
        protocol_version=2,
        stream_epoch=1,
        firmware_version="test",
        board_profile="test",
        runtime_profile_version=1,
        settings_version=0,
        audio_mode_requested="half_duplex_safe",
        ticket_jti=session_id,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=2)).isoformat(),
    )


async def test_subject_close_calls_edge_and_preserves_switched_unknown_and_other_subjects(
    tmp_path, monkeypatch
):
    app, store, authority = _close_report_fixture(tmp_path)
    seed(store, "switched-to-b", "person_a")
    seed(store, "unknown-safe", "person_a")
    seed(store, "other-child", "person_b")
    seed(store, "switched-to-a", "person_b")
    calls = []
    original = httpx.AsyncClient

    def edge(request):
        calls.append(request)
        return httpx.Response(200, json={"delivered": True})

    monkeypatch.setattr(
        device_session_close.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(edge), **kwargs),
    )
    directory = Directory()
    connections = RealtimeConnectionRegistry()
    close = partial(
        device_session_close.close_media_session_pipeline,
        store=store,
        runtime_service=ScopedAuthority(
            authority,
            {
                "session-close-1": "person_a",
                "switched-to-a": "person_a",
                "switched-to-b": "person_b",
                "unknown-safe": None,
                "other-child": "person_b",
            },
        ),
        directory=directory,
        settings=SimpleNamespace(
            media_edge_internal_control_url="http://edge",
            media_edge_internal_control_token="x" * 32,
        ),
    )
    terminator = AccountSessionTerminator(
        store=store, connections=connections, close_device_session=close
    )
    assert await terminator.terminate_subject("person_a", actor_id="parent") == 2
    assert len(calls) == 2
    assert calls[0].url.path == "/v1/internal/device-runtime/session-close"
    assert calls[0].read() == b'{"device_id":"dev_test_01","session_id":"session-close-1"}'
    assert set(directory.expired) == {"session-close-1", "switched-to-a"}
    assert len(authority.calls) == 2
    assert connections._revoked_accounts == set()
    for session_id in ("switched-to-b", "unknown-safe", "other-child"):
        assert store.get_device_media_session(session_id=session_id)["closed_at"] is None


async def test_edge_failure_leaves_projection_and_saga_retryable(tmp_path, monkeypatch):
    app, store, authority = _close_report_fixture(tmp_path)
    original = httpx.AsyncClient
    status = [503]
    requests = []

    def edge(request):
        requests.append(request)
        return httpx.Response(status[0], json={"delivered": True})

    monkeypatch.setattr(
        device_session_close.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(edge), **kwargs),
    )
    close = partial(
        device_session_close.close_media_session_pipeline,
        store=store,
        runtime_service=ScopedAuthority(authority, {"session-close-1": "person_a"}),
        directory=Directory(),
        settings=SimpleNamespace(
            media_edge_internal_control_url="http://edge",
            media_edge_internal_control_token="x" * 32,
        ),
    )
    terminator = AccountSessionTerminator(
        store=store, connections=RealtimeConnectionRegistry(), close_device_session=close
    )
    store.delete_voice_sessions(user_id="person_a")

    async def terminate(scope):
        return await terminator.terminate_subject(scope.subject_id, actor_id=scope.account_id)

    service, parts = _service(tmp_path)
    service._terminate_sessions = terminate
    scope = SubjectScope("parent", "person_a")
    with pytest.raises(HTTPException) as failed:
        await service.delete_subject(scope)
    assert failed.value.status_code == 503
    assert store.get_device_media_session(session_id="session-close-1")["closed_at"] is None
    with parts["ledger"]._connect() as conn:
        assert conn.execute("SELECT step FROM subject_deletions").fetchone()[0] == "started"
    status[0] = 200
    authority.replay_already_closed = True
    assert await service.retry_pending_deletions() == 1
    assert len(requests) == 2
    assert authority.calls[-1]["actor_id"] == "parent"
    assert store.get_device_media_session(session_id="session-close-1")["closed_at"] is not None


@pytest.mark.parametrize(
    "scope,kind,expected",
    [
        ("bound_subject", "minor_voice_session", ["subject"]),
        ("minor_account", "minor_voice_session", ["subject", "account"]),
        ("bound_subject", "memory_retention", []),
        ("minor_account", "memory_retention", ["account"]),
    ],
)
async def test_guardian_revocation_scope_preserves_owner_account(scope, kind, expected):
    from services.control_api.app.guardian_revocation import guardian_revocation_hook
    from services.guardian.consent import ConsentRevocation

    calls = []

    class Terminator:
        async def terminate_subject(self, subject, **kwargs):
            calls.append("subject")
            assert subject == "child"
            assert kwargs["binding_ids"] == (frozenset({"owned"}) if scope == "bound_subject" else None)

        async def terminate_account(self, account):
            calls.append("account")
            assert account == "child"

    class Identity:
        async def list_active_manifests_for_person(self, *args, **kwargs):
            return (SimpleNamespace(binding_id="owned", account_owner_id="parent", primary_subject_ids=("child",)),)
    hook = guardian_revocation_hook(Terminator(), identity=Identity(), bound_consent=None)
    await hook(ConsentRevocation("child", "parent", kind, scope))
    assert calls == expected


async def test_postgres_projection_selection_preserves_unknown_and_switched_subjects(
    postgres_database,
):
    from services.control_api.app.database import MemoryStore

    store = MemoryStore(dsn=postgres_database.role_dsn("memoria_control"), initialize_schema=False)
    try:
        seed(store, "target", "person_a")
        seed(store, "switched", "person_b")
        seed(store, "unknown", None)
        assert set(store.open_device_media_session_ids()) == {"target", "switched", "unknown"}
        assert store.open_device_media_session_ids(subject_id="person_a") == ("target",)
    finally:
        store.close()
