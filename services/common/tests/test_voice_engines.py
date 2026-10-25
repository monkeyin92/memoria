"""Contract tests for voice_engines.py: Literals are shared, wire format round-trips, stale keys rejected."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from services.agent.src.config import AgentSettings
from services.common.voice_engines import (
    ASR_ENGINES,
    TTS_ENGINES,
    VoiceEngines,
    reject_replaced_keys,
)
from services.control_api.app.config_fields.providers import ProviderFields


@pytest.fixture(autouse=True)
def mandatory_agent_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the agent tokens required by AgentSettings."""
    monkeypatch.setenv("MEMORIA_AGENT_HEARTBEAT_TOKEN", "heartbeat-token-material-32-characters")
    monkeypatch.setenv(
        "MEMORIA_INTERACTION_POLICY_TOKEN",
        "interaction-policy-material-32-characters",
    )
    monkeypatch.setenv(
        "MEMORIA_RESPONSE_PLAN_TOKEN",
        "response-plan-token-material-32-characters",
    )


def test_settings_literals_are_identical() -> None:
    """AgentSettings and ProviderFields import the same Literal objects."""
    agent_tts = AgentSettings.model_fields["tts_provider"].annotation
    control_tts = ProviderFields.model_fields["tts_provider"].annotation
    assert agent_tts is control_tts

    agent_asr = AgentSettings.model_fields["asr_provider"].annotation
    control_asr = ProviderFields.model_fields["asr_provider"].annotation
    assert agent_asr is control_asr


def test_smoke_line_round_trip() -> None:
    """parse_smoke_line(e.smoke_line()) == e for every valid engine pair."""
    for asr in ASR_ENGINES:
        for tts in TTS_ENGINES:
            engines = VoiceEngines(asr=asr, tts=tts)
            line = engines.smoke_line()
            parsed = VoiceEngines.parse_smoke_line(line)
            assert parsed == engines


def test_smoke_line_rejects_unknown_engine() -> None:
    """An unknown engine value raises ValueError."""
    with pytest.raises(ValueError, match="unknown ASR engine"):
        VoiceEngines.parse_smoke_line("voice_engines asr=unknown tts=doubao")
    with pytest.raises(ValueError, match="unknown TTS engine"):
        VoiceEngines.parse_smoke_line("voice_engines asr=funasr tts=unknown")


def test_smoke_line_rejects_extra_field() -> None:
    """An extra field in the smoke line raises ValueError."""
    with pytest.raises(ValueError, match="unexpected fields"):
        VoiceEngines.parse_smoke_line("voice_engines asr=funasr extra=value")


def test_reject_replaced_keys_names_each() -> None:
    """Every key in REPLACED_ENV_KEYS produces a ValueError naming the key and replacement."""
    # Test one DOUBAO_ASR_ prefixed key
    env = {"DOUBAO_ASR_APP_ID": "xyz"}
    with pytest.raises(ValueError, match="DOUBAO_ASR_APP_ID") as exc_info:
        reject_replaced_keys(env)
    assert "Volc ASR uses VOLC_ASR_APP_ID + VOLC_ASR_ACCESS_TOKEN" in str(exc_info.value)

    # Test ASR_PROVIDER=none (special value match)
    env = {"ASR_PROVIDER": "none"}
    with pytest.raises(ValueError, match="ASR_PROVIDER=none") as exc_info:
        reject_replaced_keys(env)
    assert "no longer means anything" in str(exc_info.value)

    # Test a few other replaced keys
    for key, reason_fragment in [
        ("ASR_FAILOVER_ENABLED", "use ASR_PROVIDER"),
        ("ASR_PRIMARY", "implied by ASR_PROVIDER"),
    ]:
        env = {key: "true"}
        with pytest.raises(ValueError, match=key) as exc_info:
            reject_replaced_keys(env)
        assert reason_fragment in str(exc_info.value)


def test_from_env_matches_the_settings_defaults() -> None:
    """A short-lived container that omits a selector reports what services build."""

    assert VoiceEngines.from_env({}) == VoiceEngines(
        asr=AgentSettings(_env_file=None).asr_provider,
        tts=AgentSettings(_env_file=None).tts_provider,
    )
    assert VoiceEngines.from_env({"ASR_PROVIDER": " VOLC "}).asr == "volc"


def test_from_env_refuses_an_engine_no_process_can_build() -> None:
    with pytest.raises(ValueError, match="ASR_PROVIDER"):
        VoiceEngines.from_env({"ASR_PROVIDER": "sensevoice"})
    with pytest.raises(ValueError, match="TTS_PROVIDER"):
        VoiceEngines.from_env({"TTS_PROVIDER": "stepfun_primary"})


def test_reject_replaced_keys_passes_when_absent() -> None:
    """A mapping with no replaced keys returns None."""
    env = {"TTS_PROVIDER": "doubao", "ASR_PROVIDER": "funasr"}
    reject_replaced_keys(env)  # Should not raise


def test_stepfun_enabled_survives_the_split_for_rollback() -> None:
    """The live operator env sets it true, and the previous image reads it.

    Rejecting it would fail the first release after the switch, and retiring it
    would drop the key an env-level rollback needs.
    """
    reject_replaced_keys({"TTS_PROVIDER": "doubao", "STEPFUN_TTS_ENABLED": "true"})


def test_a_retired_selector_value_fails_before_the_service_starts() -> None:
    """The live agent env still says stepfun_primary, which no longer validates.

    A release that copied it through would only fail at import time in the
    container, after the cutover.
    """
    with pytest.raises(ValueError, match="TTS_PROVIDER=doubao"):
        reject_replaced_keys({"TTS_PROVIDER": "stepfun_primary"})

    # The same key with a live value and other casing of the retired one.
    reject_replaced_keys({"TTS_PROVIDER": "doubao"})
    with pytest.raises(ValueError, match="stepfun_primary"):
        reject_replaced_keys({"TTS_PROVIDER": " StepFun_Primary "})


def test_asr_provider_defaults_to_funasr(monkeypatch: pytest.MonkeyPatch) -> None:
    """AgentSettings with no env has asr_provider == funasr and tts_provider == doubao."""
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.delenv("TTS_PROVIDER", raising=False)
    settings = AgentSettings(_env_file=None)
    assert settings.asr_provider == "funasr"
    assert settings.tts_provider == "doubao"


def test_retired_engine_values_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value outside the shared literal fails validation in both services.

    The error names the env key, which is what an operator reads. Pydantic
    rejects a Literal mismatch on its own, so no extra validator is needed.
    """
    from services.control_api.app.config import ControlSettings

    monkeypatch.setenv("TTS_PROVIDER", "stepfun_primary")
    with pytest.raises(ValidationError, match="TTS_PROVIDER"):
        AgentSettings(_env_file=None)

    monkeypatch.setenv("TTS_PROVIDER", "cosyvoice")
    with pytest.raises(ValidationError, match="TTS_PROVIDER"):
        ControlSettings(_env_file=None)

    monkeypatch.setenv("TTS_PROVIDER", "doubao")
    monkeypatch.setenv("ASR_PROVIDER", "none")
    with pytest.raises(ValidationError, match="ASR_PROVIDER"):
        ControlSettings(_env_file=None)
