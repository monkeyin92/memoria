from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from services.agent.src.config import AgentSettings, load_settings
from services.agent.src.contracts.errors import ConfigValidationError


def test_valid_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUNASR_SAMPLE_RATE", "16000")
    monkeypatch.setenv("COSYVOICE_SAMPLE_RATE", "24000")
    monkeypatch.setenv("COSYVOICE_WORD_TIMESTAMPS", "true")
    monkeypatch.setenv("VAD_MIN_SILENCE_DURATION_S", "0.30")
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "livekit_cloud")
    monkeypatch.setenv("LIVEKIT_ADAPTIVE_INTERRUPTION", "true")
    s = AgentSettings()
    assert s.funasr_sample_rate == 16000
    assert s.cosyvoice_sample_rate == 24000
    assert s.listener_cues_enabled is False
    assert s.listener_cue_aec_validated is False


def test_cn_self_hosted_forces_v1_mini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "cn_self_hosted")
    monkeypatch.setenv("LIVEKIT_TURN_DETECTOR_VERSION", "v1")
    monkeypatch.setenv("COSYVOICE_WORD_TIMESTAMPS", "true")
    s = AgentSettings()
    assert s.livekit_turn_detector_version == "v1-mini"


def test_rejects_bad_sample_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUNASR_SAMPLE_RATE", "8000")
    with pytest.raises(ValidationError):
        AgentSettings()


def test_rejects_deprecated_deepseek_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_FAST_MODEL", "deepseek-chat")
    monkeypatch.setenv("COSYVOICE_WORD_TIMESTAMPS", "true")
    with pytest.raises(ValidationError):
        AgentSettings()


def test_online_settings_require_provider_endpoints_and_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFFLINE_MOCK", "false")
    for name in (
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_WS_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConfigValidationError, match="LIVEKIT_URL.*DASHSCOPE_WS_URL"):
        load_settings(require_keys=True)


def test_dashscope_qwen_is_the_default_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-test-key")
    settings = AgentSettings()

    assert settings.llm_provider == "qwen"
    assert settings.llm_api_key == "dashscope-test-key"
    assert settings.llm_base_url.endswith("/compatible-mode/v1")
    assert settings.llm_fast_model == "qwen-turbo"
    assert settings.llm_deep_model == "qwen-plus"


def test_deepseek_configuration_remains_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-test-key")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DEEPSEEK_FAST_MODEL", "deepseek-v4-flash")
    settings = AgentSettings()

    assert settings.llm_provider == "deepseek"
    assert settings.llm_api_key == "deepseek-test-key"
    assert settings.llm_fast_model == "deepseek-v4-flash"


def test_deepseek_key_does_not_override_qwen_implicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    settings = AgentSettings()

    assert settings.llm_provider == "qwen"
    assert settings.llm_api_key == "dashscope-test-key"


def test_explicit_deepseek_requires_its_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("OFFLINE_MOCK", "false")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "test-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test-secret")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-test-key")
    monkeypatch.setenv("DASHSCOPE_WS_URL", "wss://dashscope")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ConfigValidationError, match="DEEPSEEK_API_KEY"):
        load_settings(require_keys=True)
