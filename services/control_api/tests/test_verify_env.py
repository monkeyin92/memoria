from __future__ import annotations

from typing import Any

import pytest
from scripts import verify_env


def _online_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OFFLINE_MOCK", "false")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "test-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test-livekit-material")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    monkeypatch.setenv("DASHSCOPE_WS_URL", "wss://dashscope.example.com/realtime")
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "test-doubao-key")
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-test-a")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


def test_qwen_provider_does_not_require_deepseek(monkeypatch: pytest.MonkeyPatch) -> None:
    _online_env(monkeypatch)
    errors, offline, _ = verify_env._validate_environment()
    assert errors == []
    assert offline is False


def test_deepseek_provider_requires_explicit_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _online_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-test-a")
    errors, _, _ = verify_env._validate_environment()
    assert errors == ["missing required env: DEEPSEEK_API_KEY"]


def test_environment_reuses_agent_settings_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    _online_env(monkeypatch)
    monkeypatch.setenv("LISTENER_CUE_PLAYBACK", "side-track")

    errors, _, _ = verify_env._validate_environment()

    assert "LISTENER_CUE_PLAYBACK must be main_track or background" in errors


def test_environment_rejects_invalid_turn_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    _online_env(monkeypatch)
    monkeypatch.setenv("ENDPOINTING_MIN_DELAY_S", "2.20")
    monkeypatch.setenv("ENDPOINTING_MAX_DELAY_S", "1.50")

    errors, _, _ = verify_env._validate_environment()

    assert errors == ["ENDPOINTING_MIN_DELAY_S must be <= ENDPOINTING_MAX_DELAY_S"]


def test_mark_uses_selected_llm_provider_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-test-a")
    captured: dict[str, Any] = {}

    def fake_request(url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        captured.update(url=url, **kwargs)
        return 200, {"status": "marked"}

    monkeypatch.setattr(verify_env, "_request_json", fake_request)
    assert verify_env._mark_smokes_passed("http://127.0.0.1:8000") is True
    assert captured["payload"]["llm_provider"] == "deepseek"
    assert captured["payload"]["release_tag"] == "release-test-a"
    assert captured["payload"]["tts"] == {
        "provider": "doubao",
        "audio": True,
        "word_timestamps": True,
    }
    assert captured["authorization"] == "test-auth-material-that-is-long-enough"
