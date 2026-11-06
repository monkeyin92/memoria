"""Control API archive: session event ingest, provenance and subject visibility."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import (
    ContextQuery,
    EvidenceEvent,
    SpeakerClass,
)
from services.common.companions import designed_voice_speaker_sha256
from services.common.redaction import redact_pii
from services.control_api.app.main import create_app
from services.control_api.app.persona_learning import observe_persona
from services.control_api.app.routes.archive import (
    _account_write,
)
from services.control_api.tests.archive_api_test_support import (
    PersonaObservationStub,
    _configure,
    _register_verified_adult,
    _response_provenance,
)
from services.digital_self.response_planner import PLANNER_POLICY_VERSION
from services.evolution.domain import CandidateArtifact, GateResult, ValidationReport


@pytest.mark.asyncio
async def test_account_can_append_and_read_an_idempotent_evidence_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    occurred_at = datetime(2026, 7, 19, 8, 0, tzinfo=UTC).isoformat()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register_verified_adult(
            client,
            app,
            username="idempotent-evidence-owner",
        )
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        event = {
            "event_id": "event-api-001",
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": occurred_at,
            "speaker_class": "owner",
            "source": "h5.authoritative_transcript",
            "active_subject_id": identity["user_id"],
            "payload": {"text": "我在杭州读过书。"},
        }
        internal_headers = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        first = await client.post(
            "/v1/archive/session-events", headers=internal_headers, json=event
        )
        duplicate = await client.post(
            "/v1/archive/session-events", headers=internal_headers, json=event
        )
        timeline = await client.get("/v1/archive/timeline", headers=headers)

    assert first.status_code == 201
    assert first.json()["duplicate"] is False
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert [item["event_id"] for item in timeline.json()["items"]] == ["event-api-001"]


@pytest.mark.asyncio
async def test_agent_memory_write_fence_is_accepted_and_canonicalized_for_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The production Agent envelope must not be rejected as unknown input.

    The archive boundary keeps the outer claims out of the public evidence
    model and emits one server-owned projection candidate.  The PostgreSQL
    projector independently revalidates this candidate against Session and
    Policy authority before it can become capture evidence.
    """

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register_verified_adult(
            client,
            app,
            username="archive-fence-adult",
        )
        bearer = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=bearer, json={})).json()
        event = {
            "event_id": "agent-memory-fence-001",
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": datetime.now(UTC).isoformat(),
            "speaker_class": "owner",
            "source": "funasr.authoritative_final",
            "turn_id": 3,
            "generation_id": 4,
            "tool_epoch": 2,
            "session_epoch": 1,
            "device_id": "device-1",
            "subject_revision": 7,
            "active_subject_id": identity["user_id"],
            "runtime_profile_id": "profile-1",
            "actor_id": identity["user_id"],
            "binding_id": "binding-1",
            "binding_version": 2,
            "memory_scope": "personal_private",
            "policy_receipt_id": None,
            "raw_audio_receipt_id": None,
            "training_receipt_id": None,
            "no_model_training": True,
            "event_sequence": 9,
            "payload": {"text": "请记住我喜欢杭州。"},
        }
        response = await client.post(
            "/v1/archive/session-events",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json=event,
        )

    archived = await app.state.life_archive.event(
        account_id=identity["user_id"],
        event_id=event["event_id"],
    )
    assert response.status_code == 201
    assert archived is not None
    # P2-03: 说话主体落在证据行上（此前只当 fence 输入被丢弃），
    # 这是后续按主体围栏与按说话主体 retention 的唯一输入。
    assert archived.subject_id == identity["user_id"]
    candidate = archived.payload["memory_capture_candidate_v1"]
    assert candidate == {
        "active_subject_id": identity["user_id"],
        "actor_id": identity["user_id"],
        "binding_id": "binding-1",
        "binding_version": 2,
        "device_id": "device-1",
        "event_sequence": 9,
        "generation_id": 4,
        "memory_scope": "personal_private",
        "runtime_profile_id": "profile-1",
        "session_id": session["session_id"],
        "session_epoch": 1,
        "subject_revision": 7,
        "tool_epoch": 2,
        "turn_id": 3,
    }


