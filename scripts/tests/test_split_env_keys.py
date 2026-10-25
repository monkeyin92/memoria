"""The splitter routes every key the Volcengine path reads and refuses a stale one."""

from __future__ import annotations

import pytest
from scripts.split_production_env import _AGENT_EXTRA_KEYS, split_env
from services.agent.src.providers.asr_failover import ASRFailoverConfig
from services.agent.src.providers.sensevoice import SenseVoiceRescueConfig
from services.agent.src.providers.volc_asr import VolcASRConfig

_BASE = {"TTS_PROVIDER": "doubao", "ASR_PROVIDER": "funasr"}


def test_split_routes_every_declared_voice_key() -> None:
    """A key a config reads is a key the splitter delivers to the agent."""
    declared = VolcASRConfig.ENV_KEYS | ASRFailoverConfig.ENV_KEYS | SenseVoiceRescueConfig.ENV_KEYS
    missing = declared - _AGENT_EXTRA_KEYS
    assert not missing, f"splitter drops declared keys: {sorted(missing)}"

    _control, agent, _speaker, _edge = split_env({**_BASE, "VOLC_ASR_APP_ID": "app"})
    assert agent["VOLC_ASR_APP_ID"] == "app"


def test_split_rejects_replaced_keys() -> None:
    """A key whose meaning moved fails the split and names its replacement."""
    cases = {
        "ASR_FAILOVER_ENABLED": "ASR_PROVIDER",
        "VOLC_ASR_API_KEY": "VOLC_ASR_APP_ID",
        "DOUBAO_ASR_APP_ID": "VOLC_ASR_ACCESS_TOKEN",
    }
    for key, replacement in cases.items():
        with pytest.raises(ValueError, match=replacement):
            split_env({**_BASE, key: "stale"})


def test_split_rejects_asr_provider_none() -> None:
    with pytest.raises(ValueError, match="funasr or volc"):
        split_env({"TTS_PROVIDER": "doubao", "ASR_PROVIDER": "none"})


def test_split_carries_stepfun_enabled_for_the_rollback_image() -> None:
    """Production runs STEPFUN_TTS_ENABLED=true today. The key is inert under
    TTS_PROVIDER=doubao, and the previous image needs it in the agent env."""
    _control, agent, _speaker, _edge = split_env({**_BASE, "STEPFUN_TTS_ENABLED": "true"})
    assert agent["STEPFUN_TTS_ENABLED"] == "true"


def test_split_keeps_current_selectors() -> None:
    """doubao and funasr are the live values, so a current env still splits."""
    control, agent, _speaker, _edge = split_env(_BASE)
    assert control["TTS_PROVIDER"] == "doubao"
    assert agent["TTS_PROVIDER"] == "doubao"
    assert agent["ASR_PROVIDER"] == "funasr"


def test_stepfun_voice_still_routes_while_the_field_exists() -> None:
    """STEPFUN_TTS_VOICE is still an AgentSettings alias, so it routes.

    The StepFun fields stay until the approved Doubao smoke passes. Once they
    are deleted, this key moves to the retired set and the assertion flips to
    ``not in agent``.
    """
    _control, agent, _speaker, _edge = split_env({**_BASE, "STEPFUN_TTS_VOICE": "wenrounansheng"})
    assert agent["STEPFUN_TTS_VOICE"] == "wenrounansheng"


def test_split_carries_the_doubao_auth_latch_window() -> None:
    """The window is read by the pool, so the agent env has to carry it."""

    _control, agent, _speaker, _edge = split_env({**_BASE, "DOUBAO_TTS_AUTH_LATCH_OPEN_S": "90"})
    assert agent["DOUBAO_TTS_AUTH_LATCH_OPEN_S"] == "90"
