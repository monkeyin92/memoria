"""Control API interaction: response-plan subject scope, memory retention and budgets."""

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.response_planner_client import (
    ResponsePlannerClient,
    ResponsePlannerClientConfig,
)
from services.archive.memory_domain import (
    MemorySearchItem,
    MemorySearchQuery,
    MemorySearchResult,
    PersonItem,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT
from services.control_api.app.main import create_app
from services.control_api.app.routes import interaction as interaction_routes
from services.control_api.tests.interaction_api_test_support import (
    _attach_signed_runtime_profile,
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
)
from services.persona.domain import PersonaCapsule, PersonaCapsuleEntry
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


class _SlowMemoryCatalog(_MemoryCatalog):
    def __init__(self, delay_s: float = 2.0) -> None:
        super().__init__()
        self.delay_s = delay_s
        self.context_calls = 0

    async def context(self, query: object) -> MemorySearchResult:
        self.context_calls += 1
        self.queries.append(query)
        await asyncio.sleep(self.delay_s)
        return MemorySearchResult(items=())


class _ChangingMemoryCatalog:
    def __init__(self) -> None:
        self.calls = 0

    async def people(
        self, *, account_id: str, limit: int = 100, subject_id: str | None = None
    ) -> tuple[object, ...]:
        assert subject_id is not None and subject_id.strip()
        del account_id, limit
        return ()

    async def context(self, _: object) -> MemorySearchResult:
        self.calls += 1
        return MemorySearchResult(
            items=(
                MemorySearchItem(
                    item_id=f"memory-snapshot-{self.calls}",
                    kind="claim",
                    title="快照",
                    snippet=f"首次规划时的内容 {self.calls}",
                    category="life_story",
                    status="confirmed",
                    source_event_id=f"event-snapshot-{self.calls}",
                    occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
                    score=0.99,
                ),
            )
        )


class _SlowRecallStore:
    """Session recall that has grown behind a long session and now queues."""

    def __init__(self, inner: Any, delay_s: float = 1.0) -> None:
        self._inner = inner
        self._delay_s = delay_s
        self.calls = 0

    async def append(self, **kwargs: Any) -> None:
        await self._inner.append(**kwargs)

    async def list_recent(self, **kwargs: Any) -> tuple[str, ...]:
        self.calls += 1
        await asyncio.sleep(self._delay_s)
        return cast(tuple[str, ...], await self._inner.list_recent(**kwargs))

    async def erase_subject(self, **kwargs: Any) -> int:
        return cast(int, await self._inner.erase_subject(**kwargs))


class _SlowPersonaEngine:
    def __init__(self, delay_s: float = 2.0) -> None:
        self.delay_s = delay_s
        self.calls = 0

    async def capsule(self, _: object) -> PersonaCapsule:
        self.calls += 1
        await asyncio.sleep(self.delay_s)
        return PersonaCapsule()


class _StyleOnlyPersonaEngine:
    async def capsule(self, _: object) -> PersonaCapsule:
        return PersonaCapsule(
            version_id="persona-version-1",
            version_number=1,
            entries=(
                PersonaCapsuleEntry(
                    trait_id="persona-direct",
                    category="discourse_style",
                    description="表达直接、语气平静",
                    context="",
                    counterexample="",
                    confidence=0.9,
                    source_event_ids=(),
                ),
            ),
        )


def _version(*, account_id: str, status: str) -> DigitalSelfVersion:
    return DigitalSelfVersion(
        version_id="response-plan-version",
        account_id=account_id,
        version_number=1,
        status=status,  # type: ignore[arg-type]
        manifest=DigitalSelfManifest(
            schema_version="digital-self-manifest-v2",
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
            ),
        ),
        manifest_sha256="manifest",
        created_at=datetime(2026, 7, 22, tzinfo=UTC),
    )


