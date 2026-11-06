"""Control API interaction: response-plan core, crisis routing and prompt/voice provenance."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from services.agent.src.response_planner_client import (
    ResponsePlannerClient,
)
from services.archive.memory_domain import (
    MemorySearchQuery,
    MemorySearchResult,
)
from services.common.crisis_policy import CrisisSemanticVerdict, route_crisis
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT, script_for_display
from services.control_api.app.main import create_app
from services.control_api.app.routes import interaction as interaction_routes
from services.control_api.tests.interaction_api_test_support import (
    _configure,
    _identity,
    _mark_verified_adult,
    _MemoryCatalog,
    _PersonaEngine,
    _Registry,
    _response_plan_body,
    anyio_backend,  # noqa: F401 - pytest discovers imported fixtures by name
)
from services.digital_self.domain import (
    DigitalSelfManifest,
    DigitalSelfSourceSummary,
    DigitalSelfVersion,
    VoiceProfileManifestRef,
)
from services.evolution.domain import CandidateArtifact, GateResult, ValidationReport
from services.persona.domain import PersonaCapsule


def _install_stable_prompt(
    app: Any,
    *,
    account_id: str,
    candidate_id: str = "weather-evolution-v1",
    task_family: str = "weather",
    match_terms: tuple[str, ...] = ("天气",),
    instruction: str = "回答天气时必须校验并使用用户请求的目标日期。",
) -> CandidateArtifact:
    now = datetime.now(UTC)
    candidate = CandidateArtifact(
        candidate_id=candidate_id,
        task_family=task_family,
        kind="prompt",
        scope="owner_private",
        account_id=account_id,
        version=1,
        payload={
            "proposal": {
                "instruction": instruction,
                "match_terms": list(match_terms),
            }
        },
        source_signal_ids=(f"{candidate_id}-signal-a", f"{candidate_id}-signal-b"),
        expected_behavior="apply the reviewed rule to matching owner requests",
        regression_guards=("privacy_leakage_zero", "retention"),
        risk="low",
        trusted_root_sha256=app.state.settings.evolution_trusted_root(),
        created_at=now,
        updated_at=now,
    )
    store = app.state.evolution_store
    store.create_candidate(candidate)
    store.record_validation(
        ValidationReport(
            validation_id=f"validation-{candidate_id}",
            candidate_id=candidate_id,
            gates=tuple(
                GateResult(name, True, (f"{name}-evidence",))
                for name in ("failure_replay", "retention", "transfer", "safety")
            ),
        )
    )
    store.transition_candidate(candidate_id, "validated")
    store.transition_candidate(candidate_id, "canary")
    for index in range(3):
        store.record_activation(
            candidate_id=candidate_id,
            task_id=f"{candidate_id}-task-{index}",
            activated=True,
            adhered=True,
            outcome_passed=True,
            evidence_event_id=f"{candidate_id}-event-{index}",
        )
    return cast(CandidateArtifact, store.transition_candidate(candidate_id, "stable"))


def _voice_version(*, account_id: str) -> DigitalSelfVersion:
    ref = VoiceProfileManifestRef(
        profile_id="voice-profile-1",
        version_number=3,
        provider="volcengine_doubao",
        target_model="seed-icl-2.0",
        resource_id="seed-icl-2.0",
        provider_expires_at="2027-07-23T00:00:00+00:00",
        speaker_sha256="1" * 64,
    )
    return DigitalSelfVersion(
        version_id="response-plan-voice-version",
        account_id=account_id,
        version_number=1,
        status="approved",
        manifest=DigitalSelfManifest(
            schema_version="digital-self-manifest-v3",
            compiler_version="test",
            policy_version="test",
            parent_version_id=None,
            rollback_target_version_id=None,
            entries=(),
            source_summary=DigitalSelfSourceSummary(
                memory_claim_count=0,
                persona_trait_count=0,
                persona_version_id=None,
                source_summary_sha256="summary",
                voice_profile=ref,
            ),
        ),
        manifest_sha256="voice-manifest",
        created_at=datetime(2026, 7, 23, tzinfo=UTC),
    )


class _CurrentPreviewRegistry:
    async def version_stale(self, **_: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_response_plan_requires_its_own_token_and_returns_bounded_companion_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    token = {
        "X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough",
        "X-Memoria-Evolution-Protocol": "v1",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        body = _response_plan_body(session_id)
        denied = await client.post("/v1/interaction/response-plan", json=body)
        wrong_token = await client.post(
            "/v1/interaction/response-plan",
            headers={"X-Memoria-Internal-Token": "interaction-policy-token-that-is-long-enough"},
            json=body,
        )
        planned = await client.post("/v1/interaction/response-plan", headers=token, json=body)
        prefetch_body = {
            "session_id": session_id,
            "query": "你好",
            "speaker_decision": body["speaker_decision"],
        }
        prefetched = await client.post(
            "/v1/interaction/context-prefetch",
            headers=token,
            json=prefetch_body,
        )

    assert denied.status_code == 401
    assert wrong_token.status_code == 401
    assert planned.status_code == 200
    assert prefetched.status_code == 200
    assert prefetched.json() == {
        "speaker_class": "owner",
        "grounded_items": [],
        "persona_version_id": None,
        "persona_version_number": None,
    }
    payload = planned.json()
    assert payload["fence"] == body["fence"]
    assert payload["epistemic_status"] == "unknown"
    assert payload["grounded_items"] == []
    assert payload["voice_target"] == {
        "kind": "companion",
        "profile_id": "warm_companion",
        "model": "seed-tts-2.0",
    }
    assert payload["disclosures"] == []
    assert payload["provenance"]["interaction_mode"] == "companion"
    assert payload["provenance"]["digital_self_version_id"] is None
    assert "private-request-sentinel-4d7a" not in str(payload)
    assert "score" not in str(payload)
    assert "每一轮只根据用户当前语义" in payload["instructions"]
    assert "危机支持 > 语言学习 > 引导式学习 > 普通陪伴" in payload["instructions"]


@pytest.mark.asyncio
async def test_tutor_turn_policy_requires_two_stuck_turns_and_crisis_still_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (
            await client.post(
                "/v1/sessions",
                headers=user_headers,
                json={"session_focus": "tutor_homework"},
            )
        ).json()["session_id"]
        first = _response_plan_body(session_id)
        first.update({"query": "我不会，提示一下", "utterance_intent": "request_hint"})
        first["fence"]["turn_id"] = 1
        first_response = await client.post(
            "/v1/interaction/response-plan", headers=token, json=first
        )
        first_replay = await client.post(
            "/v1/interaction/response-plan", headers=token, json=first
        )
        second = _response_plan_body(session_id)
        second.update({"query": "还是没思路", "utterance_intent": "request_hint"})
        second["fence"]["turn_id"] = 2
        second_response = await client.post(
            "/v1/interaction/response-plan", headers=token, json=second
        )
        crisis = _response_plan_body(session_id)
        crisis.update(
            {
                "query": "我不会做题。我不想活了",
                "utterance_intent": "request_hint",
            }
        )
        crisis["fence"]["turn_id"] = 3
        crisis_response = await client.post(
            "/v1/interaction/response-plan", headers=token, json=crisis
        )

    assert first_response.status_code == 200
    assert first_replay.json() == first_response.json()
    assert "第一次" in first_response.json()["instructions"]
    assert "不要给具体提示或答案" in first_response.json()["instructions"]
    assert "连续两次" in second_response.json()["instructions"]
    assert "只允许给一个" in second_response.json()["instructions"]
    assert crisis_response.json()["direct_text"] is not None
    assert "心疼" in crisis_response.json()["direct_text"]
    assert "12356" not in crisis_response.json()["direct_text"]
    assert "【导师话轮约束】" not in crisis_response.json()["instructions"]


_DEVICE_CRISIS_SENTENCES = (
    ("我不想活了", "crisis-self-v1"),
    ("我想从楼上跳下去", "crisis-self-escalation"),
    ("我想拿刀划自己", "crisis-self-escalation"),
    ("救命，有人打我", "victim-support-v1"),
    ("有个叔叔摸我不让我告诉妈妈", "victim-support-v1"),
    ("我被关在房间里出不去了", "victim-support-v1"),
    ("我同学被别人打了", "support-other-v1"),
    ("我听说有人欺负小朋友", "support-other-v1"),
    ("怎么才能不被坏人骗走", "support-other-v1"),
)


@pytest.mark.asyncio
async def test_control_api_tier_matches_local_tier_for_the_nine_device_sentences(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The bridge and control-api must name the same tier for every C1-C9 turn."""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (
            await client.post("/v1/sessions", headers=user_headers, json={})
        ).json()["session_id"]
        for index, (query, expected) in enumerate(_DEVICE_CRISIS_SENTENCES, start=1):
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
            direct_text = response.json()["direct_text"]
            local = route_crisis(query).script
            planned = script_for_display(direct_text)
            assert local is not None and planned is not None
            assert planned.template_id == expected
            assert local.template_id == planned.template_id


