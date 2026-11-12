"""The live consent ceiling must dominate cached profile capabilities."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from services.control_api.app.routes import interaction

pytestmark = pytest.mark.hermetic


@pytest.mark.parametrize("allowed", [False, True])
async def test_live_retention_dominates_profile_capability(monkeypatch, allowed):
    profile = {
        "device_id": "device",
        "service_mode": "student_minor",
        "active_subject_id": "child",
        "subject_category": "minor",
        "capabilities": ["chat", "memory_recall_private"],
        "obligations": [],
    }
    before = deepcopy(profile)
    session = {"user_id": "parent", "interaction_mode": "companion", "mode_policy_version": "v1"}

    def require_session(request, session_id, *, terminal_response=False):
        assert session_id == "session"
        assert terminal_response is True
        return session

    monkeypatch.setattr(interaction, "require_active_voice_session", require_session)
    monkeypatch.setattr(
        interaction, "current_persistent_runtime_profile", AsyncMock(return_value=profile)
    )
    monkeypatch.setattr(
        interaction, "_subject_memory_retention_allowed", AsyncMock(return_value=allowed)
    )
    monkeypatch.setattr(interaction, "_owner_custom_persona_record", AsyncMock(return_value=None))
    monkeypatch.setattr(
        interaction, "_store", lambda r: SimpleNamespace(get_profile=lambda **k: {})
    )
    monkeypatch.setattr(
        interaction, "RuntimeProfileLedger", lambda s: SimpleNamespace(current=lambda d: None)
    )
    result = await interaction.session_policy(
        interaction.SessionPolicyRequest(session_id="session"), SimpleNamespace(), None
    )
    assert result["history_eligible"] is allowed
    assert result["capabilities"]["private_memory"] is allowed
    assert result["capabilities"]["history"] is allowed
    assert profile == before
