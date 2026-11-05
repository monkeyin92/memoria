"""Internal session-recap ingest: idempotent summary, resume archives, receipts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from services.control_api.app.routes import child_session_recap as route
from services.guardian.child_memory import (
    KIND_DIALOGUE_SUMMARY,
    ArchiveFact,
    DialogueSummary,
    dialogue_summary_policy_receipt,
)
from services.memory_scope.child_layers import ChildLayerRecord, EnsuredChildLayer

pytestmark = pytest.mark.hermetic


@dataclass
class _Consent:
    consent_id: str


class _FakeGuardian:
    def __init__(self, *, memory: bool = True, archive: bool = True) -> None:
        self.memory = memory
        self.archive = archive

    async def active_consent(self, *, minor_user_id: str, consent_kind: str) -> _Consent | None:
        if consent_kind == "memory_retention":
            return _Consent("mem-1") if self.memory else None
        if consent_kind == "child_archive":
            return _Consent("arch-1") if self.archive else None
        return None


@dataclass
class _FakeLayers:
    summaries: list[DialogueSummary] = field(default_factory=list)
    facts: list[ArchiveFact] = field(default_factory=list)
    births: list[Any] = field(default_factory=list)
    by_receipt: dict[str, ChildLayerRecord] = field(default_factory=dict)
    archive_by_key: dict[tuple[str, str, str], ChildLayerRecord] = field(default_factory=dict)
    fail_archive_once: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    insert_attempts: int = 0

    async def ensure_summary(self, summary: DialogueSummary, **_: Any) -> EnsuredChildLayer:
        receipt = dialogue_summary_policy_receipt(
            subject_id=summary.subject_id, session_id=summary.session_id
        )
        async with self._lock:
            self.insert_attempts += 1
            existing = self.by_receipt.get(receipt)
            if existing is not None:
                return EnsuredChildLayer(record=existing, created=False)
            record = ChildLayerRecord(
                record_id=f"dlgsum:{len(self.by_receipt) + 1}",
                subject_id=summary.subject_id,
                kind=KIND_DIALOGUE_SUMMARY,
                text=summary.text,
                field=None,
                session_id=summary.session_id,
                created_at=datetime.now(UTC),
                retention_expires_at=None,
                payload={},
            )
            self.by_receipt[receipt] = record
            self.summaries.append(summary)
            return EnsuredChildLayer(record=record, created=True)

    async def write_summary(self, summary: DialogueSummary, **kwargs: Any) -> ChildLayerRecord:
        return (await self.ensure_summary(summary, **kwargs)).record

    async def upsert_archive_fact(self, fact: ArchiveFact, **kwargs: Any) -> ChildLayerRecord:
        if self.fail_archive_once:
            self.fail_archive_once = False
            raise RuntimeError("simulated archive crash")
        key = (fact.subject_id, fact.field, fact.text.strip())
        async with self._lock:
            existing = self.archive_by_key.get(key)
            if existing is not None:
                return existing
            record = ChildLayerRecord(
                record_id=f"charch:{len(self.archive_by_key) + 1}",
                subject_id=fact.subject_id,
                kind="child_archive",
                text=fact.text,
                field=fact.field,
                session_id=None,
                created_at=datetime.now(UTC),
                retention_expires_at=None,
                payload={},
            )
            self.archive_by_key[key] = record
            self.facts.append(fact)
            self.births.append(kwargs.get("birth_decision"))
            return record


class _FakeActivity:
    def __init__(self) -> None:
        self.touched: list[str] = []

    async def touch(self, *, subject_person_id: str, source: str) -> Any:
        self.touched.append(f"{subject_person_id}:{source}")
        return datetime.now(UTC)


def _app(
    *, memory: bool = True, archive: bool = True
) -> tuple[FastAPI, _FakeLayers, _FakeActivity]:
    app = FastAPI()
    app.include_router(route.router)
    layers = _FakeLayers()
    activity = _FakeActivity()
    app.state.child_layer_store = layers
    app.state.guardian_store = _FakeGuardian(memory=memory, archive=archive)
    app.state.subject_activity_store = activity
    app.state.settings = SimpleNamespace(
        internal_token=lambda capability: "archive-write-token-32chars-min!!"
    )
    return app, layers, activity


def _payload(
    *,
    subject_id: str = "child-1",
    session_id: str = "sess-1",
    summary_text: str = "聊了恐龙和小狗",
    facts: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "subject_id": subject_id,
        "resource_owner_id": "parent-1",
        "session_id": session_id,
        "source_event_id": f"end:{session_id}",
        "summary_text": summary_text,
        "archive_facts": facts
        if facts is not None
        else [{"field": "preference", "text": "喜欢恐龙"}],
        "skipped_reason": None,
    }


@pytest.mark.asyncio
async def test_ingest_writes_summary_and_facts_when_both_consents_present() -> None:
    app, layers, activity = _app(memory=True, archive=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/internal/child-memory/session-recap",
            headers={"X-Memoria-Internal-Token": "archive-write-token-32chars-min!!"},
            json=_payload(),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] is False
    assert body["summary_created"] is True
    assert body["summary_write_mode"] == "sync_persisted"
    assert body["archive_write_mode"] == "sync_persisted"
    assert body["summary_record_id"] == "dlgsum:1"
    assert body["archive_record_ids"] == ["charch:1"]
    assert layers.summaries[0].text == "聊了恐龙和小狗"
    assert layers.facts[0].text == "喜欢恐龙"
    assert activity.touched == ["child-1:dialogue"]
    birth = layers.births[0]
    assert birth.fact.provenance == "auto_extract"
    assert birth.lifecycle == "pending_confirm"
    assert birth.fact.confidence is not None
    assert birth.fact.confidence < 1.0
    assert birth.record_id


@pytest.mark.asyncio
async def test_ingest_drops_archive_facts_without_child_archive_consent() -> None:
    app, layers, _activity = _app(memory=True, archive=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/internal/child-memory/session-recap",
            headers={"X-Memoria-Internal-Token": "archive-write-token-32chars-min!!"},
            json=_payload(),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["archive_record_ids"] == []
    assert body["archive_write_mode"] == "not_written"
    assert body["summary_write_mode"] == "sync_persisted"
    assert layers.facts == []
    assert len(layers.summaries) == 1


@pytest.mark.asyncio
async def test_ingest_skipped_reason_writes_nothing() -> None:
    app, layers, _activity = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/internal/child-memory/session-recap",
            headers={"X-Memoria-Internal-Token": "archive-write-token-32chars-min!!"},
            json={
                "subject_id": "child-1",
                "resource_owner_id": "parent-1",
                "session_id": "sess-1",
                "source_event_id": "end:sess-1",
                "summary_text": None,
                "archive_facts": [],
                "skipped_reason": "fewer_than_two_turns",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] is True
    assert body["summary_write_mode"] == "not_written"
    assert body["archive_write_mode"] == "not_written"
    assert layers.summaries == []
    assert layers.facts == []


@pytest.mark.asyncio
async def test_retry_after_partial_write_resumes_archives_without_duplicate_summary() -> None:
    app, layers, _activity = _app()
    layers.fail_archive_once = True
    headers = {"X-Memoria-Internal-Token": "archive-write-token-32chars-min!!"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="simulated archive crash"):
            await client.post(
                "/v1/internal/child-memory/session-recap",
                headers=headers,
                json=_payload(),
            )
        assert len(layers.summaries) == 1
        assert layers.facts == []
        response = await client.post(
            "/v1/internal/child-memory/session-recap",
            headers=headers,
            json=_payload(),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["summary_created"] is False
    assert body["reason"] == "session_summary_already_written"
    assert body["summary_write_mode"] == "sync_persisted"
    assert body["archive_write_mode"] == "sync_persisted"
    assert body["archive_record_ids"] == ["charch:1"]
    assert len(layers.summaries) == 1
    assert len(layers.facts) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_requests_write_one_summary() -> None:
    app, layers, _activity = _app()
    headers = {"X-Memoria-Internal-Token": "archive-write-token-32chars-min!!"}
    payload = _payload(session_id="sess-concurrent")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

        async def _once() -> dict[str, Any]:
            response = await client.post(
                "/v1/internal/child-memory/session-recap",
                headers=headers,
                json=payload,
            )
            assert response.status_code == 200
            return response.json()

        bodies = await asyncio.gather(*[_once() for _ in range(8)])

    summary_ids = {body["summary_record_id"] for body in bodies}
    assert summary_ids == {"dlgsum:1"}
    assert len(layers.summaries) == 1
    assert sum(1 for body in bodies if body["summary_created"]) == 1
    assert all(body["summary_write_mode"] == "sync_persisted" for body in bodies)
    assert all(body["archive_record_ids"] == ["charch:1"] for body in bodies)


@pytest.mark.asyncio
async def test_old_session_retry_after_many_newer_summaries_stays_unique() -> None:
    app, layers, _activity = _app()
    headers = {"X-Memoria-Internal-Token": "archive-write-token-32chars-min!!"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/v1/internal/child-memory/session-recap",
            headers=headers,
            json=_payload(session_id="sess-old", facts=[]),
        )
        assert first.status_code == 200
        old_id = first.json()["summary_record_id"]
        for index in range(25):
            response = await client.post(
                "/v1/internal/child-memory/session-recap",
                headers=headers,
                json=_payload(session_id=f"sess-new-{index}", facts=[]),
            )
            assert response.status_code == 200
        retry = await client.post(
            "/v1/internal/child-memory/session-recap",
            headers=headers,
            json=_payload(
                session_id="sess-old",
                facts=[{"field": "preference", "text": "喜欢恐龙"}],
            ),
        )
    assert retry.status_code == 200
    body = retry.json()
    assert body["summary_record_id"] == old_id
    assert body["summary_created"] is False
    assert body["archive_write_mode"] == "sync_persisted"
    assert len(layers.summaries) == 26


@pytest.mark.asyncio
async def test_same_session_id_across_subjects_are_independent() -> None:
    app, layers, _activity = _app()
    headers = {"X-Memoria-Internal-Token": "archive-write-token-32chars-min!!"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        a = await client.post(
            "/v1/internal/child-memory/session-recap",
            headers=headers,
            json=_payload(subject_id="child-a", session_id="shared-sess", facts=[]),
        )
        b = await client.post(
            "/v1/internal/child-memory/session-recap",
            headers=headers,
            json=_payload(subject_id="child-b", session_id="shared-sess", facts=[]),
        )
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["summary_record_id"] != b.json()["summary_record_id"]
    assert len(layers.summaries) == 2
