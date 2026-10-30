#!/usr/bin/env python3
"""Synthesize the approved fixed-safety clips in Doubao child-story female voice.

Every clip is saved as 24 kHz mono 16-bit integer PCM WAV, labelled with
GB 45438-2025 compliant RIFF INFO chunks (IART, ISFT, ICMT, ICRD, IENG), and
pinned in manifest.json. ``--template`` re-renders only the named clips.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.agent.src.contracts.ids import GenerationFence  # noqa: E402
from services.agent.src.providers.doubao_tts import (  # noqa: E402
    DoubaoTTS,
    DoubaoTTSConfig,
)
from services.agent.src.providers.fixed_safety_audio import (  # noqa: E402
    DEFAULT_FIXED_SAFETY_AUDIO_DIR,
    FIXED_SAFETY_AUDIO_ENGINE,
    FIXED_SAFETY_AUDIO_VOICE,
    approved_templates,
    verify_fixed_safety_audio_directory,
    write_manifest,
)


async def _synthesize_clip(text: str, dest_wav: Path) -> None:
    config = DoubaoTTSConfig.from_env(os.environ)
    config = DoubaoTTSConfig(
        app_id=config.app_id,
        access_token=config.access_token,
        resource_id=config.resource_id,
        ws_url=config.ws_url,
        speaker=FIXED_SAFETY_AUDIO_VOICE,
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
    fence = GenerationFence("safety_render", 1, 1, 0)
    result = await tts.synthesize_stream_text([text], fence=fence)
    pcm = result.pcm
    frame_bytes = 24000 * 2 * 20 // 1000  # 960 bytes per 20 ms frame
    rem = len(pcm) % frame_bytes
    if rem != 0:
        pcm += b"\x00" * (frame_bytes - rem)
    with wave.open(str(dest_wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(pcm)


def render_all(
    directory: Path,
    *,
    app_id: str,
    access_token: str,
    template_ids: tuple[str, ...] | None = None,
) -> None:
    os.environ["DOUBAO_TTS_APP_ID"] = app_id
    os.environ["DOUBAO_TTS_ACCESS_TOKEN"] = access_token
    directory.mkdir(parents=True, exist_ok=True)

    available = {template.template_id: template for template in approved_templates()}
    if template_ids:
        unknown = set(template_ids) - available.keys()
        if unknown:
            raise SystemExit(f"unknown fixed-safety templates: {', '.join(sorted(unknown))}")
        templates = [available[template_id] for template_id in template_ids]
    else:
        templates = list(available.values())
    for template in templates:
        wav_path = directory / f"{template.template_id}.wav"
        print(f"Synthesizing {template.template_id}: {template.spoken!r}")
        asyncio.run(_synthesize_clip(template.spoken, wav_path))
        print(f"  -> Saved {wav_path.relative_to(REPO)} ({wav_path.stat().st_size} bytes)")

    print("Writing manifest and labelling RIFF INFO...")
    manifest = write_manifest(
        directory,
        voice=FIXED_SAFETY_AUDIO_VOICE,
        engine=FIXED_SAFETY_AUDIO_ENGINE,
    )
    verify_fixed_safety_audio_directory(directory)

    for entry in manifest["templates"]:
        print(
            f"  {entry['template_id']}: audio_sha256={entry['audio_sha256'][:16]}... "
            f"pcm_sha256={entry['pcm_sha256'][:16]}..."
        )
    print(f"Successfully generated and verified {directory / 'manifest.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        default=DEFAULT_FIXED_SAFETY_AUDIO_DIR,
        help="output directory for fixed-safety audio clips",
    )
    parser.add_argument("--app-id", default=os.environ.get("DOUBAO_TTS_APP_ID", ""))
    parser.add_argument("--access-token", default=os.environ.get("DOUBAO_TTS_ACCESS_TOKEN", ""))
    parser.add_argument(
        "--template",
        action="append",
        default=[],
        help="re-render only this template id; repeat for more",
    )
    args = parser.parse_args()

    if not args.app_id or not args.access_token:
        print("error: DOUBAO_TTS_APP_ID and DOUBAO_TTS_ACCESS_TOKEN are required", file=sys.stderr)
        sys.exit(1)

    dest = Path(args.directory)
    if not dest.is_absolute():
        dest = REPO / dest
    render_all(
        dest,
        app_id=args.app_id,
        access_token=args.access_token,
        template_ids=tuple(args.template) or None,
    )


if __name__ == "__main__":
    main()
