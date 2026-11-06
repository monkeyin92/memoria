"""Control API interaction: session-policy and action-policy endpoint contracts."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyReceiptV2,
)
from services.agent.src.mode_policy_client import ModePolicyClient
from services.control_api.app.main import create_app
from services.control_api.tests.interaction_api_test_support import (
    _RUNTIME_PROFILE_SIGNING_KEY,
    _attach_signed_runtime_profile,
    _configure,
    _identity,
    anyio_backend,  # noqa: F401 - pytest discovers imported fixtures by name
)


@pytest.mark.asyncio
async def test_capabilities_expose_explicit_preview_requirements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _identity(client)
        response = await client.get("/v1/interaction/capabilities", headers=headers)

    assert response.status_code == 200
    modes = response.json()["modes"]
    assert modes["companion"]["status"] == "available"
    assert modes["archive"] == {"status": "available", "conversational": False}
    assert modes["self_preview"]["status"] == "blocked"
    assert modes["self_preview"]["missing"] == [
        "approved_digital_self_version",
    ]
    assert modes["legacy"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_only_companion_creates_voice_sessions_and_keeps_preview_server_owned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _identity(client)
        invalid_binding = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"digital_self_version_id": "untrusted-client-version"},
        )
        blocked = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"interaction_mode": "self_preview"},
        )
        archive = await client.post(
            "/v1/sessions", headers=headers, json={"interaction_mode": "archive"}
        )
        created = await client.post(
            "/v1/sessions", headers=headers, json={"interaction_mode": "companion"}
        )

    assert invalid_binding.status_code == 422
    assert blocked.status_code == 422
    assert archive.status_code == 409
    assert archive.json()["detail"]["code"] == "mode_not_conversational"
    assert len(app.state.memory_store.list_voice_sessions(user_id=user_id)) == 1
    session = app.state.memory_store.get_voice_session_by_id(
        session_id=created.json()["session_id"]
    )
    assert session is not None
    assert session["interaction_mode"] == "companion"
    assert session["mode_policy_version"] == "s2-v1"
    assert session["companion_style_id"] == "starlight"
    assert session["digital_self_version_id"] is None
    assert session["relationship_profile_id"] is None
    assert session["legacy_grant_id"] is None


@pytest.mark.asyncio
async def test_tutor_focus_is_frozen_in_storage_and_internal_agent_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _identity(client)
        created = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"session_focus": "tutor_english"},
        )
        invalid = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"session_focus": "client_defined"},
        )
        cross_mode = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"interaction_mode": "archive", "session_focus": "tutor_homework"},
        )
        session_id = created.json()["session_id"]
        _attach_signed_runtime_profile(app, user_id=user_id, session_id=session_id)
        policy = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": "interaction-policy-token-that-is-long-enough"},
            json={"session_id": session_id},
        )

    assert created.status_code == 200
    assert created.json()["interaction"]["session_focus"] == "tutor_english"
    frozen = app.state.memory_store.get_voice_session_by_id(
        session_id=created.json()["session_id"]
    )
    assert frozen is not None and frozen["session_focus"] == "tutor_english"
    assert policy.status_code == 200
    assert policy.json()["session_focus"] == "tutor_english"
    assert invalid.status_code == 422
    assert cross_mode.status_code == 422


@pytest.mark.asyncio
async def test_companion_profile_change_only_applies_to_future_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _identity(client)
        first = (await client.post("/v1/sessions", headers=headers, json={})).json()
        changed = await client.put(
            f"/v1/memory/profile/{user_id}",
            headers=headers,
            json={"companion_id": "xuanmo"},
        )
        second = (await client.post("/v1/sessions", headers=headers, json={})).json()

    assert changed.status_code == 200
    first_frozen = app.state.memory_store.get_voice_session_by_id(session_id=first["session_id"])
    second_frozen = app.state.memory_store.get_voice_session_by_id(session_id=second["session_id"])
    assert first_frozen is not None and second_frozen is not None
    assert first_frozen["companion_style_id"] == "starlight"
    assert second_frozen["companion_style_id"] == "xuanmo"


@pytest.mark.asyncio
async def test_internal_policy_uses_its_own_capability_and_returns_frozen_session_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=headers, json={})).json()[
            "session_id"
        ]
        profile = _attach_signed_runtime_profile(
            app,
            user_id=user_id,
            session_id=session_id,
        )
        denied = await client.post(
            "/v1/interaction/session-policy",
            json={"session_id": session_id, "speaker_class": "guest"},
        )
        response = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": "interaction-policy-token-that-is-long-enough"},
            json={"session_id": session_id, "speaker_class": "guest"},
        )

    assert denied.status_code == 401
    assert response.status_code == 200
    policy = response.json()
    assert policy["interaction_mode"] == "companion"
    assert policy["mode_policy_version"] == "s2-v1"
    assert policy["runtime_profile"]["policy_bundle_version"] != policy["mode_policy_version"]
    assert policy["policy_scope"] == "session"
    assert policy["simulated_output"] is False
    assert policy["history_eligible"] is True
    assert policy["owner_projection_eligible"] is True
    assert policy["capabilities"]["private_memory"] is True
    assert policy["capabilities"]["persona"] is False
    assert policy["capabilities"]["persona_low_sensitivity"] is False
    assert policy["capabilities"]["tools"] is False
    assert policy["capabilities"]["history"] is True
    assert policy["capabilities"]["learning"] is True
    assert policy["capabilities"]["voice_profile"] is False
    assert policy["companion_style_id"] == "starlight"
    assert policy["companion_style_version"] == "companion-v1"
    assert policy["digital_self_version_id"] is None
    assert policy["relationship_profile_id"] is None
    assert policy["legacy_grant_id"] is None
    assert policy["runtime_profile"]["runtime_profile_id"] == profile.runtime_profile_id
    assert policy["runtime_profile"]["signature_schema"] == "runtime-profile-v2"
    assert "memory_capture" not in policy["runtime_profile"]["capabilities"]
    parsed = ModePolicyClient._parse(
        policy,
        runtime_profile_verify_key=_RUNTIME_PROFILE_SIGNING_KEY.decode(),
    )
    assert parsed.available is True
    assert parsed.runtime_profile is not None
    assert parsed.runtime_profile.profile.session_id == session_id
    assert policy["owner_display_name"] == "朋友"


@pytest.mark.asyncio
async def test_session_policy_fails_closed_without_persistent_runtime_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=headers, json={})).json()[
            "session_id"
        ]
        app.state._state.pop("session_runtime_service", None)
        response = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": "interaction-policy-token-that-is-long-enough"},
            json={"session_id": session_id},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "session_runtime_authority_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_kwargs", "context_kwargs", "expected_code"),
    [
        ({"expired": True}, {}, "session_runtime_profile_expired"),
        ({}, {"binding_id": "binding-replayed"}, "session_runtime_profile_binding_mismatch"),
        ({}, {"session_epoch": 2}, "session_runtime_profile_binding_mismatch"),
    ],
)
async def test_session_policy_rejects_stale_or_unusable_runtime_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile_kwargs: dict[str, bool],
    context_kwargs: dict[str, object],
    expected_code: str,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=headers, json={})).json()[
            "session_id"
        ]
        _attach_signed_runtime_profile(
            app,
            user_id=user_id,
            session_id=session_id,
            **profile_kwargs,
        )
        authority = app.state.session_runtime_service
        authority.context = replace(authority.context, **context_kwargs)
        response = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": "interaction-policy-token-that-is-long-enough"},
            json={"session_id": session_id},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == expected_code


@pytest.mark.asyncio
async def test_session_policy_accepts_signed_unknown_safe_as_conversation_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=headers, json={})).json()[
            "session_id"
        ]
        profile = _attach_signed_runtime_profile(
            app,
            user_id=user_id,
            session_id=session_id,
            unknown_subject=True,
        )
        response = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": "interaction-policy-token-that-is-long-enough"},
            json={"session_id": session_id},
        )
        plan_response = await client.post(
            "/v1/interaction/response-plan",
            headers={"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"},
            json={
                "session_id": session_id,
                "query": "告诉我关于我的秘密",
                "utterance_intent": "chat",
                "fence": {
                    "session_id": session_id,
                    "turn_id": 1,
                    "generation_id": 1,
                    "tool_epoch": 0,
                },
                "speaker_decision": {
                    "classification": "uncertain",
                    "reason_code": "unknown_safe_mode",
                    "model_version": "test",
                    "profile_id": None,
                    "template_version": None,
                },
            },
        )

    assert response.status_code == 200, response.text
    policy = response.json()
    assert policy["interaction_mode"] == "unknown_safe"
    assert policy["companion_style_id"] is None
    assert policy["companion_style_version"] is None
    assert "owner_display_name" not in policy
    assert policy["history_eligible"] is False
    assert policy["owner_projection_eligible"] is False
    assert policy["capabilities"] == {
        "conversation": True,
        "private_memory": False,
        "persona": False,
        "persona_low_sensitivity": False,
        "tools": False,
        "history": False,
        "learning": False,
        "voice_profile": False,
    }
    parsed = ModePolicyClient._parse(
        policy,
        runtime_profile_verify_key=_RUNTIME_PROFILE_SIGNING_KEY.decode(),
    )
    assert parsed.available is True
    assert parsed.mode == "unknown_safe"
    assert parsed.runtime_profile is not None
    assert parsed.runtime_profile.profile.runtime_profile_id == profile.runtime_profile_id

    # Direct regression for /response-plan under signed unknown_safe:
    # Fail-closed, zero grounded items, no private memory leakage.
    assert plan_response.status_code == 200, plan_response.text
    plan = plan_response.json()
    assert plan["grounded_items"] == []
    assert plan["direct_text"] is None
    assert parsed.allows_conversation() is True
    assert parsed.allows_private_context("owner") is False
    assert parsed.allows_tools("owner") is False
    assert parsed.history_eligible("owner") is False
    assert parsed.allows_learning("owner") is False


def _action_policy_body(
    session_id: str, *, capability: str = "memory_capture"
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "runtime_profile_id": f"rp-{session_id}",
        "capability": capability,
        "session_epoch": 1,
        "generation_id": 7,
        "turn_id": 4,
        "tool_epoch": 1,
        "data_classification": "private",
        "safety_state": "normal",
    }


def _action_policy_token() -> dict[str, str]:
    return {"X-Memoria-Internal-Token": "interaction-policy-token-that-is-long-enough"}


@pytest.mark.asyncio
async def test_action_policy_requires_internal_token_and_returns_full_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        created = await client.post("/v1/sessions", headers=user_headers, json={})
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        profile = _attach_signed_runtime_profile(app, user_id=user_id, session_id=session_id)
        body = _action_policy_body(session_id)
        denied = await client.post("/v1/interaction/action-policy", json=body)
        wrong_token = await client.post(
            "/v1/interaction/action-policy",
            headers={"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"},
            json=body,
        )
        self_reported = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json={**body, "actor_id": "caller-claimed-actor"},
        )
        authorized = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json=body,
        )

    assert denied.status_code == 401
    assert wrong_token.status_code == 401
    assert self_reported.status_code == 422
    assert authorized.status_code == 200
    receipt = PolicyReceiptV2.model_validate(authorized.json())
    assert receipt.actor_id == user_id
    assert receipt.session_id == session_id
    assert receipt.runtime_profile_id == profile.runtime_profile_id
    assert receipt.session_epoch == 1
    assert receipt.capability == "memory_capture"
    assert receipt.subject_id == profile.active_subject_id
    assert receipt.resource_owner_id == profile.active_subject_id
    assert receipt.device_id == profile.device_id
    assert receipt.binding_id == profile.binding_id
    assert receipt.binding_version == profile.binding_version
    assert receipt.exact_fence is True
    assert receipt.action_resource_fence.generation_id == 7
    assert receipt.action_resource_fence.turn_id == 4
    assert receipt.action_resource_fence.tool_epoch == 1
    assert receipt.effect == "allow_with_obligations"
    assert receipt.reason_code == "adult_memory_authorized"
    assert any(item.code == "WRITE_POLICY_RECEIPT" for item in receipt.obligations)


@pytest.mark.asyncio
async def test_action_policy_rejects_forged_actor_profile_and_epoch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _attach_signed_runtime_profile(app, user_id=user_id, session_id=session_id)
        body = _action_policy_body(session_id)
        forged_profile = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json={**body, "runtime_profile_id": "rp-forged-other-session"},
        )
        forged_epoch = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json={**body, "session_epoch": 2},
        )
        authority = app.state.session_runtime_service
        authority.profile = authority.profile.model_copy(update={"actor_id": "forged-actor"})
        forged_actor = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json=body,
        )

    assert forged_profile.status_code == 403
    assert forged_profile.json()["detail"]["code"] == "action_profile_forged"
    assert forged_epoch.status_code == 403
    assert forged_epoch.json()["detail"]["code"] == "action_fence_forged"
    assert forged_actor.status_code == 403
    assert forged_actor.json()["detail"]["code"] == "action_actor_forged"


@pytest.mark.asyncio
async def test_action_policy_rejects_non_deferred_and_unknown_capabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _attach_signed_runtime_profile(app, user_id=user_id, session_id=session_id)
        session_level = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json=_action_policy_body(session_id, capability="chat"),
        )
        unknown = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json=_action_policy_body(session_id, capability="not_a_capability"),
        )

    assert session_level.status_code == 403
    assert session_level.json()["detail"]["code"] == "action_capability_not_deferred"
    assert unknown.status_code == 422


@pytest.mark.asyncio
async def test_action_policy_returns_409_for_stale_fence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _attach_signed_runtime_profile(app, user_id=user_id, session_id=session_id)
        authority = app.state.session_runtime_service
        authority.context = replace(
            authority.context,
            generation_id=7,
            turn_id=4,
            tool_epoch=1,
        )
        body = _action_policy_body(session_id)
        replay = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json=body,
        )
        stale_generation = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json={**body, "generation_id": 6},
        )
        stale_epoch = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json={**body, "session_epoch": 0},
        )
        authority.context = replace(authority.context, session_epoch=2)
        epoch_behind = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json={**body, "session_epoch": 1},
        )

    assert replay.status_code == 200
    assert stale_generation.status_code == 409
    assert stale_generation.json()["detail"]["code"] == "action_fence_stale"
    assert stale_epoch.status_code == 422
    assert epoch_behind.status_code == 409
    assert epoch_behind.json()["detail"]["code"] == "action_fence_stale"


@pytest.mark.asyncio
async def test_action_policy_fails_closed_without_runtime_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        body = _action_policy_body(session_id)
        missing = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json=body,
        )
        _attach_signed_runtime_profile(app, user_id=user_id, session_id=session_id)
        unavailable = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json=body,
        )

    assert missing.status_code == 503
    assert missing.json()["detail"]["code"] == "session_runtime_authority_unavailable"
    assert unavailable.status_code == 200


@pytest.mark.asyncio
async def test_action_policy_requires_active_voice_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _attach_signed_runtime_profile(app, user_id=user_id, session_id=session_id)
        missing = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json=_action_policy_body("session-missing"),
        )
        tombstoned = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json=_action_policy_body(session_id),
        )
        app.state.memory_store.mark_voice_sessions_deleting(
            user_id=user_id,
            deleted_at=datetime.now(UTC).isoformat(),
        )
        after_delete = await client.post(
            "/v1/interaction/action-policy",
            headers=_action_policy_token(),
            json=_action_policy_body(session_id),
        )

    assert missing.status_code == 404
    assert tombstoned.status_code == 200
    assert after_delete.status_code == 410
