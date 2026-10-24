"""Real-PostgreSQL contract for unbind: it always erases the child layers.

Unbind has no keep/delete choice any more (#283): the child's summaries and
long-term archive go, the consents that justified them are withdrawn, and the
identity row is cleared. The control API is driven over HTTP while
``child_layer_store`` is a real ``ChildLayerStore`` against a fresh database
from the memory-scope fixture. Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is
set.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app
from services.control_api.app.routes.identity_lifecycle import UnbindRequest
from services.control_api.tests.test_bound_subject_binding import _bind, _owner
from services.control_api.tests.test_guardian_api import _configure
from services.guardian.child_memory import (
    KIND_CHILD_ARCHIVE,
    KIND_DIALOGUE_SUMMARY,
    ArchiveFact,
    DialogueSummary,
)
from services.memory_scope.child_layers import ChildLayerRecord, ChildLayerStore
from services.memory_scope.tests import test_postgres_store
from services.memory_scope.tests.test_child_layers_postgres import (
    _close_layers,
    _layers,
)

pg_env = test_postgres_store.pg_env

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
        reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
    ),
]

DEVICE_ID = "device-unbind-layers"
SUMMARY_TEXT = "孩子聊了小狗和画画"
ARCHIVE_TEXT = "宠物叫豆豆"
LONG_TERM_TEXT = "长期档案：喜欢蓝色"

AuditRow = dict[str, Any]


async def _bound_child(client: AsyncClient, app: Any) -> tuple[str, str, dict[str, str]]:
    owner_id, headers = await _owner(client, app, "unbind-layers-owner")
    created = await _bind(
        client,
        app,
        owner_id=owner_id,
        headers=headers,
        device_id=DEVICE_ID,
        declared_mode="parent_for_child",
        relationship="guardian_of",
        age_band="under_14",
        offers=[
            "offer_minor_voice_session_v1",
            "offer_minor_memory_retention_v1",
        ],
        preferences={"memory_level": "growth_summary"},
    )
    assert created.status_code == 201, created.text
    child_id = created.json()["primary_subject_ids"][0]
    granted = await client.post(
        f"/v1/guardian/minors/{child_id}/consents",
        headers={
            **headers,
            "Idempotency-Key": "unbind-layers-person-consent",
        },
        json={"consent_kind": "memory_retention", "policy_version": "minor-retention-v1"},
    )
    assert granted.status_code == 201, granted.text
    return owner_id, child_id, headers


async def _seed_layers(
    store: ChildLayerStore, *, child_id: str, owner_id: str
) -> tuple[ChildLayerRecord, ChildLayerRecord]:
    summary = await store.write_summary(
        DialogueSummary(
            subject_id=child_id,
            text=SUMMARY_TEXT,
            source_event_id="evt-unbind-summary",
            session_id="sess-unbind",
        ),
        resource_owner_id=owner_id,
        actor_id=owner_id,
        consent_snapshot_id="consent-unbind-summary",
    )
    archive = await store.write_archive_fact(
        ArchiveFact(subject_id=child_id, field="pet_name", text=ARCHIVE_TEXT),
        resource_owner_id=owner_id,
        actor_id=owner_id,
        consent_snapshot_id="consent-unbind-archive",
    )
    return summary, archive


async def _insert_long_term_record(
    admin_db_dsn: str, *, subject_id: str, owner_id: str
) -> str:
    """One non-child memory_records row for the same child subject.

    The child layers are the two kinds the store owns; the server also keeps
    ordinary long-term memories under the same subject id.  They must survive
    a keep-unbind and only the explicit deletion saga may remove them.
    """

    record_id = f"longterm:{uuid.uuid4()}"
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        await admin.execute(
            "INSERT INTO memory_records ("
            " record_id, scope, subject_id, resource_owner_id,"
            " source_evidence_ids, policy_receipt_id, consent_snapshot_id,"
            " memory_type, confidence, retention, retention_expires_at,"
            " payload, created_by_actor_id, created_at)"
            " VALUES ($1, 'personal_private', $2, $3, '[]'::jsonb, $4, $5,"
            " 'semantic', 1.0, 'indefinite', NULL, $6::jsonb, $3, $7)",
            record_id,
            subject_id,
            owner_id,
            f"long-term-receipt:{record_id}",
            f"long-term-consent:{record_id}",
            json.dumps(
                {"kind": "long_term_profile", "text": LONG_TERM_TEXT},
                ensure_ascii=False,
            ),
            datetime.now(UTC),
        )
    finally:
        await admin.close()
    return record_id


async def _record_text(admin_db_dsn: str, record_id: str) -> str | None:
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        raw = await admin.fetchval(
            "SELECT payload FROM memory_records WHERE record_id = $1",
            record_id,
        )
    finally:
        await admin.close()
    if raw is None:
        return None
    payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    text = payload.get("text")
    return str(text) if text is not None else None


async def _memory_counts(admin_db_dsn: str, subject_id: str) -> dict[str, int]:
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        rows = await admin.fetch(
            "SELECT COALESCE(payload ->> 'kind', 'untyped') AS kind,"
            " count(*)::int AS n FROM memory_records"
            " WHERE subject_id = $1 GROUP BY 1 ORDER BY 1",
            subject_id,
        )
    finally:
        await admin.close()
    return {str(row["kind"]): int(row["n"]) for row in rows}


async def _status_event_count(admin_db_dsn: str, record_ids: list[str]) -> int:
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        count = await admin.fetchval(
            "SELECT count(*) FROM memory_status_events"
            " WHERE record_id = ANY($1::text[])",
            record_ids,
        )
    finally:
        await admin.close()
    return int(count)


async def _child_layer_audit(
    admin_db_dsn: str, *, action: str = "child_layer_erase"
) -> list[AuditRow]:
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        rows = await admin.fetch(
            "SELECT * FROM memory_audit_events"
            " WHERE action = $1"
            " ORDER BY created_at, event_id",
            action,
        )
    finally:
        await admin.close()
    return [dict(row) for row in rows]


async def _person_consents(
    app: Any, *, owner_id: str, child_id: str
) -> tuple[Any, ...]:
    return await app.state.guardian_store.list_person_consents(
        subject_person_id=child_id, actor_person_id=owner_id
    )


def _payload(row: AuditRow) -> dict[str, Any]:
    raw = row["payload"]
    loaded = json.loads(raw) if isinstance(raw, str) else dict(raw)
    assert isinstance(loaded, dict)
    return loaded


def _serialized(row: AuditRow) -> str:
    return json.dumps(row, ensure_ascii=False, default=str)


async def _unbind(client: AsyncClient, headers: dict[str, str], *, legacy: bool | None = None) -> Any:
    body: dict[str, object] = {"reason": "unbind"}
    if legacy is not None:
        body["purge_subject_data"] = legacy
    return await client.post(
        f"/v1/devices/{DEVICE_ID}/binding/unbind",
        headers=headers,
        json=body,
    )


async def test_unbind_keeps_accepting_the_removed_switch() -> None:
    from pydantic import ValidationError

    assert UnbindRequest.model_validate({"reason": "unbind"}).purge_subject_data is None
    # An older client still sends the field; the route accepts and ignores it
    # instead of refusing the request with a 422.
    assert (
        UnbindRequest.model_validate({"purge_subject_data": False}).purge_subject_data
        is False
    )
    assert (
        UnbindRequest.model_validate({"purge_subject_data": True}).purge_subject_data
        is True
    )
    with pytest.raises(ValidationError):
        UnbindRequest.model_validate({"purge_child_data": True})


@pytest.mark.asyncio
async def test_unbind_deletes_every_child_layer_without_a_data_switch(
    pg_env: tuple[str, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.child_layer_store = store
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            owner_id, child_id, headers = await _bound_child(client, app)
            summary, archive = await _seed_layers(
                store, child_id=child_id, owner_id=owner_id
            )
            before = await _memory_counts(admin_db_dsn, child_id)
            assert before == {KIND_DIALOGUE_SUMMARY: 1, KIND_CHILD_ARCHIVE: 1}
            consents_before = await _person_consents(
                app, owner_id=owner_id, child_id=child_id
            )
            assert [item.consent_kind for item in consents_before] == [
                "memory_retention"
            ]
            started_at = datetime.now(UTC)

            # No switch is sent at all: unbind still erases the layers.
            response = await _unbind(client, headers)

            finished_at = datetime.now(UTC)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["child_layers_purged"] == "completed"
            assert body["consents_withdrawn"] >= 1
            assert "retention_basis_recorded" not in body
            assert await _memory_counts(admin_db_dsn, child_id) == {}
            assert await store.list(
                subject_id=child_id,
                kind=KIND_DIALOGUE_SUMMARY,
                viewer_subject_id=owner_id,
            ) == []
            assert await store.list(
                subject_id=child_id,
                kind=KIND_CHILD_ARCHIVE,
                viewer_subject_id=owner_id,
            ) == []
            consents_after = await _person_consents(
                app, owner_id=owner_id, child_id=child_id
            )
            assert [item.consent_id for item in consents_after] == [
                item.consent_id for item in consents_before
            ]
            assert all(item.revoked_at is not None for item in consents_after)
            # Nothing was kept, so no keep-basis row is written.
            assert (
                await _child_layer_audit(admin_db_dsn, action="child_layer_retain")
                == []
            )
            audit = await _child_layer_audit(admin_db_dsn)
            assert {_payload(row)["kind"] for row in audit} == {
                KIND_DIALOGUE_SUMMARY,
                KIND_CHILD_ARCHIVE,
            }
            for row in audit:
                assert row["action"] == "child_layer_erase"
                assert row["actor_subject_id"] == "system:child_layer_erase"
                assert started_at <= row["created_at"] <= finished_at
            serialized = "\n".join(_serialized(row) for row in audit)
            for value in (
                child_id,
                owner_id,
                body["binding_id"],
                DEVICE_ID,
                summary.record_id,
                archive.record_id,
                SUMMARY_TEXT,
                ARCHIVE_TEXT,
                "使用人",
            ):
                assert value not in serialized, value
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)


@pytest.mark.asyncio
async def test_unbind_written_with_the_old_keep_flag_still_deletes(
    pg_env: tuple[str, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An old client sends purge_subject_data=false; the switch is ignored."""

    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.child_layer_store = store
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            owner_id, child_id, headers = await _bound_child(client, app)
            await _seed_layers(store, child_id=child_id, owner_id=owner_id)
            assert (await _memory_counts(admin_db_dsn, child_id))[KIND_CHILD_ARCHIVE] == 1

            response = await _unbind(client, headers, legacy=False)

            assert response.status_code == 200, response.text
            assert response.json()["child_layers_purged"] == "completed"
            assert await _memory_counts(admin_db_dsn, child_id) == {}
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)


