"""Internal session-recap ingest: consent gates, skip, and no transcript fields."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from services.control_api.app.routes import child_session_recap as route
from services.guardian.child_memory import KIND_DIALOGUE_SUMMARY, ArchiveFact, DialogueSummary
from services.memory_scope.child_layers import ChildLayerRecord


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


class _FakeLayers:
    def __init__(self) -> None:
        self.summaries: list[DialogueSummary] = []
        self.facts: list[ArchiveFact] = []
        self.listed: list[ChildLayerRecord] = []

    async def list(self, **_: Any) -> list[ChildLayerRecord]:
        return list(self.listed)

    async def write_summary(self, summary: DialogueSummary, **_: Any) -> ChildLayerRecord:
        self.summaries.append(summary)
        from datetime import UTC, datetime

        return ChildLayerRecord(
            record_id="dlgsum:1",
            subject_id=summary.subject_id,
            kind=KIND_DIALOGUE_SUMMARY,
            text=summary.text,
            field=None,
            session_id=summary.session_id,
            created_at=datetime.now(UTC),
            retention_expires_at=None,
            payload={},
        )

    async def upsert_archive_fact(self, fact: ArchiveFact, **_: Any) -> ChildLayerRecord:
        self.facts.append(fact)
        from datetime import UTC, datetime

        return ChildLayerRecord(
            record_id=f"charch:{len(self.facts)}",
            subject_id=fact.subject_id,
            kind="child_archive",
            text=fact.text,
            field=fact.field,
            session_id=None,
            created_at=datetime.now(UTC),
            retention_expires_at=None,
            payload={},
        )


class _FakeActivity:
    def __init__(self) -> None:
        self.touched: list[str] = []

    async def touch(self, *, subject_person_id: str, source: str) -> Any:
        self.touched.append(f"{subject_person_id}:{source}")
        from datetime import UTC, datetime

        return datetime.now(UTC)


def _app(*, memory: bool = True, archive: bool = True) -> tuple[FastAPI, _FakeLayers, _FakeActivity]:
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


@pytest.mark.asyncio
async def test_ingest_writes_summary_and_facts_when_both_consents_present() -> None:
    app, layers, activity = _app(memory=True, archive=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/internal/child-memory/session-recap",
            headers={"X-Memoria-Internal-Token": "archive-write-token-32chars-min!!"},
            json={
                "subject_id": "child-1",
                "resource_owner_id": "parent-1",
                "session_id": "sess-1",
                "source_event_id": "end:sess-1",
                "summary_text": "聊了恐龙和小狗",
                "archive_facts": [{"field": "preference", "text": "喜欢恐龙"}],
                "skipped_reason": None,
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] is False
    assert body["summary_record_id"] == "dlgsum:1"
    assert body["archive_record_ids"] == ["charch:1"]
    assert layers.summaries[0].text == "聊了恐龙和小狗"
    assert layers.facts[0].text == "喜欢恐龙"
    assert activity.touched == ["child-1:dialogue"]


@pytest.mark.asyncio
async def test_ingest_drops_archive_facts_without_child_archive_consent() -> None:
    app, layers, _activity = _app(memory=True, archive=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/internal/child-memory/session-recap",
            headers={"X-Memoria-Internal-Token": "archive-write-token-32chars-min!!"},
            json={
                "subject_id": "child-1",
                "resource_owner_id": "parent-1",
                "session_id": "sess-1",
                "source_event_id": "end:sess-1",
                "summary_text": "聊了恐龙",
                "archive_facts": [{"field": "preference", "text": "喜欢恐龙"}],
            },
        )
    assert response.status_code == 200
    assert response.json()["archive_record_ids"] == []
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
    assert response.json()["skipped"] is True
    assert layers.summaries == []
    assert layers.facts == []
