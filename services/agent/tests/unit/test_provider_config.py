from __future__ import annotations

import json

import pytest
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig
from services.agent.src.providers.doubao_voice_catalog import catalog_by_id
from services.agent.src.providers.funasr_stt import FunASRConfig


def test_funasr_config_from_env() -> None:
    cfg = FunASRConfig.from_env(
        {
            "DASHSCOPE_API_KEY": "key",
            "DASHSCOPE_WS_URL": "wss://dashscope",
            "FUNASR_MODEL": "fun-asr-realtime",
            "FUNASR_SAMPLE_RATE": "8000",
            "FUNASR_LANGUAGE": "en",
            "FUNASR_CHUNK_MS": "100",
            "FUNASR_MAX_SENTENCE_SILENCE_MS": "700",
            "FUNASR_SEMANTIC_PUNCTUATION": "true",
            "FUNASR_HEARTBEAT": "false",
            "FUNASR_RECONNECT_AUDIO_MS": "1200",
            "FUNASR_CONNECT_TIMEOUT_S": "2",
            "FUNASR_RESULT_TIMEOUT_S": "4",
            "FUNASR_VOCABULARY_ID": "vocab-control-commands",
            "FUNASR_SPEECH_NOISE_THRESHOLD": "-0.1",
        }
    )
    assert cfg.sample_rate == 8000
    assert cfg.language == "en"
    assert cfg.semantic_punctuation
    assert not cfg.heartbeat
    assert cfg.reconnect_audio_ms == 1200
    assert cfg.vocabulary_id == "vocab-control-commands"
    assert cfg.speech_noise_threshold == -0.1


def test_funasr_default_sentence_silence_matches_turn_endpointing() -> None:
    cfg = FunASRConfig.from_env({"DASHSCOPE_API_KEY": "key"})
    assert cfg.max_sentence_silence_ms == 550
    assert cfg.vocabulary_id is None
    assert cfg.speech_noise_threshold is None


@pytest.mark.parametrize("value", ["-1.1", "1.1", "nan"])
def test_funasr_rejects_invalid_speech_noise_threshold(value: str) -> None:
    with pytest.raises(ValueError, match="speech noise threshold"):
        FunASRConfig.from_env(
            {
                "DASHSCOPE_API_KEY": "key",
                "FUNASR_SPEECH_NOISE_THRESHOLD": value,
            }
        )


