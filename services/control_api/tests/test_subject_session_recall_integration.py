"""Interaction seam reads subject-keyed session recall from memory_scope."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.memory_domain import MemorySearchQuery, MemorySearchResult
from services.control_api.app.main import create_app
from services.control_api.tests.test_interaction_api import (
    _attach_signed_runtime_profile,
    _configure,
    _identity,
    _mark_verified_adult,
    _response_plan_body,
)


class _RecordingCatalog:
    def __init__(self) -> None:
        self.queries: list[MemorySearchQuery] = []

    async def people(
        self, *, account_id: str, limit: int = 100, subject_id: str | None = None
    ) -> tuple[Any, ...]:
        del account_id, limit, subject_id
        return ()

    async def context(self, query: object) -> MemorySearchResult:
        assert isinstance(query, MemorySearchQuery)
        self.queries.append(query)
        return MemorySearchResult(items=())


@pytest.mark.asyncio
async def test_response_plan_merges_authoritative_session_recall_by_subject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _RecordingCatalog()
    app.state.memory_catalog = catalog
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        created = await client.post("/v1/sessions", headers=user_headers, json={})
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        _mark_verified_adult(app, user_id)
        _attach_signed_runtime_profile(app, user_id=user_id, session_id=session_id)
        store = app.state.subject_session_recall_store
        await store.append(
            subject_id=user_id,
            session_id=session_id,
            session_epoch=1,
            turn_id=3,
            text="前几轮聊到周六去苏州。",
            actor_subject_id=user_id,
        )
        body = _response_plan_body(session_id)
        body["query"] = "我们以前聊过什么？"
        body["recall_context"] = []
        planned = await client.post("/v1/interaction/response-plan", headers=token, json=body)

    assert planned.status_code == 200, planned.text
    assert len(catalog.queries) == 1
    assert "苏州" in catalog.queries[0].text


@pytest.mark.asyncio
async def test_response_plan_drops_session_recall_when_retention_revoked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _RecordingCatalog()
    app.state.memory_catalog = catalog
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        created = await client.post("/v1/sessions", headers=user_headers, json={})
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        _attach_signed_runtime_profile(
            app,
            user_id=user_id,
            session_id=session_id,
            active_subject_id="person-child",
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
            capabilities=("chat",),
        )
        store = app.state.subject_session_recall_store
        await store.append(
            subject_id="person-child",
            session_id=session_id,
            session_epoch=1,
            turn_id=1,
            text="不应进入召回",
            actor_subject_id="person-child",
        )
        body = _response_plan_body(session_id)
        body["query"] = "我们以前聊过什么？"
        planned = await client.post("/v1/interaction/response-plan", headers=token, json=body)

    assert planned.status_code == 200, planned.text
    assert catalog.queries == []
