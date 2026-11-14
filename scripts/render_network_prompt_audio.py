#!/usr/bin/env python3
"""Regenerate only network prompts using the production container's Doubao voice.

Run: python scripts/render_network_prompt_audio.py --remote memoria-prod
Verify without network/synthesis: python scripts/render_network_prompt_audio.py --check-only
Credentials stay inside the container; stdout transports only a WAV tar archive.
No firmware publication, production configuration or other audio is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from firmware.esp32.scripts.build_offline_stories import BOARD_ASSETS, OFFLINE_LINES  # noqa: E402
from services.common.aigc_opus import (  # noqa: E402
    CANONICAL_SERVICE_PROVIDER,
    aigc_tag_map,
    audio_packets_digest,
    implicit_label,
    require_aigc_tags,
)

from scripts.transcode_resynthesis_opus import encode_wav  # noqa: E402

VOICE = "zh_female_shaoergushi_uranus_bigtts"
IDS = ("offline-reconnecting-v1", "online-restored-v1", "online-restored-chat-v1")
CLIPS = {key: dict(OFFLINE_LINES)[key] for key in IDS}
ROOT = BOARD_ASSETS / "offline_lines"
MANIFEST = ROOT / "network_prompt_provenance.json"

# Execute with the deployed runtime and its environment, never exporting secrets.
REMOTE = r"""
import asyncio, dataclasses, hashlib, io, json, os, sys, tarfile, wave
from datetime import datetime, timezone
from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig
from services.agent.src.contracts.ids import GenerationFence
from services.common.aigc_opus import aigc_identity_from_env
async def main():
    config = DoubaoTTSConfig.from_env()
    if os.environ.get("TTS_PROVIDER") != "doubao":
        raise ValueError("production provider changed")
    if os.environ.get("MEMORIA_VOICE_PROFILE_ENABLED", "false").lower() != "false":
        raise ValueError("session voice resolution enabled; confirm effective voice first")
    if config.speaker != EXPECTED_VOICE or config.sample_rate != 24000:
        raise ValueError("production voice or sample rate changed")
    producer, propagator = aigc_identity_from_env()
    tts = DoubaoTTS(dataclasses.replace(config, pool_size=1))
    files = {}
    try:
        for clip_id, text in CLIPS.items():
            result = await tts.synthesize_stream_text([text], fence=GenerationFence("network-render", 1, 1, 0))
            if not result.pcm:
                raise ValueError("empty synthesis")
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(config.sample_rate)
                wav.writeframes(result.pcm)
            files[clip_id + ".wav"] = buf.getvalue()
    finally:
        await tts.aclose()
    metadata = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "voice_id": config.speaker, "voice_profile": config.voice_profile,
                "resource_id": config.resource_id, "sample_rate_hz": config.sample_rate,
                "speech_rate": config.speech_rate, "loudness_rate": config.loudness_rate,
                "pitch": config.pitch, "content_producer": producer,
                "content_propagator": propagator,
                "wav_sha256": {k:hashlib.sha256(v).hexdigest() for k,v in files.items()}}
    files["source.json"] = json.dumps(metadata).encode()
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for name, data in files.items():
            info=tarfile.TarInfo(name); info.size=len(data)
            archive.addfile(info, io.BytesIO(data))
try:
    asyncio.run(main())
except Exception as exc:
    print("network synthesis failed: " + type(exc).__name__, file=sys.stderr)
    sys.exit(1)