@pytest.mark.asyncio
async def test_a_rebound_device_starts_without_the_erased_child(
    pg_env: tuple[str, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.child_layer_store = store
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            owner_id, child_id, headers = await _bound_child(client, app)
            await _seed_layers(store, child_id=child_id, owner_id=owner_id)

            unbound = await _unbind(client, headers)
            assert unbound.status_code == 200, unbound.text
            assert await _memory_counts(admin_db_dsn, child_id) == {}

            # The erased child is a disabled placeholder, so the account is
            # offered no one to reuse; binding again creates a new child.
            candidates = await client.get(
                "/v1/device-bindings/subject-candidates", headers=headers
            )
            assert candidates.status_code == 200, candidates.text
            assert candidates.json()["subjects"] == []

            rebound = await _bind(
                client,
                app,
                owner_id=owner_id,
                headers=headers,
                device_id="device-unbind-layers-2",
                declared_mode="parent_for_child",
                relationship="guardian_of",
                age_band="under_14",
                offers=["offer_minor_voice_session_v1"],
            )
            assert rebound.status_code == 201, rebound.text
            new_child = rebound.json()["primary_subject_ids"][0]
            assert new_child != child_id
            assert await _memory_counts(admin_db_dsn, new_child) == {}
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)


@pytest.mark.asyncio
async def test_new_identity_after_unbind_does_not_restore_the_erased_layers(
    pg_env: tuple[str, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.child_layer_store = store
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            owner_a, child_a, headers_a = await _bound_child(client, app)
            await _seed_layers(store, child_id=child_a, owner_id=owner_a)

            unbound = await _unbind(client, headers_a)
            assert unbound.status_code == 200, unbound.text

            # A new account (identity partition lost, new sign-in) claims the
            # released device and binds a different child.
            owner_b, headers_b = await _owner(client, app, "unbind-layers-owner-b")
            rebound = await _bind(
                client,
                app,
                owner_id=owner_b,
                headers=headers_b,
                device_id=DEVICE_ID,
                declared_mode="parent_for_child",
                relationship="guardian_of",
                age_band="under_14",
                offers=["offer_minor_voice_session_v1"],
            )
            assert rebound.status_code == 201, rebound.text
            child_b = rebound.json()["primary_subject_ids"][0]
            assert child_b != child_a
            assert await _memory_counts(admin_db_dsn, child_a) == {}
            assert await _memory_counts(admin_db_dsn, child_b) == {}
            # The erased child is not offered to the new account; only its own
            # freshly declared one is.
            candidates_b = await client.get(
                "/v1/device-bindings/subject-candidates", headers=headers_b
            )
            assert [
                item["person_id"] for item in candidates_b.json()["subjects"]
            ] == [child_b]
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)
