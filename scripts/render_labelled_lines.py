#!/usr/bin/env python3
"""Render llm_thinking / llm_unavailable clips with Appendix-E AIGC OpusTags.

With DOUBAO_TTS_APP_ID and DOUBAO_TTS_ACCESS_TOKEN set, synthesizes peach-voice
speech (zh_female_tianmeitaozi_uranus_bigtts). Without credentials, writes the
same AIGC-tagged scaffold tones used in-repo so CI and online playback stay
label-valid. Archives prompt/tool/date under
docs/acceptance/run-20261015-labelled-llm-state-audio/archive/.
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
ONLINE_ROOT = REPO / "services" / "common" / "assets" / "labelled_lines"
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
ARCHIVE_ROOT = REPO / "docs" / "acceptance" / "run-20261015-labelled-llm-state-audio" / "archive"
VOICE_ID = "zh_female_tianmeitaozi_uranus_bigtts"

LINES = (
    ("llm_thinking", "嗯，我想一想。", 1.1, 196.0),
    ("llm_unavailable", "我现在有点卡住了，等一会儿再来找我聊吧。", 2.4, 164.0),
)


def _ffmpeg_from_wav(wav: Path, ogg: Path, *, line_id: str, sample_rate: int) -> None:
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
            str(sample_rate),
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
            "SERVICE_PROVIDER=Memoria",
            "-metadata",
            "AI_GENERATED=true",
            "-metadata",
            f"ARTIST={VOICE_ID}",
            str(ogg),
        ],
        check=True,
    )


def _scaffold_wav(path: Path, *, duration: float, hz: float, sample_rate: int) -> None:
    filter_complex = (
        f"sine=frequency={hz}:duration={duration},volume=0.18[a];"
        f"sine=frequency={hz * 1.5}:duration={duration},volume=0.06[b];"
        f"[a][b]amix=inputs=2:duration=first,"
        f"afade=t=in:st=0:d=0.05,afade=t=out:st={max(duration - 0.08, 0.01)}:d=0.08"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            filter_complex,
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            str(path),
        ],
        check=True,
    )


async def _doubao_wav(path: Path, text: str, *, sample_rate: int) -> None:
    from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig

    config = DoubaoTTSConfig.from_env(os.environ)
    config = DoubaoTTSConfig(
        app_id=config.app_id,
        access_token=config.access_token,
        resource_id=config.resource_id,
        ws_url=config.ws_url,
        speaker=VOICE_ID,
        sample_rate=sample_rate,
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
            handle.setframerate(sample_rate)
            handle.writeframes(bytes(pcm))
    finally:
        await tts.aclose()


def _have_doubao() -> bool:
    return bool(os.environ.get("DOUBAO_TTS_APP_ID") and os.environ.get("DOUBAO_TTS_ACCESS_TOKEN"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scaffold-only",
        action="store_true",
        help="force cadence-tone scaffold even when Doubao credentials exist",
    )
    args = parser.parse_args(argv)
    ONLINE_ROOT.mkdir(parents=True, exist_ok=True)
    OFFLINE_ROOT.mkdir(parents=True, exist_ok=True)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    use_doubao = _have_doubao() and not args.scaffold_only
    tool = "doubao-seed-tts-2.0" if use_doubao else "ffmpeg-libopus-scaffold"
    today = date.today().isoformat()
    records: list[dict[str, object]] = []
    for line_id, text, _duration, _hz in LINES:
        online = ONLINE_ROOT / f"{line_id}.ogg"
        offline = OFFLINE_ROOT / f"{line_id}.ogg"
        pcm_sidecar = ONLINE_ROOT / f"{line_id}.s16le"
        record: dict[str, object] = {
            "id": line_id,
            "prompt": text,
            "tool": tool,
            "date": today,
            "voice_id": VOICE_ID,
            "playable": use_doubao,
            "online_sample_rate_hz": 24_000,
            "offline_sample_rate_hz": 16_000,
        }
        if not use_doubao:
            # Scaffold tones must not land on any play path or overwrite offline placeholders.
            records.append(record)
            print(f"skip audio for {line_id}: scaffold is not playable; need DOUBAO_TTS_*")
            continue
        tmp_online = ONLINE_ROOT / f".{line_id}-24k.wav"
        tmp_offline = ONLINE_ROOT / f".{line_id}-16k.wav"
        try:
            asyncio.run(_doubao_wav(tmp_online, text, sample_rate=24_000))
            asyncio.run(_doubao_wav(tmp_offline, text, sample_rate=16_000))
            _ffmpeg_from_wav(tmp_online, online, line_id=line_id, sample_rate=24_000)
            _ffmpeg_from_wav(tmp_offline, offline, line_id=line_id, sample_rate=16_000)
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(tmp_online),
                    "-f",
                    "s16le",
                    "-acodec",
                    "pcm_s16le",
                    "-ac",
                    "1",
                    "-ar",
                    "24000",
                    str(pcm_sidecar),
                ],
                check=True,
            )
        finally:
            tmp_online.unlink(missing_ok=True)
            tmp_offline.unlink(missing_ok=True)
        record["online_ogg"] = str(online.relative_to(REPO))
        record["online_pcm"] = str(pcm_sidecar.relative_to(REPO))
        record["offline_ogg"] = str(offline.relative_to(REPO))
        records.append(record)
        print(
            f"wrote {online.relative_to(REPO)}, "
            f"{pcm_sidecar.relative_to(REPO)}, "
            f"and {offline.relative_to(REPO)} via {tool}"
        )
    provenance = {
        "generated": today,
        "voice": {
            "id": VOICE_ID,
            "terms": "trial/demo only pending Volcengine terms",
        },
        "tool": tool,
        "status": "playable" if use_doubao else "no_playable_online_assets",
        "lines": records,
    }
    payload = json.dumps(provenance, ensure_ascii=False, indent=2) + "\n"
    (ONLINE_ROOT / "provenance.json").write_text(payload, encoding="utf-8")
    (ARCHIVE_ROOT / "provenance.json").write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
