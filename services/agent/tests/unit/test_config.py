from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from services.agent.src.config import AgentSettings, load_settings
from services.agent.src.contracts.errors import ConfigValidationError


@pytest.fixture(autouse=True)
def mandatory_agent_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORIA_AGENT_HEARTBEAT_TOKEN", "heartbeat-token-material-32-characters")
    monkeypatch.setenv(
        "MEMORIA_INTERACTION_POLICY_TOKEN",
        "interaction-policy-material-32-characters",
    )
    monkeypatch.setenv(
        "MEMORIA_RESPONSE_PLAN_TOKEN",
        "response-plan-token-material-32-characters",
    )


def test_valid_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUNASR_SAMPLE_RATE", "16000")
    monkeypatch.setenv("VAD_MIN_SILENCE_DURATION_S", "0.30")
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "livekit_cloud")
    monkeypatch.setenv("LIVEKIT_ADAPTIVE_INTERRUPTION", "true")
    s = AgentSettings(_env_file=None)
    assert s.funasr_sample_rate == 16000
    assert s.funasr_context_enabled is False
    assert s.funasr_vocabulary_id == ""
    assert s.funasr_speech_noise_threshold is None
    assert s.doubao_tts_sample_rate == 24000
    assert s.tts_provider == "doubao"
    assert s.listener_cues_enabled is False
    assert s.listener_cue_playback == "main_track"
    assert s.listener_cue_aec_validated is False
    assert s.persona_enabled is False
    assert s.memory_context_enabled is False
    assert s.voice_profile_enabled is False
    assert s.response_plan_url.endswith("/v1/interaction/response-plan")
    assert s.response_plan_timeout_s == 0.8
    assert s.interrupt_semantic_enabled is True
    assert s.interrupt_semantic_model == "deepseek-v4-flash"
    assert s.interrupt_semantic_timeout_s == 1.2
    assert s.miniprogram_kws_enabled is False
    assert s.media_bridge_grpc_enabled is False
    assert s.media_bridge_mtls is False
    assert s.media_bridge_max_pending_audio_frames == 20
    assert s.media_bridge_go_shadow_enabled is False
    assert s.media_output_generation_timeout_s == 45.0
    assert s.media_owner_silence_timeout_s == 10.0
    assert s.media_slo_report_enabled is False
    assert s.media_slo_metrics_url == "http://agent:9090/"
    assert s.media_slo_report_interval_s == 30.0


def test_production_media_bridge_requires_mtls_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv("MEDIA_BRIDGE_GRPC_ENABLED", "true")
    monkeypatch.setenv("MEDIA_BRIDGE_MTLS", "false")

    with pytest.raises(ValidationError, match="media bridge requires MEDIA_BRIDGE_MTLS"):
        AgentSettings()


def test_production_media_slo_reporter_requires_scoped_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv("MEDIA_SLO_REPORT_ENABLED", "true")
    monkeypatch.setenv("MEDIA_SLO_REPORT_URL", "http://control-api:8000/v1/internal/media-runtime/slo")
    monkeypatch.setenv("MEDIA_SLO_REPORT_TOKEN", "too-short")

    with pytest.raises(ValidationError, match="media SLO reporter requires MEDIA_SLO_REPORT_TOKEN"):
        AgentSettings()


def test_production_reply_delivery_reporter_requires_scoped_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv("MEDIA_REPLY_DELIVERY_ENABLED", "true")
    monkeypatch.setenv(
        "MEDIA_REPLY_DELIVERY_URL",
        "http://control-api:8000/v1/internal/media-runtime/reply-delivery",
    )
    monkeypatch.setenv("MEDIA_REPLY_DELIVERY_TOKEN", "too-short")

    with pytest.raises(
        ValidationError,
        match="reply delivery reporter requires MEDIA_REPLY_DELIVERY_TOKEN",
    ):
        AgentSettings()


def test_media_bridge_limits_and_tls_paths_are_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MEDIA_BRIDGE_GRPC_ENABLED", "true")
    monkeypatch.setenv("MEDIA_BRIDGE_MTLS", "true")
    monkeypatch.setenv("MEDIA_BRIDGE_TLS_CERT_FILE", str(tmp_path / "server.crt"))
    monkeypatch.setenv("MEDIA_BRIDGE_TLS_KEY_FILE", str(tmp_path / "server.key"))
    monkeypatch.setenv("MEDIA_BRIDGE_CLIENT_CA_FILE", str(tmp_path / "client-ca.crt"))
    monkeypatch.setenv("MEDIA_BRIDGE_MAX_PENDING_AUDIO_FRAMES", "64")
    monkeypatch.setenv("MEDIA_BRIDGE_MAX_PENDING_MESSAGES", "256")
    monkeypatch.setenv("MEDIA_BRIDGE_GO_SHADOW_ENABLED", "true")

    settings = AgentSettings()

    assert settings.media_bridge_grpc_enabled is True
    assert settings.media_bridge_mtls is True
    assert settings.media_bridge_max_pending_audio_frames == 64
    assert settings.media_bridge_max_pending_messages == 256
    assert settings.media_bridge_go_shadow_enabled is True