@pytest.mark.asyncio
async def test_canonical_session_pair_keeps_actual_heard_as_delivery_telemetry_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {
        "X-Memoria-Internal-Token": "test-internal-archive-token",
        "X-Memoria-Evolution-Protocol": "v1",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "evolution-owner", "password": "safe-password"},
            )
        ).json()
        bearer = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=bearer, json={})).json()
        occurred_at = datetime.now(UTC).isoformat()
        user_event = {
            "event_id": "evolution-user-event",
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": occurred_at,
            "speaker_class": "owner",
            "source": "funasr.authoritative_final",
            "turn_id": 1,
            "generation_id": 1,
            "tool_epoch": 0,
            "payload": {"text": "明天南京天气如何？", "speaker_reason_code": "formal_owner"},
        }
        assistant_event = {
            "event_id": "evolution-assistant-event",
            "session_id": session["session_id"],
            "event_type": "assistant.playout_stopped",
            "occurred_at": occurred_at,
            "speaker_class": "assistant",
            "source": "generation_fence.actual_heard",
            "turn_id": 1,
            "generation_id": 1,
            "tool_epoch": 0,
            "payload": {
                "text": "明天有雨。",
                "actual_heard": True,
                "evolution_observation": {
                    "task_family": "weather",
                    "task_completed": True,
                    "quality_dimensions": {"factuality": "pass"},
                },
            },
        }
        user = await client.post(
            "/v1/archive/session-events", headers=internal, json=user_event
        )
        assistant = await client.post(
            "/v1/archive/session-events", headers=internal, json=assistant_event
        )
        duplicate = await client.post(
            "/v1/archive/session-events", headers=internal, json=assistant_event
        )

    signals = app.state.evolution_store.list_signals()
    assert (user.status_code, assistant.status_code, duplicate.status_code) == (201, 201, 200)
    # A completed playout is only delivery evidence.  It must not become a
    # task-success or failure signal until an offline evaluator replays the
    # canonical pair through the separate evolution endpoint/worker.
    assert signals == ()
    stored_assistant = await app.state.life_archive.event(
        account_id=identity["user_id"],
        event_id="evolution-assistant-event",
    )
    assert stored_assistant is not None
    assert "evolution_observation" not in stored_assistant.payload


@pytest.mark.asyncio
async def test_archive_accepts_only_the_exact_runtime_evolution_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LIVE_SEARCH_QWEN_ENABLED", "true")
    app = create_app()
    internal = {
        "X-Memoria-Internal-Token": "test-internal-archive-token",
        "X-Memoria-Evolution-Protocol": "v1",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register_verified_adult(
            client,
            app,
            username="evolution-provenance-owner",
        )
        bearer = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=bearer, json={})).json()
        now = datetime.now(UTC)
        candidate = CandidateArtifact(
            candidate_id="archive-evolution-v1",
            task_family="weather",
            kind="prompt",
            scope="owner_private",
            account_id=identity["user_id"],
            version=1,
            payload={
                "proposal": {
                    "instruction": "回答天气时使用用户请求的目标日期。",
                    "match_terms": ["天气"],
                }
            },
            source_signal_ids=("archive-failure-a", "archive-failure-b"),
            expected_behavior="use target date",
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
                validation_id="archive-evolution-validation",
                candidate_id=candidate.candidate_id,
                gates=tuple(
                    GateResult(name, True, (name,))
                    for name in ("failure_replay", "retention", "transfer", "safety")
                ),
            )
        )
        store.transition_candidate(candidate.candidate_id, "validated")
        store.transition_candidate(candidate.candidate_id, "canary")
        for index in range(3):
            store.record_activation(
                candidate_id=candidate.candidate_id,
                task_id=f"archive-canary-{index}",
                activated=True,
                adhered=True,
                outcome_passed=True,
                evidence_event_id=f"archive-canary-event-{index}",
            )
        candidate = store.transition_candidate(candidate.candidate_id, "stable")
        fence = {
            "session_id": session["session_id"],
            "turn_id": 40,
            "generation_id": 3,
            "tool_epoch": 1,
        }
        # The response-plan fence stays turn-scoped; only the archive ingress
        # carries the confirmed speaker subject.
        subject_claim = {"active_subject_id": identity["user_id"]}
        query = "我的手机号是13812345678，南京明天天气如何？"
        archived_query = redact_pii(query)
        plan = await client.post(
            "/v1/interaction/response-plan",
            headers=internal,
            json={
                "session_id": session["session_id"],
                "query": query,
                "fence": fence,
                "speaker_decision": {
                    "classification": "owner",
                    "reason_code": "formal_owner",
                    "model_version": "campplus-test",
                    "profile_id": "owner-profile",
                    "template_version": 1,
                },
            },
        )
        # Archive validation is bound to the generation snapshot. A reviewed
        # candidate can be retired while the already planned audio is playing.
        store.transition_candidate(candidate.candidate_id, "retired")
        user = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "archive-evolution-user",
                **fence,
                **subject_claim,
                "event_type": "speech.utterance_finalized",
                "occurred_at": now.isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "payload": {
                    "text": archived_query,
                    "speaker_reason_code": "formal_owner",
                    "speaker_model_version": "campplus-test",
                    "speaker_profile_id": "owner-profile",
                    "speaker_template_version": 1,
                },
            },
        )
        assert plan.status_code == 200
        assert user.status_code == 201
        plan_payload = plan.json()
        provenance = {
            **plan_payload["provenance"],
            "fence": fence,
            "llm_provider": "qwen",
            "llm_model": "qwen-plus",
            "tts_provider": None,
            "tts_model": None,
        }
        forged_provenance = dict(provenance)
        forged_provenance["evolution_artifacts"] = [
            {
                **provenance["evolution_artifacts"][0],
                "artifact_hash": "0" * 64,
            }
        ]
        assistant_base = {
            **fence,
            **subject_claim,
            "event_type": "assistant.playout_stopped",
            "occurred_at": now.isoformat(),
            "speaker_class": "assistant",
            "source": "generation_fence.actual_heard",
        }
        forged = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "archive-evolution-forged",
                **assistant_base,
                "payload": {
                    "text": "明天有雨。",
                    "actual_heard": True,
                    "response_provenance": forged_provenance,
                },
            },
        )
        accepted = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "archive-evolution-assistant",
                **assistant_base,
                "payload": {
                    "text": "明天有雨。",
                    "actual_heard": True,
                    "response_provenance": provenance,
                },
            },
        )

    assert plan_payload["provenance"]["evolution_artifacts"] == [
        {
            "candidate_id": candidate.candidate_id,
            "version": candidate.version,
            "kind": "prompt",
            "status": "stable",
            "artifact_hash": candidate.artifact_hash,
        }
    ]
    assert plan_payload["provenance"]["evolution_receipt"]["version"] == (
        "evolution-resolution-v1"
    )
    assert forged.status_code == 409
    assert forged.json()["detail"] == {"code": "response_provenance_evolution_invalid"}
    assert accepted.status_code == 201
    stored = await app.state.life_archive.event(
        account_id=identity["user_id"],
        event_id="archive-evolution-assistant",
    )
    assert stored is not None
    assert stored.payload["response_provenance"]["evolution_artifacts"] == plan_payload[
        "provenance"
    ]["evolution_artifacts"]
    assert stored.payload["response_provenance"]["evolution_receipt"] == plan_payload[
        "provenance"
    ]["evolution_receipt"]


