#!/usr/bin/env python3
"""Render spoken AIGC notice clips for offline story and bedtime playback.

Texts (also listed in docs/design/offline-lines-v2.md):

* aigc-notice-story: 「下面是 AI 合成的声音给你讲故事。」
* aigc-notice-bedtime: 「AI 合成的声音陪你睡觉啦。」

Requires DOUBAO_TTS_APP_ID and DOUBAO_TTS_ACCESS_TOKEN. Voice
zh_female_tianmeitaozi_uranus_bigtts at normal speed and normal volume
(no speech_rate / loudness override). Writes implicit AIGC OpusTags
(CONTENT_ID, SERVICE_PROVIDER, AI_GENERATED, ARTIST, DATE, CONTENT_PRODUCER).

TODO: No Doubao credentials in this environment. Do not fabricate audio.
      Run this script when credentials are available, then copy the oggs into
      firmware/.../assets/offline_lines/ and re-run build_offline_stories.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
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
CONTENT_PRODUCER = "Memoria"

LINES = (
    ("aigc-notice-story", "下面是 AI 合成的声音给你讲故事。"),
    ("aigc-notice-bedtime", "AI 合成的声音陪你睡觉啦。"),
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
            "16000",
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
            f"SERVICE_PROVIDER={CONTENT_PRODUCER}",
            "-metadata",
            "AI_GENERATED=true",
            "-metadata",
            f"ARTIST={VOICE_ID}",
            "-metadata",
            f"DATE={generated}",
            "-metadata",
            f"CONTENT_PRODUCER={CONTENT_PRODUCER}",
            str(ogg),
        ],
        check=True,
    )


async def _doubao_wav(path: Path, text: str) -> None:
    from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig

    config = DoubaoTTSConfig.from_env(os.environ)
    # Normal speed and normal volume: keep provider defaults for speech_rate
    # and loudness_rate (no speed-up, no attenuation).
    config = DoubaoTTSConfig(
        app_id=config.app_id,
        access_token=config.access_token,
        resource_id=config.resource_id,
        ws_url=config.ws_url,
        speaker=VOICE_ID,
        sample_rate=16_000,
        speech_rate=config.speech_rate,
        loudness_rate=config.loudness_rate,
        pitch=config.pitch,
        pool_size=1,
        connect_timeout_s=config.connect_timeout_s,
        first_audio_timeout_s=config.first_audio_timeout_s,
        total_timeout_s=config.total_timeout_s,
        hard_deadline_s=config.hard_deadline_s,
    )
    tts = DoubaoTTS(config)
    await tts.start()
    try:
        pcm = bytearray()
        async for chunk in tts.synthesize_stream_text(text):
            pcm.extend(chunk)
        import wave

        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            handle.writeframes(bytes(pcm))
    finally:
        await tts.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned lines and exit without calling TTS",
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
    OFFLINE_ROOT.mkdir(parents=True, exist_ok=True)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    for line_id, text in LINES:
        ogg = OFFLINE_ROOT / f"{line_id}.ogg"
        tmp = OFFLINE_ROOT / f".{line_id}-16k.wav"
        try:
            asyncio.run(_doubao_wav(tmp, text))
            _ffmpeg_from_wav(tmp, ogg, line_id=line_id, generated=today)
        finally:
            tmp.unlink(missing_ok=True)
        records.append(
            {
                "id": line_id,
                "prompt": text,
                "tool": "doubao-seed-tts-2.0",
                "date": today,
                "voice_id": VOICE_ID,
                "content_producer": CONTENT_PRODUCER,
                "offline_ogg": str(ogg.relative_to(REPO)),
            }
        )
        print(f"wrote {ogg.relative_to(REPO)}")
    provenance = {
        "generated": today,
        "voice": {"id": VOICE_ID, "terms": "trial/demo only pending Volcengine terms"},
        "tool": "doubao-seed-tts-2.0",
        "lines": records,
    }
    (ARCHIVE_ROOT / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
