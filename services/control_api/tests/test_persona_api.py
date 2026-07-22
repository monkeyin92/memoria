from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "test-internal-archive-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")


def _shadow_persona_payload(
    text: str,
    *,
    profile_id: str = "persona-shadow-profile",
    quality_score: float = 0.9,
) -> dict[str, object]:
    return {
        "text": text,
        "persona_eligible": True,
        "speaker_reason_code": "shadow_owner_candidate",
        "speaker_profile_id": profile_id,
        "speaker_quality_score": quality_score,
        "speaker_model_version": "campplus-test",
        "speaker_template_version": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("persona_eligible", (None, False))
async def test_owner_persona_learning_requires_explicit_turn_eligibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    persona_eligible: bool | None,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": f"persona-ineligible-{persona_eligible}", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        session = (await client.post("/v1/sessions", headers=headers, json={})).json()
        payload: dict[str, object] = {"text": "我觉得先把事实弄清楚。"}
        if persona_eligible is not None:
            payload["persona_eligible"] = persona_eligible
        recorded = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": f"persona-ineligible-{persona_eligible}",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test",
                "payload": payload,
            },
        )
        traits = await client.get("/v1/persona/traits?include_candidates=true", headers=headers)

    assert recorded.status_code == 201
    assert traits.json() == {"items": []}


@pytest.mark.asyncio
async def test_consent_drives_non_blocking_owner_learning_and_session_scoped_capsule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "persona-owner", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        status_before = await client.get("/v1/persona/status", headers=headers)
        consent = await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        status_after = await client.get("/v1/persona/status", headers=headers)
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        for index, text in enumerate(
            (
                "我觉得先把事实弄清楚。",
                "我觉得应该先听完对方。",
                "我觉得答应的事要做到。",
            )
        ):
            recorded = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    "event_id": f"persona-api-style-{index}",
                    "session_id": session["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime(2026, 7, 19, 15, index, tzinfo=UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "test",
                    "payload": {"text": text, "persona_eligible": True},
                    "turn_id": index + 1,
                },
            )
            assert recorded.status_code == 201

        traits = await client.get("/v1/persona/traits", headers=headers)
        versions = await client.get("/v1/persona/versions", headers=headers)
        owner_capsule = await client.post(
            "/v1/persona/session-capsule",
            headers=internal,
            json={
                "session_id": session["session_id"],
                "speaker_class": "owner",
                "topic": "表达看法",
                "max_chars": 500,
            },
        )
        guest_capsule = await client.post(
            "/v1/persona/session-capsule",
            headers=internal,
            json={
                "session_id": session["session_id"],
                "speaker_class": "guest",
                "topic": "表达看法",
                "max_chars": 500,
            },
        )
        revoked = await client.delete("/v1/persona/consent", headers=headers)
        revoked_owner_capsule = await client.post(
            "/v1/persona/session-capsule",
            headers=internal,
            json={
                "session_id": session["session_id"],
                "speaker_class": "owner",
                "topic": "表达看法",
                "max_chars": 500,
            },
        )

    assert consent.status_code == 201
    assert status_before.json() == {"learning_allowed": False}
    assert status_after.json() == {"learning_allowed": True}
    assert any(item["status"] == "confirmed" for item in traits.json()["items"])
    assert versions.json()["items"][0]["status"] == "active"
    assert "我觉得" in owner_capsule.json()["prompt_fragment"]
    assert guest_capsule.json()["entries"] == []
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert revoked_owner_capsule.json()["entries"] == []