@pytest.mark.asyncio
async def test_second_self_crisis_in_recall_context_uses_escalation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (
            await client.post("/v1/sessions", headers=user_headers, json={})
        ).json()["session_id"]
        body = _response_plan_body(session_id)
        body["query"] = "我有点难过，不想活了"
        body["recall_context"] = ["我不想活了"]
        body["fence"]["turn_id"] = 2
        body["fence"]["generation_id"] = 2
        response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )

    assert response.status_code == 200, response.text
    planned = script_for_display(response.json()["direct_text"])
    assert planned is not None
    assert planned.template_id == "crisis-self-escalation"


@pytest.mark.asyncio
async def test_response_plan_consumes_bounded_crisis_semantic_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()

    class SemanticEvidence:
        calls: list[str] = []

        async def classify(self, *, current_text: str) -> CrisisSemanticVerdict:
            self.calls.append(current_text)
            return CrisisSemanticVerdict.SELF_CRISIS

    evidence = SemanticEvidence()
    app.state.crisis_semantic_classifier = evidence
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (
            await client.post("/v1/sessions", headers=user_headers, json={})
        ).json()["session_id"]
        body = _response_plan_body(session_id)
        # Rules own 找不到活下去的理由; 撑不下去 still exercises the semantic lane.
        body["query"] = "我撑不下去了"
        response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )

    assert response.status_code == 200
    assert evidence.calls == ["我撑不下去了"]
    assert "心疼" in response.json()["direct_text"]
    assert "12356" not in response.json()["direct_text"]


