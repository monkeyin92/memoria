from __future__ import annotations

import io
import wave
from types import SimpleNamespace

import pytest
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig
from services.voice_profile import cosyvoice_preview as preview_module
from services.voice_profile.cosyvoice_preview import CosyVoicePreviewRenderer


def test_production_preview_baseline_resolves_from_approved_persona_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("COSYVOICE_MODEL", "qwen-audio-3.1-tts-flash")
    monkeypatch.setenv("COSYVOICE_VOICE_PROFILE", "warm_companion")
    monkeypatch.delenv("COSYVOICE_VOICE", raising=False)

    config = CosyVoiceConfig.from_env()

    assert config.model == "qwen-audio-3.1-tts-flash"
    assert config.voice == "longanyang_v3.1"


def test_production_preview_baseline_rejects_retired_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("COSYVOICE_MODEL", "cosyvoice-v3.5-flash")
    monkeypatch.setenv("COSYVOICE_VOICE_PROFILE", "warm_companion")
    monkeypatch.delenv("COSYVOICE_VOICE", raising=False)

    with pytest.raises(ValueError, match="qwen-audio-3.1-tts-flash"):
        CosyVoiceConfig.from_env()


def _install_tts_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, object]], list[bool]]:
    applied: list[dict[str, object]] = []
    closed: list[bool] = []

    class TTSStub:
        sample_rate = 24_000

        def apply_voice_profile(
            self,
            *,
            model: str,
            voice: str,
            profile_id: str | None = None,
            voice_kind: str | None = None,
        ) -> None:
            applied.append(
                {
                    "model": model,
                    "voice": voice,
                    "profile_id": profile_id,
                    "voice_kind": voice_kind,
                }
            )

        async def synthesize_stream_text(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(pcm=b"\x01\x00\x02\x00", discarded=False)

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        preview_module.CosyVoiceTTS,
        "from_env",
        staticmethod(lambda: TTSStub()),
    )
    return applied, closed


@pytest.mark.asyncio
async def test_preview_renders_candidate_pcm_as_browser_playable_wav(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied, closed = _install_tts_stub(monkeypatch)

    audio = await CosyVoicePreviewRenderer().render(
        text="试听候选声音",
        model="qwen-audio-3.1-tts-flash",
        voice_id="qwen-audio-3.1-tts-flash-owner01-abc123",
    )

    with wave.open(io.BytesIO(audio), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24_000)
        assert wav.readframes(2) == b"\x01\x00\x02\x00"
    assert applied == [
        {
            "model": "qwen-audio-3.1-tts-flash",
            "voice": "qwen-audio-3.1-tts-flash-owner01-abc123",
            "profile_id": "preview",
            "voice_kind": "personal",
        }
    ]
    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "voice_id",
    [
        "longanyang_v3.1",
        "longhua_v3.1",
        "longwan_v3.1",
        "longanzhi_v3.1",
        "longsanshu_v3.1",
    ],
)
async def test_preview_renders_persona_system_voices_as_designed(
    monkeypatch: pytest.MonkeyPatch,
    voice_id: str,
) -> None:
    applied, closed = _install_tts_stub(monkeypatch)

    await CosyVoicePreviewRenderer().render(
        text="试听人设声音",
        model="qwen-audio-3.1-tts-flash",
        voice_id=voice_id,
    )

    assert applied == [
        {
            "model": "qwen-audio-3.1-tts-flash",
            "voice": voice_id,
            "profile_id": None,
            "voice_kind": "designed",
        }
    ]
    assert closed == [True]