"""


def check(root: Path = ROOT) -> None:
    manifest = json.loads((root / MANIFEST.name).read_text())
    if manifest["source"]["voice_id"] != VOICE or set(manifest["clips"]) != set(IDS):
        raise ValueError("unexpected network prompt source")
    for clip_id, text in CLIPS.items():
        record = manifest["clips"][clip_id]
        blob = (root / f"{clip_id}.ogg").read_bytes()
        if record["text"] != text or hashlib.sha256(blob).hexdigest() != record["sha256"]:
            raise ValueError(f"{clip_id}: network prompt provenance mismatch")
        tags = require_aigc_tags(blob[:65536], content_id=clip_id, implicit=True)
        digest = audio_packets_digest(blob)
        if (
            digest != record["audio_packets_sha256"]
            or digest == record["previous_audio_packets_sha256"]
            or tags["voice_id"] != VOICE
            or tags["artist"] != VOICE
        ):
            raise ValueError(f"{clip_id}: network prompt provenance mismatch")
        # Opus decodes at 48 kHz; OpusHead stores the original 16 kHz input rate.
        at = blob.index(b"OpusHead")
        if blob[at + 9] != 1 or int.from_bytes(blob[at + 12 : at + 16], "little") != 16000:
            raise ValueError(f"{clip_id}: expected mono Opus with 16 kHz input")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(root / f"{clip_id}.ogg"), "-f", "null", "-"],
            check=True,
            capture_output=True,
        )


def render(remote: str, container: str) -> None:
    import shlex

    code = f"EXPECTED_VOICE={VOICE!r}\nCLIPS={CLIPS!r}\n" + REMOTE
    command = f"docker exec -i {shlex.quote(container)} /app/.venv/bin/python -"
    result = subprocess.run(
        ["ssh", remote, command], input=code.encode(), capture_output=True, check=False
    )
    if result.returncode:
        # Never forward provider exceptions or container stderr containing credentials.
        raise RuntimeError(f"remote synthesis failed (exit {result.returncode})")
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        files = {}
        for name in ["source.json", *(f"{key}.wav" for key in IDS)]:
            member = archive.extractfile(name)
            if member is None:
                raise ValueError(f"missing synthesis output {name}")
            files[name] = member.read()
    source = json.loads(files["source.json"])
    if source["voice_id"] != VOICE:
        raise ValueError("remote voice mismatch")
    records = {}
    with tempfile.TemporaryDirectory(prefix="memoria-network-") as temp:
        staged = Path(temp)
        for clip_id, text in CLIPS.items():
            original = (ROOT / f"{clip_id}.ogg").read_bytes()
            wav = staged / f"{clip_id}.wav"
            wav.write_bytes(files[wav.name])
            if hashlib.sha256(wav.read_bytes()).hexdigest() != source["wav_sha256"][wav.name]:
                raise ValueError("source WAV hash mismatch")
            tags = implicit_label(
                content_id=clip_id,
                aigc={
                    "SERVICE_PROVIDER": CANONICAL_SERVICE_PROVIDER,
                    "VOICE_ID": VOICE,
                    "ARTIST": VOICE,
                    "DATE": source["generated_at"][:10],
                },
                content_producer=source["content_producer"],
                content_propagator=source["content_propagator"],
            )
            ogg = staged / f"{clip_id}.ogg"
            encoded = encode_wav(wav, ogg, aigc=tags, title=clip_id)
            records[clip_id] = {
                "text": text,
                "sha256": hashlib.sha256(ogg.read_bytes()).hexdigest(),
                "audio_packets_sha256": encoded["audio_packets_sha256"],
                "previous_sha256": hashlib.sha256(original).hexdigest(),
                "previous_audio_packets_sha256": audio_packets_digest(original),
                "previous_voice_id": aigc_tag_map(original[:65536])["VOICE_ID"],
            }
        manifest = {
            "schema_version": 1,
            "source": source,
            "codec": "opus",
            "sample_rate_hz": 16000,
            "channels": 1,
            "bitrate_bps": 16000,
            "clips": records,
        }
        (staged / MANIFEST.name).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
        check(staged)
        for name in [MANIFEST.name, *(f"{key}.ogg" for key in IDS)]:
            (ROOT / name).write_bytes((staged / name).read_bytes())
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default="memoria-prod")
    parser.add_argument("--container", default="memoria-voice-core-media-bridge-1")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if args.check_only:
        check()
        print("network prompts: provenance, packets, voice, format and decoding passed")
    else:
        render(args.remote, args.container)


if __name__ == "__main__":
    main()