def test_funasr_vocabulary_and_noise_threshold_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FUNASR_VOCABULARY_ID", "vocab-control-commands")
    monkeypatch.setenv("FUNASR_SPEECH_NOISE_THRESHOLD", "-0.1")

    settings = AgentSettings()

    assert settings.funasr_vocabulary_id == "vocab-control-commands"
    assert settings.funasr_speech_noise_threshold == pytest.approx(-0.1)


def test_miniprogram_kws_settings_keep_model_and_calibration_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIPROGRAM_KWS_ENABLED", "true")
    monkeypatch.setenv("MINIPROGRAM_KWS_MODEL_DIR", "/tmp/kws-model")
    monkeypatch.setenv("MINIPROGRAM_KWS_KEYWORDS_FILE", "/tmp/kws-keywords.txt")
    monkeypatch.setenv("MINIPROGRAM_KWS_MIN_CONFIDENCE", "0.72")

    settings = AgentSettings()

    assert settings.miniprogram_kws_enabled is True
    assert settings.miniprogram_kws_model_dir == "/tmp/kws-model"
    assert settings.miniprogram_kws_keywords_file == "/tmp/kws-keywords.txt"
    assert settings.miniprogram_kws_min_confidence == pytest.approx(0.72)


def test_interrupt_semantic_settings_are_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERRUPT_SEMANTIC_ENABLED", "false")
    monkeypatch.setenv("INTERRUPT_SEMANTIC_MODEL", "qwen-turbo")
    monkeypatch.setenv("INTERRUPT_SEMANTIC_TIMEOUT_S", "0.4")

    settings = AgentSettings()

    assert settings.interrupt_semantic_enabled is False
    assert settings.interrupt_semantic_model == "qwen-turbo"
    assert settings.interrupt_semantic_timeout_s == 0.4


def test_cn_self_hosted_forces_v1_mini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "cn_self_hosted")
    monkeypatch.setenv("LIVEKIT_TURN_DETECTOR_VERSION", "v1")
    s = AgentSettings()
    assert s.livekit_turn_detector_version == "v1-mini"


def test_rejects_bad_sample_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUNASR_SAMPLE_RATE", "8000")
    with pytest.raises(ValidationError):
        AgentSettings()


def test_rejects_deprecated_deepseek_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_FAST_MODEL", "deepseek-chat")
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
        "DOUBAO_TTS_API_KEY",
        "DOUBAO_TTS_APP_ID",
        "DOUBAO_TTS_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConfigValidationError, match="LIVEKIT_URL.*DASHSCOPE_WS_URL"):
        load_settings(require_keys=True)


@pytest.mark.parametrize(
    "auth",
    [
        {
            "DOUBAO_TTS_API_KEY": "api-key",
            "DOUBAO_TTS_APP_ID": "app-id",
            "DOUBAO_TTS_ACCESS_TOKEN": "access-token",
        },
        {"DOUBAO_TTS_APP_ID": "app-id"},
        {"DOUBAO_TTS_ACCESS_TOKEN": "access-token"},
    ],
)
def test_agent_settings_requires_one_complete_doubao_auth_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    auth: dict[str, str],
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "DOUBAO_TTS_API_KEY",
        "DOUBAO_TTS_APP_ID",
        "DOUBAO_TTS_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in auth.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match="exactly one complete authentication mode"):
        AgentSettings()


def test_bailian_deepseek_is_the_default_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-test-key")
    settings = AgentSettings()

    assert settings.llm_provider == "bailian_deepseek"
    assert settings.llm_api_key == "dashscope-test-key"
    assert settings.llm_base_url.endswith("/compatible-mode/v1")
    assert settings.llm_fast_model == "deepseek-v4-flash"
    assert settings.llm_deep_model == "deepseek-v4-flash"


def test_deepseek_configuration_remains_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-test-key")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DEEPSEEK_FAST_MODEL", "deepseek-v4-flash")
    settings = AgentSettings()

    assert settings.llm_provider == "deepseek"
    assert settings.llm_api_key == "deepseek-test-key"
    assert settings.llm_fast_model == "deepseek-v4-flash"


def test_direct_deepseek_key_does_not_override_bailian_deepseek_implicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "bailian_deepseek")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    settings = AgentSettings()

    assert settings.llm_provider == "bailian_deepseek"
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
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "doubao-test-key")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ConfigValidationError, match="DEEPSEEK_API_KEY"):
        load_settings(require_keys=True)