@pytest.mark.asyncio
async def test_session_prompt_kind_accepts_valid_internal_value_and_defaults_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register_verified_adult(
            client,
            app,
            username="prompt-kind-owner",
        )
        bearer = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=bearer, json={})).json()
        base = {
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": datetime.now(UTC).isoformat(),
            "speaker_class": "owner",
            "source": "test",
            "active_subject_id": identity["user_id"],
        }
        await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                **base,
                "event_id": "prompt-leading",
                "turn_id": 1,
                "generation_id": 1,
                "payload": {"text": "是的", "prompt_kind": "leading"},
            },
        )
        await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                **base,
                "event_id": "prompt-invalid",
                "turn_id": 2,
                "generation_id": 2,
                "payload": {"text": "另外", "prompt_kind": "client-strong"},
            },
        )
        timeline = await client.get("/v1/archive/timeline", headers=bearer)

    kinds = {item["event_id"]: item["payload"]["prompt_kind"] for item in timeline.json()["items"]}
    assert kinds == {"prompt-invalid": "spontaneous", "prompt-leading": "leading"}


@pytest.mark.asyncio
async def test_explicit_memory_intent_is_server_owned_and_owner_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register_verified_adult(
            client,
            app,
            username="explicit-memory-owner",
        )
        bearer = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=bearer, json={})).json()
        base = {
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": datetime.now(UTC).isoformat(),
            "source": "test",
            "active_subject_id": identity["user_id"],
        }
        cases = (
            ("explicit-owner", "owner", "请记住我喜欢雨天散步。", 1, 0),
            ("spoofed-ordinary", "owner", "今天天气不错。", 2, 0),
            ("explicit-guest", "guest", "请记住我喜欢咖啡。", 3, 0),
            ("quoted-owner", "owner", "朋友说请记住他喜欢咖啡。", 4, 0),
            ("question-owner", "owner", "请记住我喜欢咖啡吗？", 5, 0),
            ("missing-fence-owner", "owner", "请记住我喜欢咖啡。", 6, None),
        )
        for event_id, speaker_class, text, turn_id, tool_epoch in cases:
            response = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    **base,
                    "event_id": event_id,
                    "turn_id": turn_id,
                    "generation_id": turn_id,
                    "tool_epoch": tool_epoch,
                    "speaker_class": speaker_class,
                    "payload": {
                        "text": text,
                        "memory_write_intent": {
                            "kind": "client_spoof",
                            "policy_version": "untrusted",
                        },
                    },
                },
            )
            assert response.status_code == 201
        archived = {
            event_id: await app.state.life_archive.event(
                account_id=identity["user_id"],
                event_id=event_id,
            )
            for event_id, *_ in cases
        }

    assert all(event is not None for event in archived.values())
    payloads = {
        event_id: dict(event.payload) for event_id, event in archived.items() if event is not None
    }
    assert payloads["explicit-owner"]["memory_write_intent"] == {
        "kind": "explicit_remember",
        "policy_version": "explicit-memory-v2",
    }
    assert "memory_write_intent" not in payloads["spoofed-ordinary"]
    assert "memory_write_intent" not in payloads["explicit-guest"]
    assert "memory_write_intent" not in payloads["quoted-owner"]
    assert "memory_write_intent" not in payloads["question-owner"]
    assert "memory_write_intent" not in payloads["missing-fence-owner"]