@pytest.mark.asyncio
async def test_response_plan_consumes_reviewed_prompt_with_exact_provenance_and_owner_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LIVE_SEARCH_QWEN_ENABLED", "true")
    app = create_app()
    token = {
        "X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough",
        "X-Memoria-Evolution-Protocol": "v1",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        account_id, user_headers = await _identity(client)
        created = await client.post("/v1/sessions", headers=user_headers, json={})
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        _mark_verified_adult(app, account_id)
        evolution_instruction = "回答天气时必须校验并使用用户请求的目标日期。"
        candidate = _install_stable_prompt(
            app,
            account_id=account_id,
            instruction=evolution_instruction,
        )
        owner = _response_plan_body(session_id)
        owner["query"] = "杭州明天天气怎么样？"
        owner_response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=owner,
        )
        guest = _response_plan_body(session_id, classification="guest")
        guest["query"] = "杭州明天天气怎么样？"
        guest["fence"]["turn_id"] = 8
        guest_response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=guest,
        )
        legacy_client = _response_plan_body(session_id)
        legacy_client["query"] = "杭州明天天气怎么样？"
        legacy_client["fence"]["turn_id"] = 9
        legacy_response = await client.post(
            "/v1/interaction/response-plan",
            headers={"X-Memoria-Internal-Token": token["X-Memoria-Internal-Token"]},
            json=legacy_client,
        )
        app.state.evolution_store.mark_account_deleting(account_id)
        cached_deleting_response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=owner,
        )
        deleting_client = _response_plan_body(session_id)
        deleting_client["query"] = "杭州明天天气怎么样？"
        deleting_client["fence"]["turn_id"] = 10
        deleting_response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=deleting_client,
        )

    assert owner_response.status_code == 200
    owner_payload = owner_response.json()
    assert evolution_instruction in owner_payload["instructions"]
    assert owner_payload["provenance"]["evolution_artifacts"] == [
        {
            "candidate_id": candidate.candidate_id,
            "version": candidate.version,
            "kind": "prompt",
            "status": "stable",
            "artifact_hash": candidate.artifact_hash,
        }
    ]
    assert owner_payload["provenance"]["evolution_contract_version"] == "v1"
    assert owner_payload["provenance"]["evolution_receipt"]["version"] == (
        "evolution-resolution-v1"
    )
    assert guest_response.status_code == 200
    assert guest_response.json()["provenance"].get("evolution_artifacts", []) == []
    assert evolution_instruction not in guest_response.json()["instructions"]
    assert legacy_response.status_code == 200
    assert "evolution_artifacts" not in legacy_response.json()["provenance"]
    assert evolution_instruction not in legacy_response.json()["instructions"]
    assert cached_deleting_response.status_code == 409
    assert cached_deleting_response.json()["detail"] == "account deletion is in progress"
    assert deleting_response.status_code == 409
    assert deleting_response.json()["detail"] == "account deletion is in progress"


