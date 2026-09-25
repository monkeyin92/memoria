"""On-demand CosyVoice preview renderer; preview audio is not persisted."""

from __future__ import annotations

import io
import uuid
import wave

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.cosyvoice_tts import CosyVoiceTTS
from services.voice_profile.domain import VoicePreviewUnavailableError


class CosyVoicePreviewRenderer:
    async def render(
        self,
        *,
        text: str,
        model: str | None,
        voice_id: str | None,
    ) -> bytes:
        if not text.strip() or len(text) > 120:
            raise ValueError("voice preview text must contain 1..120 characters")
        if (model is None) != (voice_id is None):
            raise ValueError("voice preview model and voice_id must be supplied together")
        tts: CosyVoiceTTS | None = None
        try:
            tts = CosyVoiceTTS.from_env()
            if model is not None and voice_id is not None:
                tts.apply_voice_profile(model=model, voice=voice_id)
            result = await tts.synthesize_stream_text(
                [text.strip()],
                fence=GenerationFence(
                    session_id=f"voice-preview-{uuid.uuid4()}",
                    turn_id=0,
                    generation_id=0,
                    tool_epoch=0,
                ),
            )
            if result.discarded or not result.pcm:
                raise RuntimeError("CosyVoice returned no preview audio")
            return _pcm16_mono_wav(result.pcm, sample_rate=tts.sample_rate)
        except ValueError:
            raise
        except Exception as exc:
            raise VoicePreviewUnavailableError("voice preview synthesis is unavailable") from exc
        finally:
            if tts is not None:
                await tts.aclose()


class UnavailableVoicePreviewRenderer:
    async def render(
        self,
        *,
        text: str,
        model: str | None,
        voice_id: str | None,
    ) -> bytes:
        _ = (text, model, voice_id)
        raise VoicePreviewUnavailableError("voice preview provider is not configured")


def _pcm16_mono_wav(pcm: bytes, *, sample_rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()