@pytest.mark.asyncio
async def test_generic_speech_event_requires_a_session_and_cannot_trigger_persona(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "generic-speech-persona", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        response = await client.post(
            "/v1/archive/events",
            headers=internal,
            json={
                "event_id": "generic-speech-persona",
                "account_id": identity["user_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "payload": {
                    "text": "我会先听完，再认真回答。",
                    "persona_eligible": True,
                    "speech_ms": 8000,
                    "pause_ratio": 0.55,
                    "quality_score": 0.95,
                },
            },
        )
        traits = await client.get("/v1/persona/traits?include_candidates=true", headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "session_bound_event_required"
    assert traits.json() == {"items": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    [
        "owner.action_recorded",
        "learning.task_created",
        "learning.task_transitioned",
        "memory.claim_reviewed",
    ],
)
async def test_generic_archive_rejects_server_owned_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event_type: str,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/archive/events",
            headers=internal,
            json={
                "event_id": f"forged-{event_type}",
                "account_id": "account-forged",
                "event_type": event_type,
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "forged",
                "payload": {},
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "server_owned_event_required",
        "event_type": event_type,
    }


@pytest.mark.asyncio
async def test_internal_archive_writer_cannot_append_after_deletion_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        app.state.memory_store.begin_account_deletion(
            user_id=identity["user_id"],
            started_at=datetime.now(UTC).isoformat(),
        )
        response = await client.post(
            "/v1/archive/events",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={
                "event_id": "too-late-event",
                "account_id": identity["user_id"],
                "event_type": "account.deletion_fence_probe",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "system",
                "source": "test",
                "payload": {"reason": "不应写入"},
            },
        )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_late_persona_background_task_is_rejected_by_the_deletion_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    account_id = "persona-deleting-account"
    app.state.memory_store.begin_account_deletion(
        user_id=account_id,
        started_at=datetime.now(UTC).isoformat(),
    )
    engine = PersonaObservationStub()

    await observe_persona(
        SimpleNamespace(app=app),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
        account_write=_account_write,
        account_id=account_id,
        source_event_id="late-persona-evidence",
        speech_duration_ms=1000,
        pause_ratio=0.2,
        quality_score=0.9,
    )

    assert engine.observations == 0


@pytest.mark.asyncio
async def test_guest_and_uncertain_evidence_is_recorded_but_hidden_from_owner_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # The account is a verified adult so the owner read exit is reachable;
        # the evidence under test stays guest/uncertain (never the owner).
        identity = await _register_verified_adult(
            client,
            app,
            username="guest-evidence-owner",
        )
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        classified_session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        responses = []
        for speaker_class in ("guest", "uncertain"):
            responses.append(
                await client.post(
                    "/v1/archive/session-events",
                    headers=internal,
                    json={
                        "event_id": f"discard-{speaker_class}",
                        "session_id": session["session_id"],
                        "event_type": "speech.utterance_finalized",
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "speaker_class": speaker_class,
                        "source": "funasr.authoritative_final",
                        "payload": {"text": "不得进入主人长期档案。"},
                    },
                )
            )
            responses.append(
                await client.post(
                    "/v1/archive/session-events",
                    headers=internal,
                    json={
                        "event_id": f"discard-direct-{speaker_class}",
                        "session_id": classified_session["session_id"],
                        "event_type": "speaker.classified",
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "speaker_class": speaker_class,
                        "source": "speaker.authority",
                        "payload": {"classification": speaker_class},
                    },
                )
            )
        timeline = await client.get("/v1/archive/timeline", headers=headers)

    assert [response.status_code for response in responses] == [201, 201, 201, 201]
    assert {response.json()["event_id"] for response in responses} == {
        "discard-guest",
        "discard-direct-guest",
        "discard-uncertain",
        "discard-direct-uncertain",
    }
    speaker_classes: tuple[SpeakerClass, ...] = ("guest", "uncertain")
    for speaker_class in speaker_classes:
        isolated = await app.state.life_archive.context(
            ContextQuery(
                account_id=identity["user_id"],
                session_id=session["session_id"],
                speaker_class=speaker_class,
            )
        )
        assert [item.event_id for item in isolated.evidence] == [f"discard-{speaker_class}"]
    assert timeline.json() == {"items": []}


@pytest.mark.asyncio
async def test_agent_records_session_event_without_trusting_an_account_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register_verified_adult(
            client,
            app,
            username="archive-session-event-owner",
        )
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        internal_headers = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        parent = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "session-event-parent-001",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "turn_id": 1,
                "generation_id": 2,
                "active_subject_id": identity["user_id"],
                "payload": {"text": "先保存这句用户话轮。"},
            },
        )
        recorded = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "session-event-001",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "你实际听到了这一句。",
                    "actual_heard": True,
                    "history_eligible": False,
                    "owner_projection_eligible": False,
                    "response_provenance": {
                        "fence": {
                            "session_id": session["session_id"],
                            "turn_id": 1,
                            "generation_id": 2,
                            "tool_epoch": 0,
                        },
                        "planner_policy_version": PLANNER_POLICY_VERSION,
                        "interaction_mode": "legacy",
                        "mode_policy_version": "caller-forged",
                        "digital_self_version_id": None,
                        "manifest_sha256": None,
                        "relationship_profile_id": None,
                        "relationship_profile_version": None,
                        "speaker_class": "guest",
                        "speaker_reason_code": "caller-forged",
                        "speaker_profile_id": "caller-forged",
                        "speaker_model_version": "caller-forged",
                        "speaker_template_version": 99,
                        "source_refs": [
                            {
                                "kind": "persona_trait",
                                "item_id": "trait-1",
                                "source_event_ids": ["session-event-parent-001"],
                            }
                        ],
                        "epistemic_status": "fact",
                        "epistemic_reason_codes": ["caller-forged"],
                        "disclosures": ["inference", "privacy_refusal"],
                        "llm_provider": "qwen",
                        "llm_model": "qwen-test",
                        "tts_provider": "volcengine_doubao",
                        "tts_model": "seed-tts-2.0",
                        "actual_voice_profile_id": "warm_companion",
                        "actual_voice_resource_id": "seed-tts-2.0",
                        "actual_voice_speaker_sha256": designed_voice_speaker_sha256(
                            "warm_companion"
                        ),
                    },
                },
            },
        )
        mismatched_fence = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "session-event-fence-mismatch",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "这条回答携带了错误的完整 fence。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=1,
                        generation_id=99,
                        source_refs=[
                            {
                                "kind": "persona_trait",
                                "item_id": "trait-1",
                                "source_event_ids": ["session-event-parent-001"],
                            }
                        ],
                    ),
                },
            },
        )
        forged_planner = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "session-event-planner-mismatch",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "这条回答携带了未知规划器版本。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=1,
                        generation_id=2,
                        source_refs=[],
                        planner_policy_version="forged-planner",
                    ),
                },
            },
        )
        mismatched_voice = await client.post(
            "/v1/archive/session-events",
            headers=internal_headers,
            json={
                "event_id": "session-event-voice-mismatch",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "这条回答伪造了实际使用的音色。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=1,
                        generation_id=2,
                        source_refs=[],
                        actual_voice_profile_id="forged-profile",
                    ),
                },
            },
        )
        timeline = await client.get("/v1/archive/timeline", headers=headers)

    assert (parent.status_code, recorded.status_code) == (201, 201)
    assert mismatched_fence.status_code == 409
    assert mismatched_fence.json()["detail"] == {"code": "response_provenance_fence_mismatch"}
    assert forged_planner.status_code == 409
    assert forged_planner.json()["detail"] == {"code": "response_provenance_planner_invalid"}
    assert mismatched_voice.status_code == 409
    assert mismatched_voice.json()["detail"] == {"code": "response_provenance_voice_mismatch"}
    assistant_payload = timeline.json()["items"][0]["payload"]
    assert assistant_payload["actual_heard"] is True
    assert assistant_payload["history_eligible"] is True
    assert assistant_payload["owner_projection_eligible"] is True
    provenance = assistant_payload["response_provenance"]
    assert provenance["interaction_mode"] == "companion"
    assert provenance["mode_policy_version"] == "s2-v1"
    assert provenance["speaker_class"] == "owner"
    assert provenance["speaker_reason_code"] == "unavailable"
    assert provenance["speaker_model_version"] == "unavailable"
    assert provenance["speaker_profile_id"] is None
    assert provenance["speaker_template_version"] is None
    assert provenance["source_refs"][0]["source_event_ids"] == ["session-event-parent-001"]
    assert provenance["epistemic_status"] == "not_applicable"
    assert provenance["epistemic_reason_codes"] == ["no_grounded_items"]
    assert provenance["disclosures"] == []
    assert provenance["tts_provider"] == "volcengine_doubao"
    assert provenance["tts_model"] == "seed-tts-2.0"
    assert provenance["actual_voice_profile_id"] == "warm_companion"
    assert provenance["actual_voice_resource_id"] == "seed-tts-2.0"
    assert provenance["actual_voice_speaker_sha256"] == designed_voice_speaker_sha256(
        "warm_companion"
    )
    assert "score" not in provenance