@pytest.mark.asyncio
async def test_revoked_owner_can_manage_confirmed_traits_and_version_history(
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
                json={"username": "revoked-persona-owner", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        for index in range(3):
            response = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    "event_id": f"revoked-persona-style-{index}",
                    "session_id": session["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime(2026, 7, 21, 10, index, tzinfo=UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "test",
                    "payload": {"text": "我觉得先把事实弄清楚。", "persona_eligible": True},
                    "turn_id": index + 1,
                },
            )
            assert response.status_code == 201

        initial_traits = (await client.get("/v1/persona/traits", headers=headers)).json()["items"]
        target = next(item for item in initial_traits if item["category"] == "verbal_tic")
        initial_version = (await client.get("/v1/persona/versions", headers=headers)).json()[
            "items"
        ][0]
        await client.delete("/v1/persona/consent", headers=headers)

        traits_after_revoke = await client.get("/v1/persona/traits", headers=headers)
        versions_after_revoke = await client.get("/v1/persona/versions", headers=headers)
        disabled = await client.post(
            f"/v1/persona/traits/{target['trait_id']}/review",
            headers=headers,
            json={"action": "disable"},
        )
        versions_after_disable = (await client.get("/v1/persona/versions", headers=headers)).json()[
            "items"
        ]
        rolled_back = await client.post(
            f"/v1/persona/versions/{initial_version['version_id']}/rollback",
            headers=headers,
        )

    assert any(
        item["trait_id"] == target["trait_id"] for item in traits_after_revoke.json()["items"]
    )
    assert versions_after_revoke.json()["items"][0]["status"] == "active"
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert any(version["status"] == "superseded" for version in versions_after_disable)
    assert rolled_back.status_code == 200
    assert rolled_back.json()["version_id"] == initial_version["version_id"]
    assert rolled_back.json()["status"] == "active"


@pytest.mark.asyncio
async def test_single_uncertain_candidate_is_hidden_but_owner_review_api_remains_compatible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "uncertain-persona", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        recorded = await client.post(
            "/v1/archive/session-events",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={
                "event_id": "persona-api-uncertain-candidate",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "uncertain",
                "source": "test",
                "payload": _shadow_persona_payload("我觉得先把事实弄清楚，再讨论责任。"),
                "turn_id": 1,
            },
        )
        customer_traits = await client.get("/v1/persona/traits", headers=headers)
        traits = (
            await client.get(
                "/v1/persona/traits?include_candidates=true",
                headers=headers,
            )
        ).json()["items"]
        unauthenticated_candidates = await client.get("/v1/persona/traits?include_candidates=true")
        versions_before_review = await client.get("/v1/persona/versions", headers=headers)
        verbal_tic = next(item for item in traits if item["category"] == "verbal_tic")
        other = (
            await client.post(
                "/v1/auth/register",
                json={"username": "other-persona", "password": "safe-password"},
            )
        ).json()
        other_headers = {"Authorization": f"Bearer {other['access_token']}"}
        other_traits = await client.get("/v1/persona/traits", headers=other_headers)
        other_candidates = await client.get(
            "/v1/persona/traits?include_candidates=true",
            headers=other_headers,
        )
        other_review = await client.post(
            f"/v1/persona/traits/{verbal_tic['trait_id']}/review",
            headers=other_headers,
            json={"action": "confirm"},
        )
        reviewed = await client.post(
            f"/v1/persona/traits/{verbal_tic['trait_id']}/review",
            headers=headers,
            json={"action": "confirm"},
        )
        versions_after_review = await client.get("/v1/persona/versions", headers=headers)
        uncertain_capsule = await client.post(
            "/v1/persona/session-capsule",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={
                "session_id": session["session_id"],
                "speaker_class": "uncertain",
                "speaker_reason_code": "shadow_owner_candidate",
                "topic": "表达看法",
                "max_chars": 500,
            },
        )
        await client.delete("/v1/persona/consent", headers=headers)
        revoked_uncertain_capsule = await client.post(
            "/v1/persona/session-capsule",
            headers={"X-Memoria-Internal-Token": "test-internal-archive-token"},
            json={
                "session_id": session["session_id"],
                "speaker_class": "uncertain",
                "topic": "表达看法",
                "max_chars": 500,
            },
        )

    assert recorded.status_code == 201
    assert customer_traits.json() == {"items": []}
    assert verbal_tic["status"] == "candidate"
    assert verbal_tic["source_event_ids"] == ["persona-api-uncertain-candidate"]
    assert unauthenticated_candidates.status_code == 401
    assert versions_before_review.json() == {"items": []}
    assert other_traits.json() == {"items": []}
    assert other_candidates.json() == {"items": []}
    assert other_review.status_code == 404
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "confirmed"
    assert versions_after_review.json()["items"][0]["version_number"] == 1
    interaction = uncertain_capsule.json()["interaction"]
    assert interaction["history_eligible"] is True
    assert interaction["owner_projection_eligible"] is False
    assert interaction["capabilities"]["private_memory"] is False
    assert interaction["capabilities"]["persona"] is False
    assert interaction["capabilities"]["persona_low_sensitivity"] is True
    assert interaction["capabilities"]["tools"] is False
    assert interaction["capabilities"]["learning"] is True
    assert "已确认表达风格 v1" in uncertain_capsule.json()["prompt_fragment"]
    assert [item["category"] for item in uncertain_capsule.json()["entries"]] == ["verbal_tic"]
    assert revoked_uncertain_capsule.json()["entries"] == []


@pytest.mark.asyncio
async def test_consented_uncertain_cross_session_evidence_auto_publishes_persona_v1(
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
                json={"username": "automatic-persona", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        sessions = [
            (
                await client.post(
                    "/v1/sessions",
                    headers=headers,
                    json={"user_id": identity["user_id"], "voice_backend": "cascade"},
                )
            ).json()
            for _ in range(3)
        ]
        for index in range(6):
            recorded = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    "event_id": f"automatic-persona-{index}",
                    "session_id": sessions[index // 2]["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime(2026, 7, 21, 9, index, tzinfo=UTC).isoformat(),
                    "speaker_class": "uncertain",
                    "source": "test",
                    "payload": _shadow_persona_payload("我觉得先把事实弄清楚，再讨论责任。"),
                    "turn_id": index % 2 + 1,
                },
            )
            assert recorded.status_code == 201

        traits = (await client.get("/v1/persona/traits", headers=headers)).json()["items"]
        versions = (await client.get("/v1/persona/versions", headers=headers)).json()["items"]
        capsule = await client.post(
            "/v1/persona/session-capsule",
            headers=internal,
            json={
                "session_id": sessions[-1]["session_id"],
                "speaker_class": "uncertain",
                "speaker_reason_code": "shadow_owner_candidate",
                "topic": "表达看法",
                "max_chars": 500,
            },
        )

    assert versions[0]["version_number"] == 1
    assert versions[0]["reason"] == "automatic_style_learning_v1"
    assert any(
        trait["category"] == "verbal_tic" and trait["status"] == "confirmed" for trait in traits
    )
    assert "已确认表达风格 v1" in capsule.json()["prompt_fragment"]
    assert {item["category"] for item in capsule.json()["entries"]} == {
        "verbal_tic",
        "sentence_length",
        "discourse_style",
    }


@pytest.mark.asyncio
async def test_anonymous_or_direct_uncertain_turns_cannot_create_persona_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = (await client.post("/v1/auth/anonymous")).json()
        anonymous_headers = {"Authorization": f"Bearer {anonymous['access_token']}"}
        await app.state.persona_engine.grant_consent(
            account_id=anonymous["user_id"],
            policy_version="persona-learning-v1",
        )
        anonymous_session = (
            await client.post(
                "/v1/sessions",
                headers=anonymous_headers,
                json={"user_id": anonymous["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        anonymous_event = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "anonymous-uncertain-persona",
                "session_id": anonymous_session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "uncertain",
                "source": "test",
                "payload": {"text": "我觉得匿名会话不能学习。"},
                "turn_id": 1,
            },
        )
        anonymous_traits = await client.get("/v1/persona/traits", headers=anonymous_headers)

        registered = (
            await client.post(
                "/v1/auth/register",
                json={"username": "direct-persona", "password": "safe-password"},
            )
        ).json()
        registered_headers = {"Authorization": f"Bearer {registered['access_token']}"}
        await client.post(
            "/v1/persona/consent",
            headers=registered_headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        direct_event = await client.post(
            "/v1/archive/events",
            headers=internal,
            json={
                "event_id": "direct-uncertain-persona",
                "account_id": registered["user_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "uncertain",
                "source": "test",
                "payload": {"text": "我觉得直写事件不能学习。"},
            },
        )
        direct_traits = await client.get("/v1/persona/traits", headers=registered_headers)

    assert anonymous_event.status_code == 201
    assert anonymous_traits.json() == {"items": []}
    assert direct_event.status_code == 409
    assert direct_event.json()["detail"]["code"] == "session_bound_event_required"
    assert direct_traits.json() == {"items": []}


@pytest.mark.asyncio
async def test_guest_ambiguous_and_low_quality_turns_do_not_create_persona_candidates(
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
                json={"username": "guarded-persona", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        no_consent = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "no-consent-uncertain-persona",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "uncertain",
                "source": "test",
                "payload": _shadow_persona_payload(
                    "我觉得未授权的证据不能用于人格学习。",
                ),
                "turn_id": 1,
            },
        )
        assert no_consent.status_code == 201
        await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        for event_id, speaker_class, reason_code, quality_score in (
            ("guest-persona-evidence", "guest", "shadow_owner_candidate", 0.95),
            (
                "ambiguous-uncertain-persona",
                "uncertain",
                "shadow_ambiguous_candidate",
                0.95,
            ),
            (
                "low-quality-uncertain-persona",
                "uncertain",
                "shadow_owner_candidate",
                0.49,
            ),
        ):
            payload = _shadow_persona_payload(
                "我觉得这条证据不能用于人格学习。",
                quality_score=quality_score,
            )
            payload["speaker_reason_code"] = reason_code
            response = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    "event_id": event_id,
                    "session_id": session["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "speaker_class": speaker_class,
                    "source": "test",
                    "payload": payload,
                    "turn_id": 1,
                },
            )
            assert response.status_code == 201
        traits = await client.get(
            "/v1/persona/traits?include_candidates=true",
            headers=headers,
        )

    assert traits.json() == {"items": []}


@pytest.mark.asyncio
async def test_value_or_decision_trait_requires_authenticated_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "decision-owner", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "persona-api-decision",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test",
                "payload": {
                    "text": "做重大决定时，我习惯先列事实，再睡一晚。",
                    "persona_eligible": True,
                },
                "turn_id": 1,
            },
        )
        customer_traits = await client.get("/v1/persona/traits", headers=headers)
        traits = (
            await client.get(
                "/v1/persona/traits?include_candidates=true",
                headers=headers,
            )
        ).json()["items"]
        decision = next(item for item in traits if item["category"] == "decision_habit")
        unauthenticated = await client.post(
            f"/v1/persona/traits/{decision['trait_id']}/review",
            json={"action": "confirm"},
        )
        missing_counterexample = await client.post(
            f"/v1/persona/traits/{decision['trait_id']}/review",
            headers=headers,
            json={"action": "confirm"},
        )
        confirmed = await client.post(
            f"/v1/persona/traits/{decision['trait_id']}/review",
            headers=headers,
            json={
                "action": "confirm",
                "counterexample": "紧急安全风险出现时会立即行动。",
            },
        )

    assert decision["status"] == "candidate"
    assert customer_traits.json() == {"items": []}
    assert unauthenticated.status_code == 401
    assert missing_counterexample.status_code == 422
    assert missing_counterexample.json()["detail"] == (
        "decision and value traits require a counterexample before confirmation"
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["version_id"]
