from __future__ import annotations

import io
import wave
from types import SimpleNamespace

import pytest
from services.voice_profile import cosyvoice_preview as preview_module
from services.voice_profile.cosyvoice_preview import CosyVoicePreviewRenderer


@pytest.mark.asyncio
async def test_preview_renders_candidate_pcm_as_browser_playable_wav(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied: list[tuple[str, str]] = []
    closed = False

    class TTSStub:
        sample_rate = 24_000

        def apply_voice_profile(self, *, model: str, voice: str) -> None:
            applied.append((model, voice))

        async def synthesize_stream_text(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(pcm=b"\x01\x00\x02\x00", discarded=False)

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        preview_module.CosyVoiceTTS,
        "from_env",
        staticmethod(lambda: TTSStub()),
    )

    audio = await CosyVoicePreviewRenderer().render(
        text="试听候选声音",
        model="cosyvoice-v3.5-flash",
        voice_id="cosyvoice-v3.5-flash-clone-owner",
    )

    with wave.open(io.BytesIO(audio), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24_000)
        assert wav.readframes(2) == b"\x01\x00\x02\x00"
    assert applied == [
        ("cosyvoice-v3.5-flash", "cosyvoice-v3.5-flash-clone-owner")
    ]
    assert closed