def test_production_archive_sink_requires_encryption_and_internal_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.delenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("MEMORIA_ARCHIVE_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("MEMORIA_ARCHIVE_SPOOL_KEY", raising=False)

    with pytest.raises(ValidationError, match="archive"):
        AgentSettings()


def test_production_agent_heartbeat_requires_independent_token() -> None:
    with pytest.raises(ValidationError, match="heartbeat"):
        AgentSettings(
            _env_file=None,
            ENVIRONMENT="production",
            LIVEKIT_URL="wss://test.livekit.cloud",
            MEMORIA_ARCHIVE_SINK_ENABLED=False,
            MEMORIA_AGENT_HEARTBEAT_TOKEN="",
        )


def test_production_interaction_policy_requires_independent_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.delenv("MEMORIA_INTERACTION_POLICY_TOKEN", raising=False)

    with pytest.raises(ValidationError, match="interaction policy"):
        AgentSettings()


def test_production_response_plan_requires_scoped_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.delenv("MEMORIA_RESPONSE_PLAN_TOKEN", raising=False)

    with pytest.raises(ValidationError, match="response plan requires a scoped token"):
        AgentSettings()


def test_production_response_plan_requires_secure_internal_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv(
        "MEMORIA_RESPONSE_PLAN_URL",
        "http://planner.internal.example/v1/interaction/response-plan",
    )

    with pytest.raises(ValidationError, match="response plan URL requires HTTPS"):
        AgentSettings()


def test_production_response_plan_token_is_independent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    shared = "shared-policy-plan-token-material-32-characters"
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv("MEMORIA_INTERACTION_POLICY_TOKEN", shared)
    monkeypatch.setenv("MEMORIA_RESPONSE_PLAN_TOKEN", shared)

    with pytest.raises(ValidationError, match="capability tokens must be independent"):
        AgentSettings()


def test_production_formal_speaker_authority_requires_independent_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv("MEMORIA_SPEAKER_AUTHORITY_ENABLED", "true")
    monkeypatch.delenv("MEMORIA_SPEAKER_INTERNAL_TOKEN", raising=False)

    with pytest.raises(ValidationError, match="speaker authority"):
        AgentSettings()


def test_production_persona_requires_internal_auth_even_without_archive_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv("MEMORIA_PERSONA_ENABLED", "true")
    monkeypatch.delenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("MEMORIA_PERSONA_READ_TOKEN", raising=False)

    with pytest.raises(ValidationError, match="persona"):
        AgentSettings()


def test_production_memory_context_requires_internal_auth_even_without_archive_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv("MEMORIA_MEMORY_CONTEXT_ENABLED", "true")
    monkeypatch.delenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("MEMORIA_MEMORY_READ_TOKEN", raising=False)

    with pytest.raises(ValidationError, match="memory context"):
        AgentSettings()


def test_production_voice_profile_requires_internal_auth_even_without_archive_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv("MEMORIA_VOICE_PROFILE_ENABLED", "true")
    monkeypatch.delenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("MEMORIA_VOICE_RESOLUTION_TOKEN", raising=False)

    with pytest.raises(ValidationError, match="voice profile"):
        AgentSettings()


def test_production_plaintext_internal_url_is_limited_to_local_docker_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv("MEMORIA_MEMORY_CONTEXT_ENABLED", "true")
    monkeypatch.setenv("MEMORIA_MEMORY_READ_TOKEN", "memory-read-material-that-is-long-enough")
    monkeypatch.setenv(
        "MEMORIA_MEMORY_CONTEXT_URL",
        "http://memory.internal.example/v1/archive/session-context",
    )

    with pytest.raises(ValidationError, match="HTTPS or local Docker DNS"):
        AgentSettings()


@pytest.mark.parametrize(
    "ws_url",
    [
        "ws://openspeech.bytedance.com/api/v3/tts/bidirection",
        "wss://user:password@openspeech.bytedance.com/api/v3/tts/bidirection",
        "wss://openspeech.bytedance.com/api/v3/tts/bidirection#credentials",
    ],
)
def test_production_rejects_unsafe_doubao_websocket_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ws_url: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv("DOUBAO_TTS_WS_URL", ws_url)

    with pytest.raises(ValidationError, match="DOUBAO_TTS_WS_URL"):
        AgentSettings()


def test_production_enabled_capabilities_require_independent_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = "shared-capability-material-that-is-long-enough"
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("MEMORIA_ARCHIVE_SINK_ENABLED", "false")
    monkeypatch.setenv("MEMORIA_MEMORY_CONTEXT_ENABLED", "true")
    monkeypatch.setenv("MEMORIA_PERSONA_ENABLED", "true")
    monkeypatch.setenv("MEMORIA_MEMORY_READ_TOKEN", shared)
    monkeypatch.setenv("MEMORIA_PERSONA_READ_TOKEN", shared)

    with pytest.raises(ValidationError, match="capability tokens must be independent"):
        AgentSettings()
