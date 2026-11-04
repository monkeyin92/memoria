"""A04: ChildLayer parent_confirmed facts enter dialogue grounded_items."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.memory_domain import MemorySearchResult
from services.control_api.app.main import create_app
from services.control_api.tests.test_interaction_api import (
    _attach_signed_runtime_profile,
    _configure,
    _identity,
    _response_plan_body,
)
from services.guardian.child_archive_review import build_fact_view
from services.guardian.child_memory import KIND_CHILD_ARCHIVE
from services.memory_scope.child_layers import ChildLayerRecord


class _EmptyCatalog:
    def __init__(self) -> None:
        self.queries: list[object] = []

    async def people(self, **_: object) -> tuple[Any, ...]:
        return ()

    async def context(self, query: object) -> MemorySearchResult:
        self.queries.append(query)
        return MemorySearchResult()


class _ConsentGuardian:
    def __init__(self, *, kinds: set[str]) -> None:
        self.kinds = kinds
        self.calls: list[tuple[str, str]] = []

    async def active_consent(self, *, minor_user_id: str, consent_kind: str) -> object | None:
        self.calls.append((minor_user_id, consent_kind))
        if consent_kind in self.kinds:
            return {"consent_kind": consent_kind, "minor_user_id": minor_user_id}
        return None


class _ArchiveLayers:
    def __init__(self, views: list[Any], *, summaries: list[ChildLayerRecord] | None = None) -> None:
        self._views = views
        self._summaries = summaries or []
        self.list_calls: list[dict[str, object]] = []
        self.summary_list_calls = 0

    async def list(
        self,
        *,
        subject_id: str,
        kind: str,
        viewer_subject_id: str,
        limit: int = 50,
    ) -> list[ChildLayerRecord]:
        del viewer_subject_id, limit
        if kind == "dialogue_summary":
            self.summary_list_calls += 1
            return [row for row in self._summaries if row.subject_id == subject_id]
        return []

    async def list_archive_views(
        self,
        *,
        subject_id: str,
        viewer_subject_id: str,
        limit: int = 50,
        include_withdrawn: bool = False,
    ) -> list[Any]:
        self.list_calls.append(
            {
                "subject_id": subject_id,
                "viewer_subject_id": viewer_subject_id,
                "limit": limit,
                "include_withdrawn": include_withdrawn,
            }
        )
        del subject_id
        rows = [view for view in self._views if view.record_id]
        if not include_withdrawn:
            rows = [view for view in rows if view.status != "revoked"]
        return rows[:limit]


def _archive_view(
    *,
    record_id: str,
    text: str,
    status: str,
    reason_code: str,
    provenance: str,
    fact_version: int = 1,
) -> Any:
    return build_fact_view(
        record_id=record_id,
        field="preference",
        text=text,
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
        retention_expires_at=None,
        payload={
            "kind": KIND_CHILD_ARCHIVE,
            "provenance": provenance,
            "source_session_id": "sess-a04",
            "rule_version": "preference-extract-v2",
            "fact_version": fact_version,
            "polarity": "positive",
        },
        status=status,
        reason_code=reason_code,
        handled_at=datetime(2026, 11, 4, tzinfo=UTC),
        confidence=1.0 if status == "confirmed" else 0.75,
    )


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_parent_confirmed_archive_fact_enters_grounded_items(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _EmptyCatalog()
    child_id = "person-child-a04"
    confirmed = _archive_view(
        record_id="charch:confirmed",
        text="喜欢画画",
        status="confirmed",
        reason_code="parent_confirm",
        provenance="parent_confirm",
        fact_version=2,
    )
    pending = _archive_view(
        record_id="charch:pending",
        text="喜欢恐龙",
        status="candidate",
        reason_code="child_archive_auto_extract",
        provenance="auto_extract",
    )
    withdrawn = _archive_view(
        record_id="charch:withdrawn",
        text="旧喜好",
        status="revoked",
        reason_code="parent_withdraw",
        provenance="parent_confirm",
    )
    layers = _ArchiveLayers(
        [confirmed, pending, withdrawn],
        summaries=[
            ChildLayerRecord(
                record_id="sum:1",
                subject_id=child_id,
                kind="dialogue_summary",
                text="今天聊了画画和恐龙",
                field=None,
                session_id="sess-summary",
                created_at=datetime(2026, 11, 4, tzinfo=UTC),
                retention_expires_at=None,
                payload={"kind": "dialogue_summary", "text": "今天聊了画画和恐龙"},
            )
        ],
    )
    app.state.memory_catalog = catalog
    app.state.child_layer_store = layers
    app.state.guardian_store = _ConsentGuardian(kinds={"memory_retention", "child_archive"})
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _attach_signed_runtime_profile(
            app,
            user_id=_user_id,
            session_id=session_id,
            active_subject_id=child_id,
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
            capabilities=("chat", "memory_recall_private"),
        )
        body = _response_plan_body(session_id)
        body["query"] = "我喜欢什么？"
        planned = await client.post("/v1/interaction/response-plan", headers=token, json=body)
        prefetched = await client.post(
            "/v1/interaction/context-prefetch",
            headers=token,
            json={
                "session_id": session_id,
                "query": "我喜欢什么？",
                "speaker_decision": body["speaker_decision"],
            },
        )

    assert planned.status_code == 200, planned.text
    assert prefetched.status_code == 200, prefetched.text
    planned_claims = [
        item for item in planned.json()["grounded_items"] if item["kind"] == "memory_claim"
    ]
    prefetched_claims = [
        item for item in prefetched.json()["grounded_items"] if item["kind"] == "memory_claim"
    ]
    assert planned_claims == [
        {
            "kind": "memory_claim",
            "item_id": "charch:confirmed",
            "content": "喜好：喜欢画画",
            "use_as": "fact",
            "source_event_ids": ["fact_version:2"],
            "confidence": None,
            "sharing_scope": None,
        }
    ]
    assert prefetched_claims == planned_claims
    assert "恐龙" not in planned.text
    assert "今天聊了" not in planned.text
    assert "恐龙" not in prefetched.text
    assert layers.summary_list_calls == 0
    assert layers.list_calls
    assert all(call["viewer_subject_id"] == child_id for call in layers.list_calls)
    assert ("person-child-a04", "memory_retention") in app.state.guardian_store.calls
    assert ("person-child-a04", "child_archive") in app.state.guardian_store.calls


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_without_child_archive_consent_skips_archive_recall(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _EmptyCatalog()
    child_id = f"person-child-{uuid4().hex[:8]}"
    layers = _ArchiveLayers(
        [
            _archive_view(
                record_id="charch:confirmed",
                text="喜欢画画",
                status="confirmed",
                reason_code="parent_confirm",
                provenance="parent_confirm",
            )
        ]
    )
    app.state.memory_catalog = catalog
    app.state.child_layer_store = layers
    app.state.guardian_store = _ConsentGuardian(kinds={"memory_retention"})
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _attach_signed_runtime_profile(
            app,
            user_id=user_id,
            session_id=session_id,
            active_subject_id=child_id,
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
            capabilities=("chat", "memory_recall_private"),
        )
        body = _response_plan_body(session_id)
        body["query"] = "我喜欢什么？"
        response = await client.post("/v1/interaction/response-plan", headers=token, json=body)

    assert response.status_code == 200, response.text
    assert response.json()["grounded_items"] == []
    assert layers.list_calls == []
