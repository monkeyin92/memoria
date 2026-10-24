"""The interrupt mark accepts only an event id."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from services.control_api.app.routes.crisis_script_interrupt import router

_TOKEN = "guardian-test-response-plan-token-that-is-long-enough"
_UTTERANCE = "我不会做题。我不想活了"
_INTERRUPTED = "这次的提醒话术孩子没有听完，如需帮助可拨打心理援助热线12356"


class _Settings:
    def internal_token(self, capability: str) -> str:
        assert capability == "response_plan"
        return _TOKEN


class _Store:
    def __init__(self, template_id: str | None) -> None:
        self.template_id = template_id
        self.calls: list[str] = []

    async def mark_script_interrupted(self, *, crisis_event_id: str) -> str | None:
        self.calls.append(crisis_event_id)
        return self.template_id


def _app(store: _Store) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/v1/guardian")
    app.state.settings = _Settings()
    app.state.guardian_store = store
    return app


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_mark_accepts_only_the_event_id_and_rejects_utterance_or_keyword() -> None:
    store = _Store("crisis-self-v1")
    event_id = str(uuid4())
    async with AsyncClient(
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        missing = await client.post(
            "/v1/guardian/crisis-script-interrupted",
            json={"crisis_event_id": event_id},
        )
        assert missing.status_code == 401
        assert store.calls == []

        leaked = await client.post(
            "/v1/guardian/crisis-script-interrupted",
            headers={"X-Memoria-Internal-Token": _TOKEN},
            json={
                "crisis_event_id": event_id,
                "utterance": _UTTERANCE,
                "keyword": "停",
                "detail": _INTERRUPTED,
            },
        )
        assert leaked.status_code == 422
        assert store.calls == []

        marked = await client.post(
            "/v1/guardian/crisis-script-interrupted",
            headers={"X-Memoria-Internal-Token": _TOKEN},
            json={"crisis_event_id": event_id},
        )
        assert marked.status_code == 200
        assert marked.json() == {"crisis_event_id": event_id}
        assert store.calls == [event_id]
        assert _UTTERANCE not in marked.text
        assert "停" not in marked.text
        assert _INTERRUPTED not in marked.text


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_mark_reports_a_missing_event_without_writing() -> None:
    store = _Store(None)
    event_id = str(uuid4())
    async with AsyncClient(
        transport=ASGITransport(app=_app(store)), base_url="http://test"
    ) as client:
        missing = await client.post(
            "/v1/guardian/crisis-script-interrupted",
            headers={"X-Memoria-Internal-Token": _TOKEN},
            json={"crisis_event_id": event_id},
        )
    assert missing.status_code == 404
    assert store.calls == [event_id]