@pytest.mark.asyncio
async def test_response_provenance_rejects_guest_or_assistant_source_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        guest_source = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "guest-source-forged-provenance",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "guest",
                "source": "funasr.authoritative_final",
                "turn_id": 8,
                "generation_id": 8,
                "payload": {"text": "访客提供的内容不能成为主人回答来源。"},
            },
        )
        parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "owner-parent-forged-provenance",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "turn_id": 9,
                "generation_id": 9,
                "payload": {"text": "主人当前问题。"},
            },
        )
        rejected = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "assistant-forged-provenance",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 9,
                "generation_id": 9,
                "tool_epoch": 0,
                "payload": {
                    "text": "不能归档为有主人来源的回答。",
                    "actual_heard": True,
                    "response_provenance": {
                        "fence": {
                            "session_id": session["session_id"],
                            "turn_id": 9,
                            "generation_id": 9,
                            "tool_epoch": 0,
                        },
                        "planner_policy_version": PLANNER_POLICY_VERSION,
                        "interaction_mode": "companion",
                        "mode_policy_version": "s2-v1",
                        "digital_self_version_id": None,
                        "manifest_sha256": None,
                        "relationship_profile_id": None,
                        "relationship_profile_version": None,
                        "speaker_class": "owner",
                        "speaker_reason_code": "owner_match",
                        "speaker_profile_id": "speaker-profile",
                        "speaker_model_version": "campplus-test",
                        "speaker_template_version": 1,
                        "source_refs": [
                            {
                                "kind": "memory_claim",
                                "item_id": "forged-claim",
                                "source_event_ids": ["guest-source-forged-provenance"],
                            }
                        ],
                        "epistemic_status": "fact",
                        "epistemic_reason_codes": ["grounded_manifest"],
                        "disclosures": [],
                        "llm_provider": "qwen",
                        "llm_model": "qwen-test",
                        "tts_provider": "volcengine_doubao",
                        "tts_model": "seed-tts-2.0",
                        "actual_voice_profile_id": "warm_companion",
                        "actual_voice_resource_id": "seed-tts-2.0",
                        "actual_voice_speaker_sha256": designed_voice_speaker_sha256(
                            "warm_companion"
                        ),
                    },
                },
            },
        )

    assert (guest_source.status_code, parent.status_code) == (201, 201)
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {"code": "response_provenance_source_invalid"}