def _plan_speaker() -> SpeakerDecision:
    return SpeakerDecision(
        classification="owner",
        score=0.91,
        quality_score=0.88,
        reason_code="trusted",
        model_version="campplus-test-v1",
        template_version=1,
        profile_id="speaker-profile-1",
        permissions=permissions_for_speaker("owner"),
    )


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_slow_companion_memory_still_returns_a_plan_inside_the_client_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _SlowMemoryCatalog(2.0)
    app.state.memory_catalog = catalog
    app.state.persona_engine = _PersonaEngine()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _mark_verified_adult(app, user_id)

    fence = GenerationFence(
        session_id=session_id,
        turn_id=7,
        generation_id=3,
        tool_epoch=2,
        session_epoch=0,
    )
    started = time.perf_counter()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        timeout=5.0,
    ) as http_client:
        planner = ResponsePlannerClient(
            ResponsePlannerClientConfig(
                endpoint="http://test/v1/interaction/response-plan",
                internal_token="response-plan-token-that-is-long-enough",
                timeout_s=0.8,
            ),
            client=http_client,
        )
        fetch = await planner.fetch(
            session_id=session_id,
            query="我找不到我的橡皮了",
            fence=fence,
            speaker_decision=_plan_speaker(),
        )
        await planner.aclose()
    elapsed = time.perf_counter() - started

    assert catalog.context_calls == 1
    assert elapsed < 0.8
    assert fetch.reason == "ok"
    assert fetch.plan is not None
    assert fetch.plan.grounded_items == ()


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_long_session_over_budget_crisis_turn_skips_companion_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _SlowMemoryCatalog(2.0)
    app.state.memory_catalog = catalog
    app.state.persona_engine = _PersonaEngine()
    recall = _SlowRecallStore(app.state.subject_session_recall_store)
    app.state.subject_session_recall_store = recall
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _mark_verified_adult(app, user_id)

    body = _response_plan_body(session_id)
    body["query"] = "我不想活了"
    body["recall_context"] = [f"第{index}轮普通聊天" for index in range(4)]
    body["fence"]["turn_id"] = 9
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    started = time.perf_counter()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/interaction/response-plan", headers=token, json=body)
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 0.8
    assert response.json()["direct_text"] == CRISIS_SELF_SCRIPT.display
    assert catalog.context_calls == 0
    assert recall.calls == 0


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_over_budget_normal_turn_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _SlowMemoryCatalog(2.0)
    app.state.memory_catalog = catalog
    app.state.persona_engine = _PersonaEngine()
    app.state.subject_session_recall_store = _SlowRecallStore(
        app.state.subject_session_recall_store
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _mark_verified_adult(app, user_id)

    body = _response_plan_body(session_id)
    body["query"] = "我找不到我的橡皮了"
    body["recall_context"] = [f"第{index}轮普通聊天" for index in range(4)]
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    started = time.perf_counter()
    with caplog.at_level("WARNING"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/interaction/response-plan", headers=token, json=body)
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 0.8
    assert response.json()["grounded_items"] == []
    assert any("companion context exceeded budget" in record.getMessage() for record in caplog.records)


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_a_slow_persona_keeps_account_memory_that_already_finished(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Persona missing the companion budget must not discard a finished memory read."""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _MemoryCatalog()
    persona = _SlowPersonaEngine()
    app.state.memory_catalog = catalog
    app.state.persona_engine = persona
    app.state.subject_session_recall_store = _SlowRecallStore(
        app.state.subject_session_recall_store
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _mark_verified_adult(app, user_id)

    body = _response_plan_body(session_id)
    body["query"] = "我在杭州读过书。"
    body["recall_context"] = [f"第{index}轮普通聊天" for index in range(4)]
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    started = time.perf_counter()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/interaction/response-plan", headers=token, json=body)
    elapsed = time.perf_counter() - started

    assert response.status_code == 200, response.text
    assert elapsed < 0.8
    assert persona.calls == 1
    assert catalog.queries
    assert any("杭州" in item["content"] for item in response.json()["grounded_items"])


@pytest.mark.asyncio
async def test_response_plan_applies_relative_time_and_confirmed_entity_filters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    now = datetime(2026, 8, 7, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(interaction_routes, "_local_now", lambda _: now)
    app = create_app()
    catalog = _MemoryCatalog()
    catalog.people_items = (
        PersonItem(
            person_id="15c1ea15-8465-4cdf-92a8-90ac860c6aac",
            display_name="李梅",
            relationship_to_owner="mother",
            aliases=("妈妈", "母亲", "李梅"),
            status="confirmed",
            source_event_id="mother-source",
        ),
    )
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
        body["query"] = "昨天妈妈提到的南京旅行"
        response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )

    assert response.status_code == 200
    assert len(catalog.queries) == 1
    memory_query = catalog.queries[0]
    assert isinstance(memory_query, MemorySearchQuery)
    assert memory_query.entity_ids == ("15c1ea15-8465-4cdf-92a8-90ac860c6aac",)
    assert memory_query.occurred_after == datetime(2026, 8, 5, 16, tzinfo=UTC)
    assert memory_query.occurred_before == datetime(
        2026,
        8,
        6,
        15,
        59,
        59,
        999999,
        tzinfo=UTC,
    )
    assert "昨天" not in memory_query.text
    # The confirmed-person entity filter narrows to 妈妈's items; the alias
    # itself stays in the planned text as the lexical bridge to claims whose
    # only surface overlap is the alias (e.g. "我妈妈提到的南京旅行" style
    # values). Stripping it made person-scoped recall return nothing.
    assert "妈妈" in memory_query.text
    assert "南京旅行" in memory_query.text


@pytest.mark.asyncio
async def test_shadow_response_plan_tracks_style_snapshot_without_empty_source_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.persona_engine = _StyleOnlyPersonaEngine()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _mark_verified_adult(app, user_id)
        body = _response_plan_body(session_id, classification="uncertain")
        body["speaker_decision"]["reason_code"] = "shadow_owner_candidate"
        response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )

    assert response.status_code == 200
    provenance = response.json()["provenance"]
    assert provenance["source_refs"] == []
    assert provenance["persona_version_id"] == "persona-version-1"
    assert provenance["persona_version_number"] == 1
    assert provenance["persona_style_only"] is True


@pytest.mark.asyncio
async def test_response_plan_reuses_the_first_snapshot_for_an_identical_full_fence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _ChangingMemoryCatalog()
    app.state.memory_catalog = catalog
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _mark_verified_adult(app, user_id)
        body = _response_plan_body(session_id)
        first = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )
        second = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert catalog.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict_kind", ("query", "speaker"))
async def test_response_plan_rejects_conflicting_input_for_the_same_full_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    conflict_kind: str,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        body = _response_plan_body(session_id)
        first = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )
        conflicting = {
            **body,
            "speaker_decision": dict(body["speaker_decision"]),
        }
        if conflict_kind == "query":
            conflicting["query"] = "同一个 fence 不得改成另一条问题"
        else:
            conflicting["speaker_decision"]["classification"] = "guest"
            conflicting["speaker_decision"]["reason_code"] = "owner_mismatch"
        second = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=conflicting,
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == {"code": "response_plan_conflict"}


@pytest.mark.asyncio
async def test_response_plan_rejects_client_owned_fields_and_mismatched_fences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        mismatched = _response_plan_body(session_id)
        mismatched["fence"] = {
            **mismatched["fence"],
            "session_id": "other-session",
        }
        extra = _response_plan_body(session_id)
        extra["account_id"] = "forged-account"
        extra["speaker_decision"] = {
            **extra["speaker_decision"],
            "score": 0.99,
        }
        fence_response = await client.post(
            "/v1/interaction/response-plan", headers=token, json=mismatched
        )
        extra_response = await client.post(
            "/v1/interaction/response-plan", headers=token, json=extra
        )

    assert fence_response.status_code == 422
    assert extra_response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("classification", ("guest", "uncertain"))
async def test_response_plan_never_grants_companion_private_context_to_non_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, classification: str
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=_response_plan_body(session_id, classification=classification),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["grounded_items"] == []
    assert payload["provenance"]["speaker_class"] == classification
    assert payload["provenance"]["source_refs"] == []
    assert catalog.queries == []


@pytest.mark.asyncio
async def test_minor_without_memory_retention_cannot_read_private_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (
            await client.post("/v1/sessions", headers=user_headers, json={})
        ).json()["session_id"]
        app.state.memory_store.update_subject_profile(
            user_id=user_id,
            subject_category="minor",
            birth_year_band="14_17",
            now=datetime.now(UTC).isoformat(),
        )
        body = _response_plan_body(session_id)
        body["query"] = "我们以前聊过什么？"
        response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=body,
        )

    assert response.status_code == 200
    assert response.json()["grounded_items"] == []
    assert catalog.queries == []


@pytest.mark.asyncio
async def test_response_plan_reads_the_current_subject_and_not_the_account_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A non-owner turn reads the catalog, scoped to that subject.

    The legacy archive stores every first-person claim under subject_key="self"
    inside the login account.  The read still runs, but the query names the
    current subject and the returned rows are that subject's, never the
    account owner's.
    """

    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _MemoryCatalog()
    catalog.items_by_subject = {
        "person-independent-child": (
            MemorySearchItem(
                item_id="memory-child",
                kind="claim",
                title="孩子的经历",
                snippet="我在学校学画画。",
                category="life_story",
                status="confirmed",
                source_event_id="event-memory-child",
                occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
                score=0.99,
            ),
        ),
    }
    app.state.memory_catalog = catalog
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        catalog.items_by_subject[user_id] = (
            MemorySearchItem(
                item_id="memory-owner",
                kind="claim",
                title="账号本人的经历",
                snippet="我在杭州读过书。",
                category="life_story",
                status="confirmed",
                source_event_id="event-memory-owner",
                occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
                score=0.99,
            ),
        )
        created = await client.post("/v1/sessions", headers=user_headers, json={})
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        _attach_signed_runtime_profile(
            app,
            user_id=user_id,
            session_id=session_id,
            active_subject_id="person-independent-child",
            subject_category="adult",
            age_band="adult",
            capabilities=("chat", "memory_recall_private"),
        )
        other_subject = _response_plan_body(session_id)
        other_subject["query"] = "我们以前聊过什么？"
        other_response = await client.post(
            "/v1/interaction/response-plan", headers=token, json=other_subject
        )

        _attach_signed_runtime_profile(
            app,
            user_id=user_id,
            session_id=session_id,
            capabilities=("chat", "memory_recall_private"),
        )
        owner_turn = _response_plan_body(session_id)
        owner_turn["fence"] = {**owner_turn["fence"], "turn_id": 8, "generation_id": 4}
        owner_response = await client.post(
            "/v1/interaction/response-plan", headers=token, json=owner_turn
        )

    assert other_response.status_code == 200, other_response.text
    assert owner_response.status_code == 200, owner_response.text
    assert [query.subject_id for query in catalog.queries] == [
        "person-independent-child",
        user_id,
    ]
    child_claims = [
        item
        for item in other_response.json()["grounded_items"]
        if item["kind"] == "memory_claim"
    ]
    owner_claims = [
        item
        for item in owner_response.json()["grounded_items"]
        if item["kind"] == "memory_claim"
    ]
    assert [item["item_id"] for item in child_claims] == ["memory-child"]
    assert "杭州" not in other_response.text
    assert [item["item_id"] for item in owner_claims] == ["memory-owner"]


@pytest.mark.asyncio
async def test_companion_memory_queries_carry_the_resolved_subject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Both Agent seams query the subject the turn was resolved for.

    ``_resolve_subject_memory_scope`` decides the subject once, and both
    /response-plan and /context-prefetch must spend that answer: the query used
    to name the login account as its subject, so the resolver's result never
    reached the catalog and the two could drift apart.
    """

    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        created = await client.post("/v1/sessions", headers=user_headers, json={})
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        _attach_signed_runtime_profile(app, user_id=user_id, session_id=session_id)
        body = _response_plan_body(session_id)
        body["query"] = "我们以前聊过什么？"
        planned = await client.post("/v1/interaction/response-plan", headers=token, json=body)
        prefetched = await client.post(
            "/v1/interaction/context-prefetch",
            headers=token,
            json={
                "session_id": session_id,
                "query": "我们以前聊过什么？",
                "speaker_decision": body["speaker_decision"],
            },
        )

    assert planned.status_code == 200, planned.text
    assert planned.json()["grounded_items"]
    assert prefetched.status_code == 200, prefetched.text
    assert prefetched.json()["grounded_items"]
    assert len(catalog.queries) == 2
    for recorded in catalog.queries:
        assert isinstance(recorded, MemorySearchQuery)
        assert recorded.account_id == user_id
        assert recorded.subject_id == user_id
        assert recorded.include_candidates is False


@pytest.mark.asyncio
async def test_companion_seams_read_the_member_subject_and_not_the_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Both Agent seams query the member, and neither returns the owner's rows.

    The catalog is reached.  Every people() and context() call names the
    current subject, and a catalog that only has the account owner's row
    contributes nothing to the member's plan.
    """

    class _RecordingCatalog:
        def __init__(self) -> None:
            self.people_subjects: list[str | None] = []
            self.context_subjects: list[str | None] = []

        async def people(
            self, *, account_id: str, limit: int = 100, subject_id: str | None = None
        ) -> tuple[PersonItem, ...]:
            del account_id, limit
            self.people_subjects.append(subject_id)
            return ()

        async def context(self, query: object) -> MemorySearchResult:
            self.context_subjects.append(getattr(query, "subject_id", None))
            # The owner's row is present in the account.  A query that forgot
            # the subject (subject_id is None) would return it; a member query
            # must not.
            if getattr(query, "subject_id", None) is None:
                return MemorySearchResult(
                    items=(
                        MemorySearchItem(
                            item_id="memory-owner",
                            kind="claim",
                            title="账号本人",
                            snippet="我在杭州读过书。",
                            category="life_story",
                            status="confirmed",
                            source_event_id="event-owner",
                            occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
                            score=0.99,
                        ),
                    )
                )
            return MemorySearchResult()

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
            active_subject_id="person-independent-child",
            subject_category="adult",
            age_band="adult",
            capabilities=("chat", "memory_recall_private"),
        )
        body = _response_plan_body(session_id)
        body["query"] = "我们以前聊过什么？"
        planned = await client.post("/v1/interaction/response-plan", headers=token, json=body)
        prefetched = await client.post(
            "/v1/interaction/context-prefetch",
            headers=token,
            json={
                "session_id": session_id,
                "query": "我们以前聊过什么？",
                "speaker_decision": body["speaker_decision"],
            },
        )

    assert planned.status_code == 200, planned.text
    assert prefetched.status_code == 200, prefetched.text
    assert catalog.people_subjects == [
        "person-independent-child",
        "person-independent-child",
    ]
    assert catalog.context_subjects == [
        "person-independent-child",
        "person-independent-child",
    ]
    assert all(subject_id != user_id for subject_id in catalog.context_subjects)
    planned_claims = [
        item for item in planned.json()["grounded_items"] if item["kind"] == "memory_claim"
    ]
    prefetched_claims = [
        item
        for item in prefetched.json()["grounded_items"]
        if item["kind"] == "memory_claim"
    ]
    assert planned_claims == []
    assert prefetched_claims == []
    assert "杭州" not in planned.text
    assert "杭州" not in prefetched.text


@pytest.mark.asyncio
async def test_response_plan_looks_up_retention_consent_for_the_active_subject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Category and consent must be read from the same person.

    A minor's retention consent lives on the minor's own person id, so looking
    it up under the login account both misses real consent and answers about
    the wrong person.
    """

    class _RecordingGuardianStore:
        def __init__(self) -> None:
            self.minor_ids: list[str] = []

        async def active_consent(self, *, minor_user_id: str, consent_kind: str) -> None:
            del consent_kind
            self.minor_ids.append(minor_user_id)
            return None

    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog
    guardian = _RecordingGuardianStore()
    app.state.guardian_store = guardian
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
            active_subject_id="person-independent-child",
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
            capabilities=("chat", "tutor", "english_practice"),
        )
        body = _response_plan_body(session_id)
        body["query"] = "我们以前聊过什么？"
        response = await client.post(
            "/v1/interaction/response-plan", headers=token, json=body
        )

    assert response.status_code == 200, response.text
    assert guardian.minor_ids == ["person-independent-child"]
    assert catalog.queries == []
    assert catalog.people_calls == []
    assert response.json()["grounded_items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ("raises", "blank", "unknown", "missing"),
)
async def test_unresolved_subject_returns_empty_items_without_a_catalog_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    """A subject the authority cannot name is not the login account.

    The resolver used to start from account_id and keep that value when the
    profile was missing, blank or unknown.  That queried the owner's rows.
    Each of these failures returns empty items and never calls the catalog.
    """

    class _CountingCatalog:
        def __init__(self) -> None:
            self.calls = 0

        async def people(self, **_: object) -> tuple[PersonItem, ...]:
            self.calls += 1
            return ()

        async def context(self, _: object) -> MemorySearchResult:
            self.calls += 1
            return MemorySearchResult()

    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _CountingCatalog()
    app.state.memory_catalog = catalog
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        created = await client.post("/v1/sessions", headers=user_headers, json={})
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        if failure == "raises":

            class _BrokenAuthority:
                async def current(self, **_: object) -> dict[str, object]:
                    raise RuntimeError("runtime profile unavailable")

            app.state.session_runtime_service = _BrokenAuthority()
        elif failure == "missing":
            # The shared helper replaces None with the login account, and other
            # tests depend on that fallback.  This case needs a profile that
            # really has no active subject, so it is built here instead.
            _attach_signed_runtime_profile(
                app,
                user_id=user_id,
                session_id=session_id,
                unknown_subject=True,
                capabilities=("chat", "memory_recall_private"),
            )
        else:
            active_subject_id = {"blank": "  ", "unknown": "unknown"}[failure]
            _attach_signed_runtime_profile(
                app,
                user_id=user_id,
                session_id=session_id,
                active_subject_id=active_subject_id,
                subject_category="adult",
                age_band="adult",
                capabilities=("chat", "memory_recall_private"),
            )
        body = _response_plan_body(session_id)
        body["query"] = "我们以前聊过什么？"
        planned = await client.post("/v1/interaction/response-plan", headers=token, json=body)
        prefetched = await client.post(
            "/v1/interaction/context-prefetch",
            headers=token,
            json={
                "session_id": session_id,
                "query": "我们以前聊过什么？",
                "speaker_decision": body["speaker_decision"],
            },
        )

    assert planned.status_code == 200, planned.text
    assert prefetched.status_code == 200, prefetched.text
    assert [
        item for item in planned.json()["grounded_items"] if item["kind"] == "memory_claim"
    ] == []
    assert [
        item
        for item in prefetched.json()["grounded_items"]
        if item["kind"] == "memory_claim"
    ] == []
    assert catalog.calls == 0


@pytest.mark.asyncio
async def test_switching_subject_does_not_reuse_the_previous_response_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The cache key includes the subject, not only the generation fence.

    Replaying the same fence after the device switches person must not return
    the previous subject's grounded items.
    """

    _configure(monkeypatch, tmp_path)
    app = create_app()
    catalog = _MemoryCatalog()
    catalog.items_by_subject = {
        "person-independent-child": (
            MemorySearchItem(
                item_id="memory-child",
                kind="claim",
                title="孩子的经历",
                snippet="我在学校学画画。",
                category="life_story",
                status="confirmed",
                source_event_id="event-memory-child",
                occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
                score=0.99,
            ),
        ),
    }
    app.state.memory_catalog = catalog
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        catalog.items_by_subject[user_id] = (
            MemorySearchItem(
                item_id="memory-owner",
                kind="claim",
                title="账号本人的经历",
                snippet="我在杭州读过书。",
                category="life_story",
                status="confirmed",
                source_event_id="event-memory-owner",
                occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
                score=0.99,
            ),
        )
        created = await client.post("/v1/sessions", headers=user_headers, json={})
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        _attach_signed_runtime_profile(
            app,
            user_id=user_id,
            session_id=session_id,
            capabilities=("chat", "memory_recall_private"),
        )
        body = _response_plan_body(session_id)
        body["query"] = "我们以前聊过什么？"
        owner_plan = await client.post("/v1/interaction/response-plan", headers=token, json=body)
        _attach_signed_runtime_profile(
            app,
            user_id=user_id,
            session_id=session_id,
            active_subject_id="person-independent-child",
            subject_category="adult",
            age_band="adult",
            capabilities=("chat", "memory_recall_private"),
        )
        child_plan = await client.post("/v1/interaction/response-plan", headers=token, json=body)

    assert owner_plan.status_code == 200, owner_plan.text
    assert child_plan.status_code == 200, child_plan.text
    owner_claims = [
        item["item_id"]
        for item in owner_plan.json()["grounded_items"]
        if item["kind"] == "memory_claim"
    ]
    child_claims = [
        item["item_id"]
        for item in child_plan.json()["grounded_items"]
        if item["kind"] == "memory_claim"
    ]
    assert owner_claims == ["memory-owner"]
    assert child_claims == ["memory-child"]
    assert "杭州" not in child_plan.text
    assert [query.subject_id for query in catalog.queries] == [
        user_id,
        "person-independent-child",
    ]


@pytest.mark.asyncio
async def test_response_plan_rejects_deleted_sessions_and_accounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        first_session_id = (
            await client.post("/v1/sessions", headers=user_headers, json={})
        ).json()["session_id"]
        app.state.memory_store.mark_voice_sessions_deleting(
            user_id=user_id, deleted_at=datetime.now(UTC).isoformat()
        )
        deleted_session = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=_response_plan_body(first_session_id),
        )
        second_session_id = (
            await client.post("/v1/sessions", headers=user_headers, json={})
        ).json()["session_id"]
        app.state.memory_store.begin_account_deletion(
            user_id=user_id, started_at=datetime.now(UTC).isoformat()
        )
        deleting_account = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=_response_plan_body(second_session_id),
        )

    assert deleted_session.status_code == 410
    assert deleting_account.status_code == 410


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "version_account", "version_status", "relationship_profile_id"),
    (
        ("self_preview", "same", "draft", None),
        ("self_preview", "other", "approved", None),
        ("legacy", "same", "frozen", "missing-relationship"),
    ),
)
async def test_response_plan_fails_closed_for_invalid_frozen_digital_self_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    version_account: str,
    version_status: str,
    relationship_profile_id: str | None,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, _ = await _identity(client)
        _mark_verified_adult(app, user_id)
        version = _version(
            account_id=user_id if version_account == "same" else "other-account",
            status=version_status,
        )
        app.state.digital_self_registry = _Registry(version)
        session_id = f"response-plan-{mode}-{version_account}"
        app.state.memory_store.add_voice_session(
            session_id=session_id,
            user_id=user_id,
            room_name=f"room-{session_id}",
            voice_backend="cascade",
            interaction_mode=mode,
            mode_policy_version="s2-v1",
            digital_self_version_id=version.version_id,
            relationship_profile_id=relationship_profile_id,
            legacy_grant_id="server-grant" if mode == "legacy" else None,
            companion_style_id=None,
            companion_style_version=None,
            created_at=datetime.now(UTC).isoformat(),
        )
        response = await client.post(
            "/v1/interaction/response-plan",
            headers=token,
            json=_response_plan_body(session_id),
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "response_plan_unavailable"}
