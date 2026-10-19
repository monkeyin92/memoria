"""Real-PostgreSQL contract for unbind: keep the child layers or purge them.

The control API is driven over HTTP while ``child_layer_store`` is a real
``ChildLayerStore`` against a fresh database from the memory-scope fixture.
Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app
from services.control_api.app.routes.identity_lifecycle import UnbindRequest
from services.control_api.tests.child_layer_test_support import (
    FailOnSubjectDelete,
    record_layer_erasures,
)
from services.control_api.tests.test_binding_subject_candidates import _rebind_existing
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
        offers=["offer_minor_voice_session_v1"],
    )
    assert created.status_code == 201, created.text
    return owner_id, created.json()["primary_subject_ids"][0], headers


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


async def _child_layer_audit(admin_db_dsn: str) -> list[AuditRow]:
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        rows = await admin.fetch(
            "SELECT * FROM memory_audit_events"
            " WHERE action = 'child_layer_erase'"
            " ORDER BY created_at, event_id"
        )
    finally:
        await admin.close()
    return [dict(row) for row in rows]


def _payload(row: AuditRow) -> dict[str, Any]:
    raw = row["payload"]
    loaded = json.loads(raw) if isinstance(raw, str) else dict(raw)
    assert isinstance(loaded, dict)
    return loaded


def _serialized(row: AuditRow) -> str:
    return json.dumps(row, ensure_ascii=False, default=str)


async def _unbind(
    client: AsyncClient, headers: dict[str, str], *, purge: bool | None
) -> Any:
    body: dict[str, object] = {"reason": "unbind"}
    if purge is not None:
        body["purge_subject_data"] = purge
    return await client.post(
        f"/v1/devices/{DEVICE_ID}/binding/unbind",
        headers=headers,
        json=body,
    )


async def test_unbind_request_defaults_to_keeping_subject_data() -> None:
    assert UnbindRequest().purge_subject_data is False
    assert UnbindRequest.model_validate({"reason": "unbind"}).purge_subject_data is False
    assert (
        UnbindRequest.model_validate({"purge_subject_data": False}).purge_subject_data
        is False
    )


@pytest.mark.asyncio
async def test_keep_unbind_omitting_flag_preserves_all_layers_and_retention_expires(
    pg_env: tuple[str, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.child_layer_store = store
    deletion = FailOnSubjectDelete()
    app.state.subject_deletion = deletion
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            owner_id, child_id, headers = await _bound_child(client, app)
            fresh_summary = await store.write_summary(
                DialogueSummary(
                    subject_id=child_id,
                    text=SUMMARY_TEXT,
                    source_event_id="evt-keep-fresh",
                    session_id="sess-keep",
                ),
                resource_owner_id=owner_id,
                actor_id=owner_id,
                consent_snapshot_id="consent-keep-summary",
            )
            expired_summary = await store.write_summary(
                DialogueSummary(
                    subject_id=child_id,
                    text="孩子聊了天气",
                    source_event_id="evt-keep-expired",
                    session_id="sess-old",
                ),
                resource_owner_id=owner_id,
                actor_id=owner_id,
                consent_snapshot_id="consent-keep-old",
                created_at=datetime.now(UTC) - timedelta(days=31),
            )
            archive = await store.write_archive_fact(
                ArchiveFact(subject_id=child_id, field="pet_name", text=ARCHIVE_TEXT),
                resource_owner_id=owner_id,
                actor_id=owner_id,
                consent_snapshot_id="consent-keep-archive",
            )
            long_term_id = await _insert_long_term_record(
                admin_db_dsn, subject_id=child_id, owner_id=owner_id
            )
            before = await _memory_counts(admin_db_dsn, child_id)
            assert before == {
                KIND_DIALOGUE_SUMMARY: 2,
                KIND_CHILD_ARCHIVE: 1,
                "long_term_profile": 1,
            }
            layer_calls = record_layer_erasures(monkeypatch, store)

            response = await _unbind(client, headers, purge=None)

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["child_layers_purged"] == "not_requested"
            assert body["subject_deletion"] == "not_requested"
            assert layer_calls == []
            assert deletion.calls == []
            assert await _memory_counts(admin_db_dsn, child_id) == before
            assert await _child_layer_audit(admin_db_dsn) == []
            summaries = await store.list(
                subject_id=child_id,
                kind=KIND_DIALOGUE_SUMMARY,
                viewer_subject_id=owner_id,
            )
            assert {item.record_id for item in summaries} == {
                fresh_summary.record_id,
                expired_summary.record_id,
            }
            assert [
                item.record_id
                for item in await store.list(
                    subject_id=child_id,
                    kind=KIND_CHILD_ARCHIVE,
                    viewer_subject_id=owner_id,
                )
            ] == [archive.record_id]
            assert await _record_text(admin_db_dsn, long_term_id) == LONG_TERM_TEXT

            # 30-day retention still runs after a keep-unbind: only the due row.
            expired = await store.expire_due_summaries(subject_id=child_id)
            assert int(expired["erased"]["memory_records"]) == 1
            assert [
                item.record_id
                for item in await store.list(
                    subject_id=child_id,
                    kind=KIND_DIALOGUE_SUMMARY,
                    viewer_subject_id=owner_id,
                )
            ] == [fresh_summary.record_id]
            # The 180-day archive pass still removes the archive, and the
            # ordinary long-term record stays with the account either way.
            archived = await store.purge_archive(subject_id=child_id)
            assert int(archived["erased"]["memory_records"]) == 1
            assert (
                await store.list(
                    subject_id=child_id,
                    kind=KIND_CHILD_ARCHIVE,
                    viewer_subject_id=owner_id,
                )
                == []
            )
            after_retention = await _memory_counts(admin_db_dsn, child_id)
            assert after_retention == {
                KIND_DIALOGUE_SUMMARY: 1,
                "long_term_profile": 1,
            }
            assert await _record_text(admin_db_dsn, long_term_id) == LONG_TERM_TEXT
            retention_audit = await _child_layer_audit(admin_db_dsn)
            assert {_payload(row)["reason"] for row in retention_audit} == {
                "retention_expiry"
            }
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)


@pytest.mark.asyncio
async def test_rebind_same_child_after_keep_unbind_preserves_counts(
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

            unbound = await _unbind(client, headers, purge=False)
            assert unbound.status_code == 200, unbound.text
            assert unbound.json()["child_layers_purged"] == "not_requested"

            candidates = await client.get(
                "/v1/device-bindings/subject-candidates", headers=headers
            )
            assert candidates.status_code == 200, candidates.text
            assert [item["person_id"] for item in candidates.json()["subjects"]] == [
                child_id
            ]

            rebound = await _rebind_existing(
                client,
                app,
                owner_id=owner_id,
                headers=headers,
                device_id="device-unbind-layers-2",
                child_id=child_id,
            )
            assert rebound.status_code == 201, rebound.text
            assert rebound.json()["primary_subject_ids"] == [child_id]
            assert await _memory_counts(admin_db_dsn, child_id) == before
            assert [
                item.record_id
                for item in await store.list(
                    subject_id=child_id,
                    kind=KIND_DIALOGUE_SUMMARY,
                    viewer_subject_id=owner_id,
                )
            ] == [summary.record_id]
            assert [
                item.record_id
                for item in await store.list(
                    subject_id=child_id,
                    kind=KIND_CHILD_ARCHIVE,
                    viewer_subject_id=owner_id,
                )
            ] == [archive.record_id]
            assert await _child_layer_audit(admin_db_dsn) == []
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)


@pytest.mark.asyncio
async def test_new_identity_and_reprovision_do_not_erase_existing_child_layers(
    pg_env: tuple[str, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.child_layer_store = store
    deletion = FailOnSubjectDelete()
    app.state.subject_deletion = deletion
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            owner_a, child_a, headers_a = await _bound_child(client, app)
            await _seed_layers(store, child_id=child_a, owner_id=owner_a)
            before_a = await _memory_counts(admin_db_dsn, child_a)
            layer_calls = record_layer_erasures(monkeypatch, store)

            unbound = await _unbind(client, headers_a, purge=False)
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
            assert layer_calls == []
            assert deletion.calls == []
            assert await _memory_counts(admin_db_dsn, child_a) == before_a
            assert await _memory_counts(admin_db_dsn, child_b) == {}
            candidates_b = await client.get(
                "/v1/device-bindings/subject-candidates", headers=headers_b
            )
            assert [
                item["person_id"] for item in candidates_b.json()["subjects"]
            ] == [child_b]
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)


@pytest.mark.asyncio
async def test_unbind_purge_deletes_layers_and_writes_content_free_audit(
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
            started_at = datetime.now(UTC)

            response = await _unbind(client, headers, purge=True)

            finished_at = datetime.now(UTC)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["child_layers_purged"] == "completed"
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
            # Explicit delete leaves no memory record or status row behind.
            assert await _memory_counts(admin_db_dsn, child_id) == {}
            assert (
                await _status_event_count(
                    admin_db_dsn, [summary.record_id, archive.record_id]
                )
                == 0
            )

            audit = await _child_layer_audit(admin_db_dsn)
            assert len(audit) == 2, audit
            by_kind = {_payload(row)["kind"]: row for row in audit}
            assert set(by_kind) == {KIND_DIALOGUE_SUMMARY, KIND_CHILD_ARCHIVE}
            expected_records = {
                KIND_DIALOGUE_SUMMARY: 1,
                KIND_CHILD_ARCHIVE: 1,
            }
            for kind, expected in expected_records.items():
                row = by_kind[kind]
                payload = _payload(row)
                assert row["action"] == "child_layer_erase"
                assert row["actor_subject_id"] == "system:child_layer_erase"
                assert row["subject_id"] is None
                assert row["record_id"] is None
                assert row["proposal_id"] is None
                assert set(payload) == {
                    "kind",
                    "memory_records",
                    "memory_status_events",
                    "only_expired",
                    "reason",
                }
                assert payload["memory_records"] == expected
                assert payload["memory_status_events"] == expected
                assert payload["only_expired"] is False
                assert payload["reason"] == "unbind_purge"
                assert started_at <= row["created_at"] <= finished_at
                assert "previous_value" not in _serialized(row)

            serialized = "\n".join(_serialized(row) for row in audit)
            forbidden = [
                child_id,
                owner_id,
                body["binding_id"],
                DEVICE_ID,
                summary.record_id,
                archive.record_id,
                SUMMARY_TEXT,
                ARCHIVE_TEXT,
                "使用人",
                "evt-unbind-summary",
                "sess-unbind",
            ]
            for value in forbidden:
                assert value not in serialized, value
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)