@pytest.mark.asyncio
async def test_response_provenance_requires_a_nonempty_source_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "empty-source-parent",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "funasr.authoritative_final",
                "turn_id": 1,
                "generation_id": 1,
                "payload": {"text": "主人问题。"},
            },
        )
        rejected = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "empty-source-response",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 1,
                "tool_epoch": 0,
                "payload": {
                    "text": "这条来源为空。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=1,
                        generation_id=1,
                        source_refs=[
                            {
                                "kind": "memory_claim",
                                "item_id": "empty-source-claim",
                                "source_event_ids": [],
                            }
                        ],
                    ),
                },
            },
        )

    assert parent.status_code == 201
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "response_provenance_invalid"


@pytest.mark.asyncio
async def test_response_provenance_requires_projection_eligibility_and_allows_owner_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="source-without-projection",
                account_id=identity["user_id"],
                subject_id=identity["user_id"],
                event_type="speech.utterance_finalized",
                occurred_at=datetime.now(UTC),
                speaker_class="owner",
                source="test",
                payload={"text": "缺少投影资格。"},
            )
        )
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="owner-action-source",
                account_id=identity["user_id"],
                subject_id=identity["user_id"],
                event_type="owner.action_recorded",
                occurred_at=datetime.now(UTC),
                speaker_class="owner",
                source="user.growth_feedback",
                payload={"owner_projection_eligible": True},
            )
        )
        parent = {
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": datetime.now(UTC).isoformat(),
            "speaker_class": "owner",
            "source": "funasr.authoritative_final",
            "active_subject_id": identity["user_id"],
            "payload": {"text": "主人问题。"},
        }
        first_parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                **parent,
                "event_id": "missing-projection-parent",
                "turn_id": 1,
                "generation_id": 1,
            },
        )
        missing_projection = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "missing-projection-response",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 1,
                "tool_epoch": 0,
                "payload": {
                    "text": "不应采用缺少资格的来源。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=1,
                        generation_id=1,
                        source_refs=[
                            {
                                "kind": "memory_claim",
                                "item_id": "missing-projection-claim",
                                "source_event_ids": ["source-without-projection"],
                            }
                        ],
                    ),
                },
            },
        )
        second_parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={**parent, "event_id": "owner-action-parent", "turn_id": 2, "generation_id": 2},
        )
        owner_action = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "owner-action-response",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 2,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "允许已验证的主人行动来源。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=2,
                        generation_id=2,
                        source_refs=[
                            {
                                "kind": "memory_claim",
                                "item_id": "owner-action-claim",
                                "source_event_ids": ["owner-action-source"],
                            }
                        ],
                    ),
                },
            },
        )

    assert (first_parent.status_code, second_parent.status_code) == (201, 201)
    assert missing_projection.json()["detail"] == {"code": "response_provenance_source_invalid"}
    assert owner_action.status_code == 201


