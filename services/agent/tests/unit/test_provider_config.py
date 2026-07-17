from __future__ import annotations

import pytest
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
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
        }
    )
    assert cfg.sample_rate == 8000
    assert cfg.language == "en"
    assert cfg.semantic_punctuation
    assert not cfg.heartbeat
    assert cfg.reconnect_audio_ms == 1200


def test_funasr_default_sentence_silence_matches_turn_endpointing() -> None:
    cfg = FunASRConfig.from_env({"DASHSCOPE_API_KEY": "key"})
    assert cfg.max_sentence_silence_ms == 550


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
    tts = CosyVoiceTTS(
        CosyVoiceConfig(api_key="key", ws_url="wss://example", pool_size=0)
    )

    tts.apply_speech_plan(emotion="sad", rate=0.95)

    assert tts.current_instruction == "你正在进行闲聊互动，你说话的情感是sad。"
    assert tts.current_rate == 0.95


def test_longanyang_rejects_free_form_instruction() -> None:
    with pytest.raises(ValueError, match="longanyang"):
        CosyVoiceConfig.from_env(
            {
                "DASHSCOPE_API_KEY": "key",
                "COSYVOICE_VOICE": "longanyang",
                "COSYVOICE_INSTRUCTION": "请自然一点，并适当拉长重点词。",
            }
        )
