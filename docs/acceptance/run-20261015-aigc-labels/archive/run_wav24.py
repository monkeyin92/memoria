"""One-off Doubao 24 kHz WAV render used on the test server (2026-10-15).

Archived for provenance. Paths below match the docker volume layout from
`archive/README.md` (`/work/render_aigc_notices.py`, `/out/*.wav`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import os
import wave
from pathlib import Path

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig

_RENDER = Path("/work/render_aigc_notices.py")
_spec = importlib.util.spec_from_file_location("r", _RENDER)
assert _spec is not None and _spec.loader is not None
r = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r)

base = DoubaoTTSConfig.from_env({**os.environ, "DOUBAO_TTS_POOL_SIZE": "1"})
cfg = dataclasses.replace(
    base,
    speaker=r.VOICE_ID,
    sample_rate=24000,
    speech_rate=0,
    loudness_rate=0,
    pitch=0,
    style_control_enabled=False,
    instruction=None,
)
print(
    "speaker",
    cfg.speaker,
    "sr",
    cfg.sample_rate,
    "speech_rate",
    cfg.speech_rate,
    "loudness_rate",
    cfg.loudness_rate,
    "pitch",
    cfg.pitch,
    "resource",
    cfg.resource_id,
    "token_len",
    len(cfg.access_token or ""),
)


async def go(path: str, text: str, n: int) -> None:
    tts = DoubaoTTS(cfg)
    try:
        fence = GenerationFence("aigc-intro", n, 1, 0)
        tts.bind_fence(fence)
        tts.apply_speech_plan(
            emotion="neutral",
            rate=1.0,
            instruction="",
            pitch=0,
            fence=fence,
        )
        res = await tts.synthesize_stream_text([text], fence=fence)
        print(
            "voice",
            tts.current_voice,
            "task",
            res.task_id,
            "align",
            res.alignment_status,
            "discarded",
            res.discarded,
        )
    finally:
        await tts.pool.aclose()
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(res.pcm)
    print("wav", path, len(res.pcm))


for n, (lid, text) in enumerate(r.LINES, 1):
    asyncio.run(go(f"/out/{lid}-24k.wav", text, n))
