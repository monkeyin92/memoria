from __future__ import annotations

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.prosody import cosyvoice_instruction
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
from services.agent.src.providers.funasr_stt import FunASRConfig
from services.agent.src.providers.qwen_voice_catalog import catalog_by_id
from services.common.voice_identity import TTS_MODEL, TTS_PROVIDER


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
            "FUNASR_WS_TRACE": "true",
        }
    )
    assert cfg.sample_rate == 8000
    assert cfg.language == "en"
    assert cfg.semantic_punctuation
    assert not cfg.heartbeat
    assert cfg.reconnect_audio_ms == 1200
    assert cfg.vocabulary_id == "vocab-control-commands"
    assert cfg.speech_noise_threshold == -0.1
    assert cfg.ws_trace
    assert cfg.vad_model is None


def test_funasr_config_defaults_to_fun_asr_and_validates_vad_model() -> None:
    cfg = FunASRConfig.from_env(
        {"DASHSCOPE_API_KEY": "key", "FUNASR_VAD_MODEL": "near_meeting_16k"}
    )
    assert cfg.model == "fun-asr-realtime"
    assert cfg.vad_model == "near_meeting_16k"
    with pytest.raises(ValueError, match="vad_model"):
        FunASRConfig.from_env({"FUNASR_VAD_MODEL": "near_field"})


def test_funasr_default_sentence_silence_matches_turn_endpointing() -> None:
    cfg = FunASRConfig.from_env({"DASHSCOPE_API_KEY": "key"})
    assert cfg.max_sentence_silence_ms == 550
    assert cfg.vocabulary_id is None
    assert cfg.speech_noise_threshold is None
    assert not cfg.ws_trace


@pytest.mark.parametrize("value", ["-1.1", "1.1", "nan"])
def test_funasr_rejects_invalid_speech_noise_threshold(value: str) -> None:
    with pytest.raises(ValueError, match="speech noise threshold"):
        FunASRConfig.from_env(
            {
                "DASHSCOPE_API_KEY": "key",
                "FUNASR_SPEECH_NOISE_THRESHOLD": value,
            }
        )


_CLONE = f"{TTS_MODEL}-owner01-3f9a2c"


def _tts() -> CosyVoiceTTS:
    return CosyVoiceTTS(CosyVoiceConfig(api_key="key", ws_url="wss://example", pool_size=0))


def test_tts_config_defaults_to_qwen_audio_31_and_prefers_mock_url() -> None:
    cfg = CosyVoiceConfig.from_env(
        {
            "DASHSCOPE_API_KEY": "key",
            "DASHSCOPE_WS_URL": "wss://real",
            "COSYVOICE_MOCK_WS_URL": "ws://mock",
            "COSYVOICE_SAMPLE_RATE": "16000",
            "COSYVOICE_RATE": "1.1",
            "COSYVOICE_VOLUME": "40",
            "COSYVOICE_WORD_TIMESTAMPS": "false",
            "COSYVOICE_POOL_SIZE": "2",
        }
    )
    assert cfg.model == TTS_MODEL
    assert cfg.voice == catalog_by_id()["warm_companion"].speaker_id == "longanyang_v3.1"
    assert cfg.ws_url == "ws://mock"
    assert cfg.sample_rate == 16000
    assert not cfg.word_timestamps
    assert cfg.pool_size == 2
    assert cfg.rate == 1.1


def test_tts_config_resolves_the_persona_profile_voice() -> None:
    cfg = CosyVoiceConfig.from_env(
        {"DASHSCOPE_API_KEY": "key", "COSYVOICE_VOICE_PROFILE": "soft_confidante"}
    )
    assert cfg.voice == "longwan_v3.1"
    assert cfg.voice_profile == "soft_confidante"


@pytest.mark.parametrize("pool_size", [-1, 4])
def test_tts_pool_must_stay_within_the_provider_rps_limit(pool_size: int) -> None:
    with pytest.raises(ValueError, match="3 RPS"):
        CosyVoiceConfig(api_key="key", ws_url="wss://example", pool_size=pool_size)


def test_tts_rejects_runaway_instruction() -> None:
    with pytest.raises(ValueError, match="too long"):
        CosyVoiceConfig(api_key="key", ws_url="wss://example", instruction="慢" * 201)


def test_tts_applies_a_freeform_generation_speech_plan() -> None:
    tts = _tts()

    tts.apply_speech_plan(emotion="sad", rate=0.95)

    assert tts.current_instruction == cosyvoice_instruction("sad", freeform=True)
    assert "你说话的情感是" not in (tts.current_instruction or "")
    assert tts.current_rate == 0.95
    assert tts.current_pitch == 0
    assert tts.current_context_texts == ()


