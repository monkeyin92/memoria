#!/usr/bin/env python3
"""Synthesize and encode prompt audio clips in Doubao child-story female voice.

Covers:
1. wo_zai_ne.ogg (Wake prompt sound on board: "我在呢。")
2. offline_lines/llm_thinking.ogg (Board thinking filler: "嗯，我想一想。")
3. services/common/assets/labelled_lines/llm_thinking.{ogg,s16le} (Online thinking filler)
4. offline_lines/llm_unavailable.ogg (Board unavailable notice: "我现在有点卡住了，等一会儿再来找我聊吧。")
5. services/common/assets/labelled_lines/llm_unavailable.{ogg,s16le} (Online unavailable notice)

Every clip is encoded with GB 45438-2025 implicit AIGC OpusTags:
CONTENT_ID, SERVICE_PROVIDER, AI_GENERATED, ARTIST, VOICE_ID, DATE,
CONTENT_PRODUCER, CONTENT_PROPAGATOR, LABEL=1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import wave
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.common.aigc_opus import (  # noqa: E402
    CANONICAL_SERVICE_PROVIDER,
    aigc_identity_from_env,
    audio_packets_digest,
    implicit_label,
    require_aigc_tags,
    rewrite_opus_comments,
)

BOARD_ASSETS_ROOT = (
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
)
ONLINE_LABELLED_ROOT = REPO / "services" / "common" / "assets" / "labelled_lines"
VOICE_ID = "zh_female_shaoergushi_uranus_bigtts"
CONTENT_PRODUCER, CONTENT_PROPAGATOR = aigc_identity_from_env()

# (clip_id, text, board_rel_path, is_online_labelled)
PROMPT_CLIPS = (
    ("wo_zai_ne", "我在呢。", Path("wo_zai_ne.ogg"), False),
    ("llm_thinking", "嗯，我想一想。", Path("offline_lines") / "llm_thinking.ogg", True),
    (
        "llm_unavailable",
        "我现在有点卡住了，等一会儿再来找我聊吧。",
        Path("offline_lines") / "llm_unavailable.ogg",
        True,
    ),
)


def _encode_board_opus(
    wav_path: Path, ogg_path: Path, *, content_id: str, title: str, today: str
) -> None:
    """Encode 16 kHz mono 16 kbps Opus with GB 45438-2025 Vorbis comments."""
    ogg_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav_path),
            "-map_metadata",
            "-1",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            "16000",
            "-vbr",
            "off",
            "-application",
            "voip",
            str(ogg_path),
        ],
        check=True,
    )
    aigc = implicit_label(
        content_id=content_id,
        aigc={
            "SERVICE_PROVIDER": CANONICAL_SERVICE_PROVIDER,
            "ARTIST": VOICE_ID,
            "VOICE_ID": VOICE_ID,
            "DATE": today,
        },
        content_producer=CONTENT_PRODUCER,
        content_propagator=CONTENT_PROPAGATOR,
    )
    comments = {"TITLE": title, **dict(aigc)}
    raw = ogg_path.read_bytes()
    tagged = rewrite_opus_comments(raw, aigc=comments)
    if audio_packets_digest(raw) != audio_packets_digest(tagged):
        raise RuntimeError(f"{ogg_path.name}: tag rewrite changed audio packets")
    require_aigc_tags(tagged[:65536], content_id=content_id, implicit=True)
    ogg_path.write_bytes(tagged)


def _encode_online_assets(
    wav_24k_path: Path,
    ogg_path: Path,
    pcm_path: Path,
    *,
    content_id: str,
    title: str,
    today: str,
) -> None:
    """Encode 24 kHz mono 16 kbps Opus and s16le PCM sidecar for server catalogue."""
    ogg_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav_24k_path),
            "-map_metadata",
            "-1",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "libopus",
            "-b:a",
            "16000",
            "-vbr",
            "off",
            str(ogg_path),
        ],
        check=True,
    )
    aigc = implicit_label(
        content_id=content_id,
        aigc={
            "SERVICE_PROVIDER": CANONICAL_SERVICE_PROVIDER,
            "ARTIST": VOICE_ID,
            "VOICE_ID": VOICE_ID,
            "DATE": today,
        },
        content_producer=CONTENT_PRODUCER,
        content_propagator=CONTENT_PROPAGATOR,
    )
    comments = {"TITLE": title, **dict(aigc)}
    raw = ogg_path.read_bytes()
    tagged = rewrite_opus_comments(raw, aigc=comments)
    if audio_packets_digest(raw) != audio_packets_digest(tagged):
        raise RuntimeError(f"{ogg_path.name}: tag rewrite changed audio packets")
    require_aigc_tags(tagged[:65536], content_id=content_id, implicit=True)
    ogg_path.write_bytes(tagged)

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav_24k_path),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            "24000",
            str(pcm_path),
        ],
        check=True,
    )


async def _synthesize_doubao_wav(text: str, dest_wav: Path) -> None:
    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig

    config = DoubaoTTSConfig.from_env(os.environ)
    config = DoubaoTTSConfig(
        app_id=config.app_id,
        access_token=config.access_token,
        resource_id=config.resource_id,
        ws_url=config.ws_url,
        speaker=VOICE_ID,
        sample_rate=24000,
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
    fence = GenerationFence("render", 1, 1, 0)
    result = await tts.synthesize_stream_text([text], fence=fence)
    with wave.open(str(dest_wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(result.pcm)


def synthesize_all(app_id: str, access_token: str) -> None:
    os.environ["DOUBAO_TTS_APP_ID"] = app_id
    os.environ["DOUBAO_TTS_ACCESS_TOKEN"] = access_token
    today = date.today().isoformat()
    online_records = []

    for content_id, text, board_rel, is_online in PROMPT_CLIPS:
        print(f"Synthesizing {content_id} ({text})...")
        tmp_wav = REPO / f".tmp_{content_id}_24k.wav"
        try:
            asyncio.run(_synthesize_doubao_wav(text, tmp_wav))
            board_dest = BOARD_ASSETS_ROOT / board_rel
            _encode_board_opus(tmp_wav, board_dest, content_id=content_id, title=text, today=today)
            print(f"  -> Wrote board clip: {board_dest.relative_to(REPO)}")

            if is_online:
                online_ogg = ONLINE_LABELLED_ROOT / f"{content_id}.ogg"
                online_pcm = ONLINE_LABELLED_ROOT / f"{content_id}.s16le"
                _encode_online_assets(
                    tmp_wav,
                    online_ogg,
                    online_pcm,
                    content_id=content_id,
                    title=text,
                    today=today,
                )
                print(f"  -> Wrote online clip: {online_ogg.relative_to(REPO)}")
                online_records.append(
                    {
                        "id": content_id,
                        "prompt": text,
                        "tool": "doubao-seed-tts-2.0",
                        "date": today,
                        "voice_id": VOICE_ID,
                        "content_producer": CONTENT_PRODUCER,
                        "content_propagator": CONTENT_PROPAGATOR,
                        "label": "1",
                        "playable": True,
                        "online_sample_rate_hz": 24000,
                        "offline_sample_rate_hz": 16000,
                        "online_ogg": str(online_ogg.relative_to(REPO)),
                        "online_pcm": str(online_pcm.relative_to(REPO)),
                        "offline_ogg": str(board_dest.relative_to(REPO)),
                    }
                )
        finally:
            tmp_wav.unlink(missing_ok=True)

    if online_records:
        provenance = {
            "generated": today,
            "voice": {
                "id": VOICE_ID,
                "terms": "trial/demo only pending Volcengine terms",
            },
            "tool": "doubao-seed-tts-2.0",
            "status": "playable",
            "lines": online_records,
        }
        prov_path = ONLINE_LABELLED_ROOT / "provenance.json"
        prov_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Updated {prov_path.relative_to(REPO)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", default=os.environ.get("DOUBAO_TTS_APP_ID", ""))
    parser.add_argument("--access-token", default=os.environ.get("DOUBAO_TTS_ACCESS_TOKEN", ""))
    args = parser.parse_args()
    if not args.app_id or not args.access_token:
        print("error: DOUBAO_TTS_APP_ID and DOUBAO_TTS_ACCESS_TOKEN are required", file=sys.stderr)
        sys.exit(1)
    synthesize_all(args.app_id, args.access_token)


if __name__ == "__main__":
    main()
