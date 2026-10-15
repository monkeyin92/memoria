#!/usr/bin/env python3
"""Render spoken AIGC intro clips for offline story and bedtime playback.

Texts (also listed in docs/design/offline-lines-v2.md):

* aigc_story_intro: 「下面是 AI 合成的声音给你讲故事。」
* aigc_soothe_intro: 「AI 合成的声音陪你睡觉啦。」

Requires DOUBAO_TTS_APP_ID and DOUBAO_TTS_ACCESS_TOKEN. Voice
zh_female_tianmeitaozi_uranus_bigtts at normal speed and normal volume
(speech_rate / loudness_rate / pitch = 0). Synthesizes 24 kHz PCM (provider
requirement), then ffmpeg-encodes 16 kHz Opus with implicit AIGC OpusTags
(CONTENT_ID, SERVICE_PROVIDER=Volcengine Doubao, AI_GENERATED, ARTIST, VOICE_ID,
DATE, CONTENT_PRODUCER=Memoria).

Do not fabricate audio when credentials are missing.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import wave
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OFFLINE_ROOT = (
    REPO
    / "firmware"
    / "esp32"
    / "overlay"
    / "files"
    / "main"
    / "boards"
    / "memoria"
    / "esp-vocat"
    / "assets"
    / "offline_lines"
)
ARCHIVE_ROOT = REPO / "docs" / "acceptance" / "run-20261015-aigc-labels" / "archive"
VOICE_ID = "zh_female_tianmeitaozi_uranus_bigtts"
SERVICE_PROVIDER = "Volcengine Doubao"
CONTENT_PRODUCER = "Memoria"
DOUBAO_SAMPLE_RATE_HZ = 24_000
PACK_SAMPLE_RATE_HZ = 16_000

LINES = (
    ("aigc_story_intro", "下面是 AI 合成的声音给你讲故事。"),
    ("aigc_soothe_intro", "AI 合成的声音陪你睡觉啦。"),
)


def _have_doubao() -> bool:
    return bool(os.environ.get("DOUBAO_TTS_APP_ID") and os.environ.get("DOUBAO_TTS_ACCESS_TOKEN"))


def _ffmpeg_from_wav(wav: Path, ogg: Path, *, line_id: str, generated: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav),
            "-ar",
            str(PACK_SAMPLE_RATE_HZ),
            "-ac",
            "1",
            "-c:a",
            "libopus",
            "-b:a",
            "16000",
            "-metadata",
            f"TITLE={line_id}",
            "-metadata",
            f"CONTENT_ID={line_id}",
            "-metadata",
            f"SERVICE_PROVIDER={SERVICE_PROVIDER}",
            "-metadata",
            "AI_GENERATED=true",
            "-metadata",
            f"ARTIST={VOICE_ID}",
            "-metadata",
            f"VOICE_ID={VOICE_ID}",
            "-metadata",
            f"DATE={generated}",
            "-metadata",
            f"CONTENT_PRODUCER={CONTENT_PRODUCER}",
            str(ogg),
        ],
        check=True,
    )


async def _doubao_wav(path: Path, text: str, *, generation: int) -> str:
    """Synthesize 24 kHz PCM via the current DoubaoTTS API; return task_id."""

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig

    base = DoubaoTTSConfig.from_env({**os.environ, "DOUBAO_TTS_POOL_SIZE": "1"})
    config = dataclasses.replace(
        base,
        speaker=VOICE_ID,
        sample_rate=DOUBAO_SAMPLE_RATE_HZ,
        speech_rate=0,
        loudness_rate=0,
        pitch=0,
        style_control_enabled=False,
        instruction=None,
    )
    tts = DoubaoTTS(config)
    try:
        fence = GenerationFence("aigc-intro", generation, 1, 0)
        tts.bind_fence(fence)
        tts.apply_speech_plan(
            emotion="neutral",
            rate=1.0,
            instruction="",
            pitch=0,
            fence=fence,
        )
        result = await tts.synthesize_stream_text([text], fence=fence)
        if result.discarded or not result.pcm:
            raise RuntimeError(
                f"Doubao returned no PCM for {path.name} "
                f"(task={result.task_id!r} discarded={result.discarded})"
            )
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(DOUBAO_SAMPLE_RATE_HZ)
            handle.writeframes(result.pcm)
        return str(result.task_id or "")
    finally:
        await tts.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned lines and exit without calling TTS",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=OFFLINE_ROOT,
        help="directory for aigc_story_intro.ogg / aigc_soothe_intro.ogg",
    )
    args = parser.parse_args(argv)
    today = date.today().isoformat()
    if args.dry_run:
        for line_id, text in LINES:
            print(f"{line_id}: {text}")
        return 0
    if not _have_doubao():
        print(
            "TODO: DOUBAO_TTS_APP_ID / DOUBAO_TTS_ACCESS_TOKEN unset. "
            "Refusing to fabricate notice audio. Texts are in docs/design/offline-lines-v2.md.",
            file=sys.stderr,
        )
        for line_id, text in LINES:
            print(f"  {line_id}: {text}", file=sys.stderr)
        return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    for generation, (line_id, text) in enumerate(LINES, start=1):
        ogg = args.out_dir / f"{line_id}.ogg"
        wav24 = args.out_dir / f"{line_id}-24k.wav"
        try:
            task_id = asyncio.run(_doubao_wav(wav24, text, generation=generation))
            _ffmpeg_from_wav(wav24, ogg, line_id=line_id, generated=today)
        finally:
            # Keep the 24 kHz WAV beside the ogg for provenance when writing to
            # an archive/out dir; only delete the temp name under offline_lines.
            if args.out_dir.resolve() == OFFLINE_ROOT.resolve():
                wav24.unlink(missing_ok=True)
        records.append(
            {
                "id": line_id,
                "prompt": text,
                "tool": "doubao-seed-tts-2.0",
                "date": today,
                "voice_id": VOICE_ID,
                "service_provider": SERVICE_PROVIDER,
                "content_producer": CONTENT_PRODUCER,
                "doubao_sample_rate_hz": DOUBAO_SAMPLE_RATE_HZ,
                "pack_sample_rate_hz": PACK_SAMPLE_RATE_HZ,
                "task_id": task_id,
                "offline_ogg": str(ogg),
            }
        )
        print(f"wrote {ogg}")
    provenance = {
        "generated": today,
        "voice": {"id": VOICE_ID, "terms": "trial/demo only pending Volcengine terms"},
        "tool": "doubao-seed-tts-2.0",
        "service_provider": SERVICE_PROVIDER,
        "lines": records,
    }
    (ARCHIVE_ROOT / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
