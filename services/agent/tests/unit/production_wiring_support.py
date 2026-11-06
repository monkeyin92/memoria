"""Shared builders for the production prompt wiring contract tests.

The RuntimeProfile payload, ModePolicy and DuplexRuntime these tests build
are the same across every behavior file, so they live here."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.runtime_profile import (
    VERIFY_KEY_ENV,
    parse_runtime_profile,
)
from services.agent.src.transactional_effect_commit import PreparedToolEffect
from services.agent.tests.unit.runtime_profile_test_helpers import (
    TEST_VERIFY_KEY,
    canonical_wire_payload,
    install_receipt_verifier,
)
from services.speaker.domain import (
    SpeakerDecision,
    permissions_for_speaker,
)


def _speaker_decision() -> SpeakerDecision:
    return SpeakerDecision(
        classification="owner",
        score=0.98,
        quality_score=0.95,
        reason_code="owner_match",
        model_version="campplus-test",
        template_version=1,
        profile_id="owner-profile",
        permissions=permissions_for_speaker("owner"),
    )


def _profile(
    epoch: int,
    *,
    subject: str = "person_child",
    mode: str = "student_minor",
    session_id: str = "ses_prompt_wiring",
) -> object:
    is_child = subject == "person_child"
    return canonical_wire_payload(
        runtime_profile_id=f"rp_epoch_{epoch}",
        session_id=session_id,
        active_subject_id=subject,
        subject_category="minor" if is_child else "adult",
        age_band="under_14" if is_child else "adult",
        service_mode=mode,
        session_epoch=epoch,
        capabilities=["chat", "tutor", "english_practice", "memory_recall_private"],
    )


def _policy(profile: object) -> ModePolicy:
    base = ModePolicy.companion_for_test(
        policy_version="test-policy",
        private_context=True,
        owner_evidence=True,
        tools=True,
        voice_profile=True,
        shadow_low_sensitivity_persona=False,
        session_focus="chat",
    )
    if profile is None:
        return replace(base, runtime_profile=None)
    parsed = parse_runtime_profile(profile, verify_key=TEST_VERIFY_KEY)
    assert parsed is not None
    return replace(base, runtime_profile=parsed)


def _runtime(monkeypatch: pytest.MonkeyPatch) -> DuplexRuntime:
    monkeypatch.setenv(VERIFY_KEY_ENV, TEST_VERIFY_KEY)
    runtime = DuplexRuntime.create(session_id="ses_prompt_wiring", device_id="dev_01J_test")
    install_receipt_verifier(runtime)

    async def _static_refresh() -> object:
        return runtime.orchestrator.runtime_profiles.current

    runtime.set_runtime_profile_refresher(_static_refresh)
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        install_playback_stop_seam,
    )

    install_playback_stop_seam(runtime)
    return runtime


async def _prepared_intent(
    _args: dict[str, object],
    _cancel: asyncio.Event,
) -> PreparedToolEffect:
    return PreparedToolEffect(intent="memory_write", payload={"prepared": True})

