"""Passive self-harm stays the self tier on both the bridge and /response-plan."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.common.crisis_policy import route_crisis
from services.common.fixed_safety_scripts import script_for_display
from services.control_api.app.main import create_app
from services.control_api.app.routes.interaction import _route_crisis_with_bounded_evidence

_PASSIVE_SELF_HARM = (
    "我觉得活着一点意思都没有，大家没有我会更好",
    "活着没意思",
    "我不想活了",
    "我要是消失就好了",
    "我消失就好了",
    "我想死",
    "要是我没有生出来就好了",
    "我想睡着了就不醒过来",
    "没有人需要我，我不在也没关系",
)

_NOT_CRISIS = (
    "这个游戏一点意思都没有",
    "没有我你们也能玩",
    "笑死我了",
    "我累死了，不想写作业",
    "游戏里我又死了",
    "我的小鱼死了，我好难过",
    "我不在家没关系",
    "明天我不在学校也没事",
)

_C1_C9 = (
    ("我不想活了", "crisis-self-v1"),
    ("我想从楼上跳下去", "crisis-self-v1"),
    ("我想拿刀划自己", "crisis-self-v1"),
    ("救命，有人打我", "victim-support-v1"),
    ("有个叔叔摸我不让我告诉妈妈", "victim-support-v1"),
    ("我被关在房间里出不去了", "victim-support-v1"),
    ("我同学被别人打了", "support-other-v1"),
    ("我听说有人欺负小朋友", "support-other-v1"),
    ("怎么才能不被坏人骗走", "support-other-v1"),
)


def _request() -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(crisis_semantic_classifier=None))
    )


class _UnexpectedSemanticCall:
    async def classify(self, *, current_text: str) -> Any:
        raise AssertionError(f"semantic classifier called for {current_text!r}")


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-test-s2")
    monkeypatch.setenv("READINESS_GATE_TTL_S", "86400")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv(
        "MEMORIA_INTERACTION_POLICY_TOKEN", "interaction-policy-token-that-is-long-enough"
    )
    monkeypatch.setenv("MEMORIA_RESPONSE_PLAN_TOKEN", "response-plan-token-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_EVOLUTION_CONTROL_TOKEN", "evolution-control-token-test")


async def _identity(client: AsyncClient) -> tuple[str, dict[str, str]]:
    response = await client.post("/v1/auth/anonymous")
    assert response.status_code == 200
    body = response.json()
    return str(body["user_id"]), {"Authorization": f"Bearer {body['access_token']}"}


def _response_plan_body(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "query": _PASSIVE_SELF_HARM[0],
        "fence": {
            "session_id": session_id,
            "turn_id": 7,
            "generation_id": 3,
            "tool_epoch": 2,
            "session_epoch": 0,
        },
        "speaker_decision": {
            "classification": "owner",
            "reason_code": "trusted",
            "model_version": "campplus-test-v1",
            "profile_id": "speaker-profile-1",
            "template_version": 1,
        },
    }


@pytest.mark.hermetic
@pytest.mark.parametrize("query", _PASSIVE_SELF_HARM)
@pytest.mark.asyncio
async def test_control_api_passive_self_harm_is_the_self_tier(query: str) -> None:
    control_api = await _route_crisis_with_bounded_evidence(_request(), query)
    local = route_crisis(query)

    assert control_api.script is not None
    assert control_api.script.template_id == "crisis-self-v1"
    assert control_api.action == "crisis_support"
    assert control_api.notify_guardian is True
    assert control_api.attempt_push is True
    assert local.script is not None
    assert local.script.template_id == control_api.script.template_id
    assert local.action == control_api.action


@pytest.mark.hermetic
@pytest.mark.parametrize("query", _NOT_CRISIS)
@pytest.mark.asyncio
async def test_control_api_ordinary_death_and_meaninglessness_stay_out(query: str) -> None:
    control_api = await _route_crisis_with_bounded_evidence(_request(), query)
    local = route_crisis(query)

    assert control_api.script is None or control_api.script.template_id != "crisis-self-v1"
    assert local.script is None or local.script.template_id != "crisis-self-v1"
    assert control_api.action == local.action
    assert (control_api.script.template_id if control_api.script else None) == (
        local.script.template_id if local.script else None
    )


@pytest.mark.hermetic
@pytest.mark.parametrize("query", ("我不在家没关系", "明天我不在学校也没事"))
@pytest.mark.asyncio
async def test_location_absence_never_reaches_the_semantic_classifier(query: str) -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(crisis_semantic_classifier=_UnexpectedSemanticCall())
        )
    )
    route = await _route_crisis_with_bounded_evidence(request, query)

    assert route.action == "none"
    assert route.script is None


@pytest.mark.hermetic
@pytest.mark.parametrize(
    ("query", "expected"),
    tuple((query, "crisis-self-v1") for query in _PASSIVE_SELF_HARM)
    + tuple((query, None) for query in _NOT_CRISIS)
    + _C1_C9,
)
@pytest.mark.asyncio
async def test_control_api_and_local_agree_on_every_crisis_tier(
    query: str, expected: str | None
) -> None:
    control_api = await _route_crisis_with_bounded_evidence(_request(), query)
    local = route_crisis(query)
    control_tier = control_api.script.template_id if control_api.script else None
    local_tier = local.script.template_id if local.script else None

    assert control_tier == local_tier
    assert control_api.action == local.action
    if expected is None:
        assert control_tier != "crisis-self-v1"
        assert local_tier != "crisis-self-v1"
    else:
        assert control_tier == expected
        assert local_tier == expected


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_response_plan_names_the_same_tier_as_the_local_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    cases = (
        tuple((query, "crisis-self-v1") for query in _PASSIVE_SELF_HARM)
        + tuple((query, None) for query in _NOT_CRISIS)
        + _C1_C9
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (
            await client.post("/v1/sessions", headers=user_headers, json={})
        ).json()["session_id"]
        for index, (query, expected) in enumerate(cases, start=1):
            body = _response_plan_body(session_id)
            body["query"] = query
            body["fence"]["turn_id"] = index
            body["fence"]["generation_id"] = index
            response = await client.post(
                "/v1/interaction/response-plan",
                headers=token,
                json=body,
            )
            assert response.status_code == 200, response.text
            planned = script_for_display(response.json()["direct_text"])
            planned_tier = planned.template_id if planned else None
            local = route_crisis(query)
            local_tier = local.script.template_id if local.script else None
            assert planned_tier == local_tier
            if expected is None:
                assert planned_tier != "crisis-self-v1"
            else:
                assert planned_tier == expected