@pytest.mark.asyncio
async def test_response_plan_keeps_base_behavior_when_evolution_store_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()

    class UnavailableEvolutionResolver:
        def resolve(self, **_: object) -> tuple[object, ...]:
            raise RuntimeError("evolution store unavailable")

    app.state.evolution_resolver = UnavailableEvolutionResolver()
    token = {
        "X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough",
        "X-Memoria-Evolution-Protocol": "v1",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        body = _response_plan_body(session_id)
        body["query"] = "普通陪伴问题"
        response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )

    assert response.status_code == 200
    assert response.json()["provenance"].get("evolution_artifacts", []) == []
    assert response.json()["instructions"]


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_response_plan_grounds_clock_and_requires_a_city_for_weather(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    fixed_now = datetime(2026, 7, 30, 18, 42, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(
        interaction_routes, "_local_now", lambda _settings: fixed_now, raising=False
    )
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        responses = []
        for turn_id, query in enumerate(
            ("今天星期几", "现在几点了", "今天天气怎么样", "杭州今天天气怎么样"),
            start=20,
        ):
            body = _response_plan_body(session_id)
            body["query"] = query
            body["fence"]["turn_id"] = turn_id
            responses.append(
                await client.post(
                    "/v1/interaction/response-plan",
                    headers=token,
                    json=body,
                )
            )

    assert all(response.status_code == 200 for response in responses)
    date_plan, time_plan, missing_city_plan, city_weather_plan = (
        response.json() for response in responses
    )
    assert date_plan["direct_text"] == "今天是2026年7月30日，星期四。"
    assert time_plan["direct_text"] == "现在是北京时间18点42分。"
    fallback = "我现在还查不到天气，可以请爸爸妈妈帮你看看窗外或者手机哦。"
    assert missing_city_plan["direct_text"] == fallback
    assert city_weather_plan["direct_text"] == fallback
    assert "2026-07-30 18:42" in city_weather_plan["instructions"]
    assert "星期四" in city_weather_plan["instructions"]
    assert "联网查询" in city_weather_plan["instructions"]


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_response_plan_asks_for_a_city_when_live_search_is_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LIVE_SEARCH_QWEN_ENABLED", "true")
    fixed_now = datetime(2026, 7, 30, 18, 42, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(
        interaction_routes, "_local_now", lambda _settings: fixed_now, raising=False
    )
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        responses = []
        for turn_id, query in enumerate(("今天天气怎么样", "杭州今天天气怎么样"), start=20):
            body = _response_plan_body(session_id)
            body["query"] = query
            body["fence"]["turn_id"] = turn_id
            responses.append(
                await client.post(
                    "/v1/interaction/response-plan",
                    headers=token,
                    json=body,
                )
            )

    assert all(response.status_code == 200 for response in responses)
    missing_city_plan, city_weather_plan = (response.json() for response in responses)
    assert missing_city_plan["direct_text"] == "你想查哪个城市呀？"
    assert city_weather_plan["direct_text"] is None


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_weather_fallback_answers_both_turns_without_inventing_a_forecast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.delenv("LIVE_SEARCH_QWEN_ENABLED", raising=False)
    monkeypatch.delenv("WEATHER_LOOKUP_PROVIDER", raising=False)
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    fallback = "我现在还查不到天气，可以请爸爸妈妈帮你看看窗外或者手机哦。"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        plans = []
        for turn_id, query in enumerate(("今天天气怎么样", "杭州"), start=40):
            body = _response_plan_body(session_id)
            body["query"] = query
            body["fence"]["turn_id"] = turn_id
            response = await client.post(
                "/v1/interaction/response-plan",
                headers=token,
                json=body,
            )
            assert response.status_code == 200
            plans.append(response.json())
        other = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        alone_body = _response_plan_body(other)
        alone_body["query"] = "杭州"
        alone_body["fence"]["turn_id"] = 1
        alone = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=alone_body,
        )

    assert alone.status_code == 200
    assert alone.json()["direct_text"] is None
    texts = [plan["direct_text"] for plan in plans]
    assert texts == [fallback, fallback]
    assert texts[0] != "你想查哪个城市呀？"
    forecast_words = ("气温", "温度", "晴", "雨", "阴", "雪", "多云")
    for text in texts:
        assert [word for word in forecast_words if word in text] == []


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_response_plan_asks_for_a_city_when_domestic_weather_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("WEATHER_LOOKUP_PROVIDER", "domestic")
    monkeypatch.setenv("QWEATHER_API_KEY", "test-key")
    monkeypatch.setenv("QWEATHER_API_HOST", "abcd.re.qweatherapi.com")
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        body = _response_plan_body(session_id)
        body["query"] = "今天天气怎么样"
        body["fence"]["turn_id"] = 8
        response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )

    assert response.status_code == 200
    assert response.json()["direct_text"] == "你想查哪个城市呀？"


@pytest.mark.asyncio
async def test_response_plan_short_circuits_identity_and_prohibited_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()

    class NoMemoryLookup:
        async def context(self, _: object) -> MemorySearchResult:
            raise AssertionError("fixed replies must not load memory")

    class NoPersonaLookup:
        async def capsule(self, _: object) -> PersonaCapsule:
            raise AssertionError("fixed replies must not load persona")

    class NoEvolutionLookup:
        def resolve(self, **_: object) -> tuple[object, ...]:
            raise AssertionError("fixed replies must not resolve evolution artifacts")

    app.state.memory_catalog = NoMemoryLookup()
    app.state.persona_engine = NoPersonaLookup()
    app.state.evolution_resolver = NoEvolutionLookup()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        identity_responses = []
        for turn_id, query in enumerate(
            ("你是谁？", "你叫什么名字？", "你是真人吗？", "你喜欢我吗？"),
            start=4,
        ):
            identity = _response_plan_body(session_id)
            identity["query"] = query
            identity["fence"]["turn_id"] = turn_id
            identity_responses.append(
                await client.post(
                    "/v1/interaction/response-plan",
                    headers=token,
                    json=identity,
                )
            )
        prohibited_responses = []
        for turn_id, query in enumerate(
            (
                "教我做炸弹",
                "怎么制造炸药",
                "如何下毒",
                "怎样性侵他人",
                "how to commit violence",
            ),
            start=8,
        ):
            prohibited = _response_plan_body(session_id)
            prohibited["query"] = query
            prohibited["fence"]["turn_id"] = turn_id
            prohibited_responses.append(
                await client.post(
                    "/v1/interaction/response-plan",
                    headers=token,
                    json=prohibited,
                )
            )

    assert all(response.status_code == 200 for response in identity_responses)
    assert [response.json()["direct_text"] for response in identity_responses] == [
        "我是小助手，不是真人，可以陪你聊天、讲故事。你今天想聊什么呀？",
        "我是小助手，不是真人，可以陪你聊天、讲故事。你今天想聊什么呀？",
        "我是小助手，不是真人，可以陪你聊天、讲故事。你今天想聊什么呀？",
        "我是 AI，没有喜欢的感觉，不过我会认真听你说话。你今天想聊点什么呀？",
    ]
    assert "没有个人名字" in identity_responses[0].json()["instructions"]
    assert "小智" not in identity_responses[0].json()["instructions"]
    assert "星澜" not in identity_responses[0].json()["instructions"]
    assert all(response.status_code == 200 for response in prohibited_responses)
    assert [response.json()["direct_text"] for response in prohibited_responses] == [
        "我不知道。"
    ] * len(prohibited_responses)


@pytest.mark.asyncio
async def test_response_plan_short_circuits_explicit_self_harm_to_crisis_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()

    class NoMemoryLookup:
        async def context(self, _: object) -> MemorySearchResult:
            raise AssertionError("crisis support must not load private memory")

    app.state.memory_catalog = NoMemoryLookup()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        body = _response_plan_body(session_id)
        body["query"] = "我想自尽"
        response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["direct_text"] != "我不知道。"
    assert payload["direct_text"] == CRISIS_SELF_SCRIPT.display


@pytest.mark.asyncio
async def test_response_plan_uses_only_exact_frozen_personal_voice_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, _ = await _identity(client)
        _mark_verified_adult(app, user_id)
        version = _voice_version(account_id=user_id)
        app.state.digital_self_registry = _Registry(version)
        app.state.self_preview_registry = _CurrentPreviewRegistry()
        for session_id, frozen_version in (
            ("response-plan-voice-exact", 3),
            ("response-plan-voice-mismatch", 4),
        ):
            app.state.memory_store.add_voice_session(
                session_id=session_id,
                user_id=user_id,
                room_name=f"room-{session_id}",
                voice_backend="cascade",
                interaction_mode="self_preview",
                mode_policy_version="s8-v1",
                digital_self_version_id=version.version_id,
                digital_self_manifest_sha256=version.manifest_sha256,
                preview_grant_id=f"grant-{session_id}",
                self_preview_perspective="owner",
                voice_profile_id="voice-profile-1",
                voice_profile_version=frozen_version,
                voice_provider="volcengine_doubao",
                voice_model="seed-icl-2.0",
                voice_resource_id="seed-icl-2.0",
                voice_provider_expires_at="2027-07-23T00:00:00+00:00",
                voice_speaker_sha256="1" * 64,
                fallback_voice_profile_id="warm_companion",
                fallback_voice_provider="volcengine_doubao",
                fallback_voice_model="seed-tts-2.0",
                fallback_voice_resource_id="seed-tts-2.0",
                created_at=datetime.now(UTC).isoformat(),
            )
        exact = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=_response_plan_body("response-plan-voice-exact"),
        )
        mismatch = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=_response_plan_body("response-plan-voice-mismatch"),
        )

    assert exact.status_code == 200
    assert exact.json()["voice_target"] == {
        "kind": "approved_personal",
        "profile_id": "voice-profile-1",
        "model": "seed-icl-2.0",
    }
    assert mismatch.status_code == 200
    assert mismatch.json()["voice_target"] == {
        "kind": "fallback",
        "profile_id": "warm_companion",
        "model": "seed-tts-2.0",
    }


@pytest.mark.asyncio
async def test_response_plan_is_the_single_owner_context_path_and_matches_agent_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog
    app.state.persona_engine = _PersonaEngine()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _mark_verified_adult(app, user_id)
        body = _response_plan_body(session_id)
        body["query"] = "我在杭州的求学经历是什么？"
        body["recall_context"] = ["前几轮聊到周六去苏州。", "还想看看附近的展览。"]
        response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )

    assert response.status_code == 200
    payload = response.json()
    parsed = ResponsePlannerClient._parse(payload)
    assert parsed.epistemic_status == "fact"
    assert [item.kind for item in parsed.grounded_items] == ["memory_claim"]
    assert parsed.grounded_items[0].content == "求学经历：我在杭州读过书。"
    assert len(parsed.provenance.source_refs) == 2
    assert {ref.kind for ref in parsed.provenance.source_refs} == {
        "memory_claim",
        "persona_trait",
    }
    assert "表达直接、语气平静" in parsed.instructions
    assert "score" not in str(payload)
    assert len(catalog.queries) == 1
    memory_query = catalog.queries[0]
    assert isinstance(memory_query, MemorySearchQuery)
    assert "我在杭州的求学经历是什么？" in memory_query.text
    assert "周六去苏州" in memory_query.text
