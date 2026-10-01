"""Server-side half of the soak voice bank: speak the test user's lines in production Doubao voices.

Run inside the Voice Core bridge container so the provider key never leaves the server (the local half
is ``scripts/voice_soak_bank.py``, which builds the item list and decodes the output)::

    SAMPLE_CFG_B64=<base64 json> docker exec -i -e SAMPLE_CFG_B64 <bridge> /app/.venv/bin/python - < scripts/voice_soak_bank_render.py

Config: ``{"items": [{"id": "hello", "voice": "bright_peer", "text": "...", "rate": 1.0, "instruction": "..."}]}``.
One JSON line per item on stdout: ``{"id", "voice", "text", "seconds", "wav_b64"}`` or ``{"id", "error"}``.
Nothing is stored and no conversation data is read; only the lab's own sentences are sent.
"""

import asyncio
import base64
import io
import json
import os
import re
import time
import wave

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig
from services.agent.src.providers.doubao_voice_catalog import DOUBAO_TTS_MODEL, catalog_by_id

cfg = json.loads(base64.b64decode(os.environ["SAMPLE_CFG_B64"]))
tts_config = DoubaoTTSConfig.from_env({**os.environ, "DOUBAO_TTS_POOL_SIZE": "1"})
tts = DoubaoTTS(tts_config)


def wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def sentences(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"(?<=[。！？!?；;\n])", text) if part.strip()]
    return parts or [text]


async def main() -> None:
    for index, item in enumerate(cfg["items"], 1):
        spec = catalog_by_id()[item["voice"]]
        tts.apply_voice_profile(
            model=DOUBAO_TTS_MODEL,
            resource_id=DOUBAO_TTS_MODEL,
            voice=spec.speaker_id,
            profile_id=spec.profile_id,
            provider="volcengine_doubao",
            voice_kind="designed",
        )
        fence = GenerationFence(f"bank-{item['id']}", index, index, 0)
        tts.bind_fence(fence)
        tts.apply_speech_plan(
            emotion="neutral",
            rate=float(item.get("rate", 1.0)),
            instruction=item.get("instruction", ""),
            pitch=int(item.get("pitch", 0)),
            reference_contexts=(),
            fence=fence,
        )
        started = time.time()
        try:
            result = await tts.synthesize_stream_text(sentences(item["text"]), fence=fence)
        except Exception as exc:  # keep the batch going; the caller sees the error row
            print(json.dumps({"id": item["id"], "error": repr(exc)[:200]}, ensure_ascii=False), flush=True)
            continue
        print(
            json.dumps(
                {
                    "id": item["id"],
                    "voice": item["voice"],
                    "text": item["text"],
                    "tts_s": round(time.time() - started, 2),
                    "seconds": round(len(result.pcm) / 2 / tts_config.sample_rate, 2),
                    "wav_b64": base64.b64encode(wav_bytes(result.pcm, tts_config.sample_rate)).decode(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    await tts.pool.aclose()


asyncio.run(main())
