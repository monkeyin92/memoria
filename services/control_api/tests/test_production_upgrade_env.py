from __future__ import annotations

from scripts.prepare_production_upgrade_env import prepare


def test_upgrade_env_is_valid_split_and_does_not_expose_storage_secrets_to_agent() -> None:
    legacy = {
        "ENVIRONMENT": "production",
        "DEPLOYMENT_PROFILE": "cn_self_hosted",
        "LLM_PROVIDER": "qwen",
        "PUBLIC_BASE_URL": "https://voice.example.com/memoria-api",
        "ALLOWED_ORIGINS": "https://voice.example.com",
        "LIVEKIT_URL": "wss://voice.example.com",
        "LIVEKIT_API_KEY": "livekit-key",
        "LIVEKIT_API_SECRET": "livekit-secret-material-that-is-long-enough",
        "DASHSCOPE_API_KEY": "dashscope-secret",
        "DASHSCOPE_WS_URL": "wss://dashscope.example/ws",
        "DASHSCOPE_COMPATIBLE_BASE_URL": "https://dashscope.example/v1",
        "DASHSCOPE_BASE_URL": "https://dashscope.example/v1",
        "FUNASR_MODEL": "fun-asr-realtime",
        "FUNASR_SAMPLE_RATE": "16000",
        "COSYVOICE_MODEL": "cosyvoice-v3.5-flash",
        "COSYVOICE_SAMPLE_RATE": "24000",
        "COSYVOICE_WORD_TIMESTAMPS": "true",
        "MEMORIA_AUTH_SECRET": "auth-secret-material-that-is-long-enough",
        "QWEN_OMNI_PLUS_VAD_THRESHOLD": "ignored-legacy-key",
    }
    postgres = {
        "MEMORIA_DB_APP_PASSWORD": "app-pass",
        "MEMORIA_DB_COMPILER_PASSWORD": "compiler-pass",
    }
    minio = {
        "MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY": "archive-access",
        "MEMORIA_ARCHIVE_OBJECT_SECRET_KEY": "archive-secret",
        "MEMORIA_VOICE_OBJECT_ACCESS_KEY": "voice-access",
        "MEMORIA_VOICE_OBJECT_SECRET_KEY": "voice-secret",
    }

    control, agent, speaker_model = prepare(
        legacy=legacy,
        postgres=postgres,
        minio=minio,
        release_tag="20260719-test",
    )

    assert control["MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY"] == "archive-access"
    assert control["MEMORIA_VOICE_OBJECT_ACCESS_KEY"] == "voice-access"
    assert agent["MEMORIA_ARCHIVE_SINK_ENABLED"] == "true"
    assert agent["MEMORIA_MEMORY_CONTEXT_ENABLED"] == "true"
    assert agent["MEMORIA_PERSONA_ENABLED"] == "true"
    assert agent["MEMORIA_VOICE_PROFILE_ENABLED"] == "true"
    assert "MEMORIA_ARCHIVE_OBJECT_SECRET_KEY" not in agent
    assert "MEMORIA_VOICE_OBJECT_SECRET_KEY" not in agent
    assert "QWEN_OMNI_PLUS_VAD_THRESHOLD" not in control
    assert "QWEN_OMNI_PLUS_VAD_THRESHOLD" not in agent
    assert speaker_model == {
        "MEMORIA_SPEAKER_MODEL_TOKEN": control["MEMORIA_SPEAKER_EMBEDDING_TOKEN"]
    }
