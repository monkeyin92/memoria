from __future__ import annotations

import pytest
from pydantic import ValidationError
from services.archive.domain import IdempotencyConflictError
from services.common.companions import designed_voice_speaker_sha256
from services.common.tts_defaults import STEPFUN_TTS_MODEL
from services.control_api.app.archive_event_conflicts import (
    ResponseProvenanceCreate,
    _canonical_actual_voice,
    event_id_conflict,
)
from services.digital_self.response_planner import PLANNER_POLICY_VERSION


@pytest.mark.hermetic
def test_content_conflict_409_reports_its_cause_code() -> None:
    conflict = event_id_conflict(IdempotencyConflictError("different content"))
    assert (conflict.status_code, conflict.detail) == (
        409,
        {"code": "event_id_content_conflict"},
    )


def _stepfun_provenance(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "fence": {
            "session_id": "session-1",
            "turn_id": 1,
            "generation_id": 2,
            "tool_epoch": 0,
        },
        "planner_policy_version": PLANNER_POLICY_VERSION,
        "interaction_mode": "companion",
        "mode_policy_version": "s2-v1",
        "speaker_class": "owner",
        "speaker_reason_code": "unavailable",
        "speaker_model_version": "unavailable",
        "epistemic_status": "not_applicable",
        "tts_provider": "stepfun",
        "tts_model": STEPFUN_TTS_MODEL,
        "actual_voice_profile_id": "warm_companion",
        "actual_voice_resource_id": "seed-tts-2.0",
        "actual_voice_speaker_sha256": designed_voice_speaker_sha256("warm_companion"),
    }
    payload.update(overrides)
    return payload


@pytest.mark.hermetic
def test_stepfun_provenance_is_accepted_for_the_designed_voice() -> None:
    submitted = ResponseProvenanceCreate.model_validate(_stepfun_provenance())

    assert _canonical_actual_voice(
        submitted=submitted,
        session={
            "interaction_mode": "companion",
            "mode_policy_version": "s2-v1",
            "companion_style_id": "starlight",
            "user_id": "account-1",
        },
        interaction_mode="companion",
    ) == (
        "warm_companion",
        None,
        "seed-tts-2.0",
        None,
        designed_voice_speaker_sha256("warm_companion"),
    )


@pytest.mark.hermetic
def test_sentence_list_on_playout_provenance_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResponseProvenanceCreate.model_validate(
            _stepfun_provenance(
                tts_sentences=[
                    {
                        "index": 0,
                        "engine": "stepfun",
                        "model": STEPFUN_TTS_MODEL,
                        "voice": "yuanqishaonv",
                        "switched": False,
                    }
                ]
            )
        )


@pytest.mark.hermetic
def test_stepfun_personal_voice_snapshot_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResponseProvenanceCreate.model_validate(
            _stepfun_provenance(
                actual_voice_resource_id="seed-icl-2.0",
                actual_voice_profile_version=1,
                actual_voice_provider_expires_at="2030-01-01T00:00:00+00:00",
            )
        )