def test_unknown_emotion_falls_back_to_neutral_instruction() -> None:
    tts = _tts()
    tts.apply_speech_plan(emotion="ecstatic", rate=2.0)
    assert tts.current_instruction == cosyvoice_instruction("neutral", freeform=True)
    assert tts.current_rate == 1.05


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"COSYVOICE_MODEL": "cosyvoice-v3.5-flash"}, "qwen-audio-3.1-tts-flash"),
        ({"COSYVOICE_VOICE": "longanyang"}, "approved persona voice"),
        ({"COSYVOICE_VOICE": _CLONE}, "approved persona voice"),
        ({"COSYVOICE_VOICE_PROFILE": "unknown_profile"}, "approved persona voice"),
    ],
    ids=["legacy-model", "legacy-voice", "clone-as-baseline", "unknown-profile"],
)
def test_production_rejects_anything_but_an_approved_persona_voice(
    env: dict[str, str], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        CosyVoiceConfig.from_env(
            {
                "ENVIRONMENT": "production",
                "DASHSCOPE_API_KEY": "key",
                "DASHSCOPE_WS_URL": "wss://dashscope.example",
                **env,
            }
        )


def test_production_accepts_the_approved_persona_voice() -> None:
    cfg = CosyVoiceConfig.from_env(
        {
            "ENVIRONMENT": "production",
            "DASHSCOPE_API_KEY": "key",
            "DASHSCOPE_WS_URL": "wss://dashscope.example",
            "COSYVOICE_VOICE_PROFILE": "calm_guide",
            "COSYVOICE_VOICE": "longanzhi_v3.1",
        }
    )
    assert cfg.voice == "longanzhi_v3.1"


def test_designed_voice_profile_must_be_an_approved_persona_voice() -> None:
    tts = _tts()
    tts.apply_voice_profile(
        model=TTS_MODEL,
        voice="longhua_v3.1",
        profile_id="bright_peer",
        provider=TTS_PROVIDER,
        voice_kind="designed",
        resource_id=TTS_MODEL,
    )
    assert tts.current_voice == "longhua_v3.1"
    assert tts.current_voice_kind == "designed"
    for bad in (
        {"voice": "longxiaoxia_v3.1"},  # system voice outside the persona catalog
        {"voice": "longhua_v3.1", "profile_id": "calm_guide"},
        {"model": "cosyvoice-v3.5-flash"},
        {"provider": "volcengine_doubao"},
    ):
        kwargs = {
            "model": TTS_MODEL,
            "voice": "longhua_v3.1",
            "profile_id": "bright_peer",
            "provider": TTS_PROVIDER,
            "voice_kind": "designed",
            **bad,
        }
        with pytest.raises(ValueError):
            tts.apply_voice_profile(**kwargs)  # type: ignore[arg-type]


def test_personal_voice_requires_a_current_model_clone() -> None:
    tts = _tts()
    tts.apply_voice_profile(
        model=TTS_MODEL,
        voice=_CLONE,
        profile_id="voice-profile-1",
        provider=TTS_PROVIDER,
        voice_kind="personal",
        resource_id=TTS_MODEL,
    )
    assert tts.current_voice_kind == "personal"
    assert tts.current_voice_profile_id == "voice-profile-1"
    for voice in (
        "cosyvoice-v3.5-flash-owner01-3f9a2c",  # a clone bound to the retired model
        "longanyang_v3.1",  # persona voices cannot pose as a personal clone
    ):
        with pytest.raises(ValueError, match="enrolled Qwen-Audio clone"):
            tts.apply_voice_profile(
                model=TTS_MODEL,
                voice=voice,
                profile_id="voice-profile-1",
                provider=TTS_PROVIDER,
                voice_kind="personal",
            )
    tts.use_baseline_voice()
    assert tts.current_voice == "longanyang_v3.1"
    assert tts.current_voice_kind == "designed"


def test_personal_clone_falls_back_to_the_configured_persona_voice() -> None:
    tts = _tts()
    reported: list[tuple[str, str, str, str]] = []
    tts.set_voice_fallback_callback(
        lambda _fence, profile, resource, voice, kind: reported.append(
            (profile, resource, voice, kind)
        )
    )
    tts.apply_voice_profile(
        model=TTS_MODEL,
        voice=_CLONE,
        profile_id="voice-profile-1",
        provider=TTS_PROVIDER,
        voice_kind="personal",
    )
    config = tts._config
    assert tts._fallback_config(config).voice == "longanyang_v3.1"

    tts.configure_personal_fallback(
        profile_id="low_magnetic",
        provider=TTS_PROVIDER,
        model=TTS_MODEL,
        resource_id=TTS_MODEL,
        voice="longsanshu_v3.1",
    )
    fallback = tts._fallback_config(config)
    assert (fallback.model, fallback.voice, fallback.voice_profile) == (
        TTS_MODEL,
        "longsanshu_v3.1",
        "low_magnetic",
    )
    tts._report_voice_fallback(GenerationFence("fallback", 1, 1, 0), fallback)
    assert reported == [("low_magnetic", TTS_MODEL, "longsanshu_v3.1", "designed")]

    tts.clear_personal_fallback()
    assert tts._fallback_config(config).voice == "longanyang_v3.1"
    with pytest.raises(ValueError, match="approved designed voice"):
        tts.configure_personal_fallback(
            profile_id="low_magnetic",
            provider=TTS_PROVIDER,
            model=TTS_MODEL,
            resource_id=TTS_MODEL,
            voice="longanyang_v3.1",
        )