def test_doubao_config_uses_approved_profile_and_old_console_auth() -> None:
    voice = catalog_by_id()["bright_peer"]
    config = DoubaoTTSConfig.from_env(
        {
            "DOUBAO_TTS_MOCK_WS_URL": "ws://mock",
            "DOUBAO_TTS_VOICE_PROFILE": "bright_peer",
            "DOUBAO_TTS_APP_ID": "app-test",
            "DOUBAO_TTS_ACCESS_TOKEN": "token-test",
        }
    )

    assert config.ws_url == "ws://mock"
    assert config.speaker == voice.speaker_id
    headers = config.auth_headers(connect_id="connect-test")
    assert headers["X-Api-App-Id"] == "app-test"
    assert headers["X-Api-Access-Key"] == "token-test"
    assert headers["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert "X-Api-Key" not in headers


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
def test_doubao_config_requires_exactly_one_complete_auth_mode(
    auth: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="exactly one complete authentication mode"):
        DoubaoTTSConfig.from_env(
            {
                "DOUBAO_TTS_VOICE_PROFILE": "warm_companion",
                **auth,
            }
        )


def test_doubao_speech_plan_keeps_alignment_safe_and_clamps_rate() -> None:
    config = DoubaoTTSConfig(
        api_key="test",
        speaker=catalog_by_id()["warm_companion"].speaker_id,
        instruction="不要保留这条指令",
        pool_size=0,
    )
    tts = DoubaoTTS(config)

    tts.apply_speech_plan(emotion="happy", rate=1.2)

    assert tts.current_instruction is None
    assert tts.current_rate == 1.05


def test_production_doubao_rejects_unapproved_explicit_voice() -> None:
    with pytest.raises(ValueError, match="approved companion voice"):
        DoubaoTTSConfig.from_env(
            {
                "ENVIRONMENT": "production",
                "DOUBAO_TTS_API_KEY": "test",
                "DOUBAO_TTS_VOICE_PROFILE": "warm_companion",
                "DOUBAO_TTS_SPEAKER": "unapproved-voice",
            }
        )


@pytest.mark.parametrize(
    "environment,variable,ws_url",
    [
        (
            "production",
            "DOUBAO_TTS_WS_URL",
            "ws://openspeech.bytedance.com/api/v3/tts/bidirection",
        ),
        (
            "production",
            "DOUBAO_TTS_WS_URL",
            "wss://user:password@openspeech.bytedance.com/api/v3/tts/bidirection",
        ),
        (
            "development",
            "DOUBAO_TTS_MOCK_WS_URL",
            "ws://mock/path#credentials",
        ),
    ],
)
def test_doubao_config_rejects_unsafe_websocket_urls(
    environment: str,
    variable: str,
    ws_url: str,
) -> None:
    env = {
        "ENVIRONMENT": environment,
        "DOUBAO_TTS_API_KEY": "test",
        "DOUBAO_TTS_VOICE_PROFILE": "warm_companion",
        variable: ws_url,
    }

    with pytest.raises(ValueError, match="Doubao TTS WebSocket URL"):
        DoubaoTTSConfig.from_env(env)


def test_cosyvoice_config_from_env_prefers_mock_url() -> None:
    cfg = CosyVoiceConfig.from_env(
        {
            "DASHSCOPE_API_KEY": "key",
            "DASHSCOPE_WS_URL": "wss://real",
            "COSYVOICE_MOCK_WS_URL": "ws://mock",
            "COSYVOICE_MODEL": "cosyvoice-v3-flash",
            "COSYVOICE_VOICE": "longanyang",
            "COSYVOICE_SAMPLE_RATE": "16000",
            "COSYVOICE_RATE": "1.1",
            "COSYVOICE_PITCH": "0.9",
            "COSYVOICE_VOLUME": "40",
            "COSYVOICE_WORD_TIMESTAMPS": "false",
            "COSYVOICE_POOL_SIZE": "2",
            "COSYVOICE_CONNECT_TIMEOUT_S": "3",
            "COSYVOICE_FIRST_AUDIO_TIMEOUT_S": "1",
            "COSYVOICE_TOTAL_TIMEOUT_S": "10",
        }
    )
    assert cfg.ws_url == "ws://mock"
    assert cfg.sample_rate == 16000
    assert not cfg.word_timestamps
    assert cfg.pool_size == 2
    assert cfg.rate == 1.1


def test_cosyvoice_applies_a_valid_generation_speech_plan() -> None:
    config = CosyVoiceConfig(
        api_key="key",
        ws_url="wss://example",
        volume=45,
        pool_size=0,
    )
    tts = CosyVoiceTTS(config)

    tts.apply_speech_plan(emotion="sad", rate=0.95)

    assert tts.current_instruction == "你正在进行闲聊互动，你说话的情感是sad。"
    assert tts.current_rate == 0.95
    assert config.volume == 45


def test_longanyang_rejects_free_form_instruction() -> None:
    with pytest.raises(ValueError, match="longanyang"):
        CosyVoiceConfig.from_env(
            {
                "DASHSCOPE_API_KEY": "key",
                "COSYVOICE_MODEL": "cosyvoice-v3-flash",
                "COSYVOICE_VOICE": "longanyang",
                "COSYVOICE_INSTRUCTION": "请自然一点，并适当拉长重点词。",
            }
        )


def test_v3_5_applies_freeform_speech_plan() -> None:
    tts = CosyVoiceTTS(
        CosyVoiceConfig(
            api_key="key",
            ws_url="wss://example",
            model="cosyvoice-v3.5-flash",
            voice="cosyvoice-v3.5-flash-vd-warmboy-demo",
            pool_size=0,
        )
    )
    tts.apply_speech_plan(emotion="happy", rate=1.0)
    assert tts.current_instruction is not None
    assert "轻松愉快" in tts.current_instruction
    assert "你说话的情感是" not in tts.current_instruction


def test_v3_5_from_env_requires_designed_voice(tmp_path, monkeypatch) -> None:
    empty_registry = tmp_path / "empty_voices.json"
    empty_registry.write_text('{"target_model":"cosyvoice-v3.5-flash","voices":{}}\n')
    monkeypatch.setenv("COSYVOICE_VOICE_REGISTRY", str(empty_registry))
    with pytest.raises(ValueError, match="designed voice"):
        CosyVoiceConfig.from_env(
            {
                "DASHSCOPE_API_KEY": "key",
                "COSYVOICE_MODEL": "cosyvoice-v3.5-flash",
                "COSYVOICE_VOICE": "",
                "COSYVOICE_VOICE_REGISTRY": str(empty_registry),
            }
        )


def test_v3_5_from_env_accepts_explicit_voice_id() -> None:
    cfg = CosyVoiceConfig.from_env(
        {
            "DASHSCOPE_API_KEY": "key",
            "COSYVOICE_MODEL": "cosyvoice-v3.5-flash",
            "COSYVOICE_VOICE": "cosyvoice-v3.5-flash-vd-warmboy-abc123",
        }
    )
    assert cfg.model == "cosyvoice-v3.5-flash"
    assert cfg.voice.endswith("abc123")
    assert cfg.uses_freeform_instruct


def test_production_rejects_an_unapproved_clone_as_the_baseline(tmp_path) -> None:
    approved = "cosyvoice-v3.5-flash-vd-warmboy-approved"
    registry = tmp_path / "voices.json"
    registry.write_text(
        json.dumps(
            {
                "target_model": "cosyvoice-v3.5-flash",
                "voices": {
                    "warm_companion": {
                        "voice_id": approved,
                        "target_model": "cosyvoice-v3.5-flash",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="approved designed baseline"):
        CosyVoiceConfig.from_env(
            {
                "ENVIRONMENT": "production",
                "DASHSCOPE_API_KEY": "key",
                "DASHSCOPE_WS_URL": "wss://dashscope.example",
                "COSYVOICE_MODEL": "cosyvoice-v3.5-flash",
                "COSYVOICE_VOICE_PROFILE": "warm_companion",
                "COSYVOICE_VOICE": "cosyvoice-v3.5-flash-clone-owner001",
                "COSYVOICE_VOICE_REGISTRY": str(registry),
            }
        )


def test_production_accepts_the_whitelisted_designed_baseline(tmp_path) -> None:
    approved = "cosyvoice-v3.5-flash-vd-warmboy-approved"
    registry = tmp_path / "voices.json"
    registry.write_text(
        json.dumps(
            {
                "target_model": "cosyvoice-v3.5-flash",
                "voices": {
                    "warm_companion": {
                        "voice_id": approved,
                        "target_model": "cosyvoice-v3.5-flash",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = CosyVoiceConfig.from_env(
        {
            "ENVIRONMENT": "production",
            "DASHSCOPE_API_KEY": "key",
            "DASHSCOPE_WS_URL": "wss://dashscope.example",
            "COSYVOICE_MODEL": "cosyvoice-v3.5-flash",
            "COSYVOICE_VOICE_PROFILE": "warm_companion",
            "COSYVOICE_VOICE": approved,
            "COSYVOICE_VOICE_REGISTRY": str(registry),
        }
    )

    assert config.voice == approved