@pytest.mark.asyncio
async def test_shadow_owner_candidate_archives_style_only_persona_snapshot_without_source_refs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        shadow_parent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "shadow-style-parent",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "uncertain",
                "source": "funasr.authoritative_final",
                "turn_id": 2,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "可能是主人。",
                    "speaker_reason_code": "shadow_owner_candidate",
                },
            },
        )
        response = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "shadow-style-response",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 2,
                "generation_id": 2,
                "tool_epoch": 0,
                "payload": {
                    "text": "只采用低敏风格。",
                    "actual_heard": True,
                    "response_provenance": _response_provenance(
                        session_id=session["session_id"],
                        turn_id=2,
                        generation_id=2,
                        source_refs=[],
                        persona_version_id="persona-version-1",
                        persona_version_number=1,
                        persona_style_only=True,
                    ),
                },
            },
        )

    assert (shadow_parent.status_code, response.status_code) == (201, 201)
    provenance = (
        await app.state.life_archive.event(
            account_id=identity["user_id"], event_id="shadow-style-response"
        )
    ).payload["response_provenance"]
    assert provenance["epistemic_status"] == "not_applicable"
    assert provenance["disclosures"] == []
    assert provenance["source_refs"] == []
    assert provenance["persona_version_id"] == "persona-version-1"
    assert provenance["persona_version_number"] == 1
    assert provenance["persona_style_only"] is True


@pytest.mark.asyncio
async def test_assistant_evidence_waits_for_its_canonical_parent_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        response = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "missing-parent-assistant",
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 7,
                "generation_id": 3,
                "payload": {"text": "迟到的回复。", "actual_heard": True},
            },
        )

    assert response.status_code == 425
    assert response.json()["detail"] == {"code": "parent_turn_not_recorded"}


@pytest.mark.parametrize(
    (
        "speaker_class",
        "reason_code",
        "expected_history",
        "expected_owner_projection",
        "expected_private_authority",
        "expected_low_sensitivity",
    ),
    (
        pytest.param("owner", None, True, True, True, False, id="owner"),
        pytest.param(
            "guest",
            "shadow_owner_candidate",
            False,
            False,
            False,
            False,
            id="guest-forged-shadow-reason",
        ),
        pytest.param(
            "uncertain",
            "shadow_owner_candidate",
            False,
            False,
            False,
            True,
            id="shadow-owner-candidate",
        ),
        pytest.param(
            "uncertain",
            "shadow_ambiguous_candidate",
            False,
            False,
            False,
            False,
            id="ambiguous",
        ),
    ),
)
@pytest.mark.asyncio
async def test_session_events_canonicalize_the_user_and_assistant_permission_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    speaker_class: SpeakerClass,
    reason_code: str | None,
    expected_history: bool,
    expected_owner_projection: bool,
    expected_private_authority: bool,
    expected_low_sensitivity: bool,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    user_event_id = f"interaction-user-{speaker_class}-{reason_code or 'formal'}"
    assistant_event_id = f"interaction-assistant-{speaker_class}-{reason_code or 'formal'}"
    forged_interaction = {
        "interaction_mode": "legacy",
        "mode_policy_version": "caller-forged",
        "simulated_output": True,
        "history_eligible": not expected_history,
        "owner_projection_eligible": not expected_owner_projection,
        "capabilities": {"private_memory": True, "persona": True, "tools": True},
        "caller_forged": True,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if expected_history or expected_low_sensitivity:
            identity = await _register_verified_adult(
                client,
                app,
                username=f"archive-matrix-{speaker_class}",
            )
        else:
            identity = (await client.post("/v1/auth/anonymous")).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        # Only the verified-adult fixture may claim the account as the speaker
        # subject; the unknown cases stay without a subject claim.
        subject_claim = (
            {"active_subject_id": identity["user_id"]}
            if expected_history or expected_low_sensitivity
            else {}
        )
        user_event = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                **subject_claim,
                "event_id": user_event_id,
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": speaker_class,
                "source": "test",
                "turn_id": 1,
                "generation_id": 1,
                "payload": {
                    "text": user_event_id,
                    "speaker_reason_code": reason_code,
                    "interaction_mode": "legacy",
                    "mode_policy_version": "caller-forged",
                    "simulated_output": True,
                    "history_eligible": not expected_history,
                    "owner_projection_eligible": not expected_owner_projection,
                    "interaction": forged_interaction,
                },
            },
        )
        assistant_event = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": assistant_event_id,
                "session_id": session["session_id"],
                "event_type": "assistant.playout_stopped",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "assistant",
                "source": "generation_fence.actual_heard",
                "turn_id": 1,
                "generation_id": 1,
                "payload": {
                    "text": assistant_event_id,
                    "actual_heard": True,
                    "interaction_mode": "legacy",
                    "mode_policy_version": "caller-forged",
                    "simulated_output": True,
                    "history_eligible": expected_history,
                    "owner_projection_eligible": expected_owner_projection,
                    "interaction": forged_interaction,
                },
            },
        )

    assert (user_event.status_code, assistant_event.status_code) == (201, 201)
    recorded = await app.state.life_archive.context(
        ContextQuery(
            account_id=identity["user_id"],
            session_id=session["session_id"],
            speaker_class=speaker_class,
        )
    )
    events = {event.event_id: event for event in recorded.evidence}
    assert {user_event_id, assistant_event_id}.issubset(events)

    for event_id in (user_event_id, assistant_event_id):
        payload = events[event_id].payload
        interaction = payload["interaction"]
        assert payload["interaction_mode"] == interaction["interaction_mode"] == "companion"
        assert payload["mode_policy_version"] == interaction["mode_policy_version"] == "s2-v1"
        assert payload["simulated_output"] is interaction["simulated_output"] is False
        assert payload["history_eligible"] is expected_history
        assert interaction["history_eligible"] is expected_history
        assert payload["owner_projection_eligible"] is expected_owner_projection
        assert interaction["owner_projection_eligible"] is expected_owner_projection
        assert "caller_forged" not in interaction

    user_capabilities = events[user_event_id].payload["interaction"]["capabilities"]
    assert user_capabilities["private_memory"] is expected_private_authority
    assert user_capabilities["persona"] is expected_private_authority
    assert user_capabilities["tools"] is expected_private_authority
    assert user_capabilities["persona_low_sensitivity"] is expected_low_sensitivity
    assert user_capabilities["history"] is expected_history
    assert user_capabilities["learning"] is expected_history
    assistant_capabilities = events[assistant_event_id].payload["interaction"]["capabilities"]
    assert assistant_capabilities["private_memory"] is False
    assert assistant_capabilities["persona"] is False
    assert assistant_capabilities["persona_low_sensitivity"] is False
    assert assistant_capabilities["tools"] is False


@pytest.mark.asyncio
async def test_session_event_retry_ignores_delivery_timestamp_but_rejects_semantic_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    occurred_at = datetime(2026, 7, 20, 11, 0, tzinfo=UTC)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register_verified_adult(
            client,
            app,
            username="archive-retry-owner",
        )
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        session = (await client.post("/v1/sessions", headers=owner_headers, json={})).json()
        other_session = (await client.post("/v1/sessions", headers=owner_headers, json={})).json()
        other_owner = (await client.post("/v1/auth/anonymous")).json()
        other_owner_headers = {"Authorization": f"Bearer {other_owner['access_token']}"}
        other_owner_session = (
            await client.post("/v1/sessions", headers=other_owner_headers, json={})
        ).json()
        event = {
            "event_id": "session-event-retry-001",
            "session_id": session["session_id"],
            "event_type": "speech.utterance_finalized",
            "occurred_at": occurred_at.isoformat(),
            "speaker_class": "owner",
            "source": "funasr.authoritative_final",
            "turn_id": 1,
            "generation_id": 0,
            "active_subject_id": owner["user_id"],
            "payload": {"text": "这是一段稳定的权威转写。"},
        }
        first = await client.post("/v1/archive/session-events", headers=internal, json=event)
        retry = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={**event, "occurred_at": (occurred_at.replace(minute=1)).isoformat()},
        )
        changed_payload = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={**event, "payload": {"text": "内容已被替换。"}},
        )
        changed_session = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={**event, "session_id": other_session["session_id"]},
        )
        changed_owner = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={**event, "session_id": other_owner_session["session_id"]},
        )

    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json()["duplicate"] is True
    assert changed_payload.status_code == 409
    assert changed_session.status_code == 409
    assert changed_owner.status_code == 409
    assert [
        response.json()["detail"]["code"]
        for response in (changed_payload, changed_session, changed_owner)
    ] == ["event_id_content_conflict"] * 3
