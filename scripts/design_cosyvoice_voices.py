#!/usr/bin/env python3
"""Create CosyVoice v3.5 designed voices from the Memoria catalog.

Requires:
  DASHSCOPE_API_KEY
  Optional: DASHSCOPE_VOICE_DESIGN_URL or DASHSCOPE_WORKSPACE_ID

Usage:
  python scripts/design_cosyvoice_voices.py
  python scripts/design_cosyvoice_voices.py --profiles warm_companion soft_confidante
  python scripts/design_cosyvoice_voices.py --dry-run

Writes voice_ids to infra/voices/designed_voice_ids.json (gitignored recommended
for local overrides; a committed template can point production at shared IDs).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.agent.src.providers.cosyvoice_voice_catalog import (  # noqa: E402
    DEFAULT_DESIGN_TARGET_MODEL,
    VOICE_DESIGN_CATALOG,
    catalog_by_id,
    default_registry_path,
)


def _design_url() -> str:
    explicit = os.environ.get("DASHSCOPE_VOICE_DESIGN_URL")
    if explicit:
        return explicit.rstrip("/")
    workspace = os.environ.get("DASHSCOPE_WORKSPACE_ID") or os.environ.get("WORKSPACE_ID")
    if workspace:
        return (
            f"https://{workspace}.cn-beijing.maas.aliyuncs.com"
            "/api/v1/services/audio/tts/customization"
        )
    # Legacy DashScope host (Beijing).
    return "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"


def create_designed_voice(
    *,
    api_key: str,
    target_model: str,
    voice_prompt: str,
    preview_text: str,
    prefix: str,
) -> dict:
    body = {
        "model": "voice-enrollment",
        "input": {
            "action": "create_voice",
            "target_model": target_model,
            "voice_prompt": voice_prompt,
            "preview_text": preview_text,
            "prefix": prefix,
            "language_hints": ["zh"],
        },
        "parameters": {
            "sample_rate": 24000,
            "response_format": "wav",
        },
    }
    req = urllib.request.Request(
        _design_url(),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"voice design HTTP {exc.code}: {detail}") from exc
    output = payload.get("output") or {}
    voice_id = output.get("voice_id")
    if not voice_id:
        raise RuntimeError(f"voice design missing voice_id: {payload}")
    return {
        "voice_id": voice_id,
        "target_model": output.get("target_model") or target_model,
        "preview_audio_b64": (output.get("preview_audio") or {}).get("data"),
        "raw": payload,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profiles",
        nargs="*",
        default=None,
        help="Profile ids to create (default: all catalog entries)",
    )
    parser.add_argument(
        "--target-model",
        default=os.environ.get("COSYVOICE_MODEL", DEFAULT_DESIGN_TARGET_MODEL),
        help="Must match later COSYVOICE_MODEL (default cosyvoice-v3.5-flash)",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=default_registry_path(),
        help="Output registry JSON path",
    )
    parser.add_argument(
        "--preview-dir",
        type=Path,
        default=ROOT / "infra" / "voices" / "previews",
        help="Where to store preview wav files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print prompts only; do not call the API",
    )
    args = parser.parse_args()

    catalog = catalog_by_id()
    profile_ids = args.profiles or [s.profile_id for s in VOICE_DESIGN_CATALOG]
    for pid in profile_ids:
        if pid not in catalog:
            print(f"unknown profile: {pid}", file=sys.stderr)
            return 2

    if args.dry_run:
        for pid in profile_ids:
            spec = catalog[pid]
            print(f"=== {pid} ({spec.display_name}) prefix={spec.prefix}")
            print(spec.voice_prompt)
            print()
        return 0

    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("DASHSCOPE_API_KEY is required", file=sys.stderr)
        return 2

    if "v3.5" not in args.target_model:
        print(
            f"warning: target_model={args.target_model} is not a v3.5 model; "
            "designed voices for v3.5 should use cosyvoice-v3.5-flash/plus",
            file=sys.stderr,
        )

    registry: dict = {"target_model": args.target_model, "voices": {}, "updated_at": None}
    if args.registry.is_file():
        try:
            registry = json.loads(args.registry.read_text(encoding="utf-8"))
            registry.setdefault("voices", {})
        except json.JSONDecodeError:
            pass

    args.preview_dir.mkdir(parents=True, exist_ok=True)
    args.registry.parent.mkdir(parents=True, exist_ok=True)

    for pid in profile_ids:
        spec = catalog[pid]
        print(f"creating {pid} → target={args.target_model} prefix={spec.prefix} ...")
        result = create_designed_voice(
            api_key=api_key,
            target_model=args.target_model,
            voice_prompt=spec.voice_prompt,
            preview_text=spec.preview_text,
            prefix=spec.prefix,
        )
        voice_id = result["voice_id"]
        registry["voices"][pid] = {
            "voice_id": voice_id,
            "display_name": spec.display_name,
            "prefix": spec.prefix,
            "voice_prompt": spec.voice_prompt,
            "target_model": result["target_model"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        preview_b64 = result.get("preview_audio_b64")
        if preview_b64:
            wav_path = args.preview_dir / f"{pid}.wav"
            wav_path.write_bytes(base64.b64decode(preview_b64))
            print(f"  voice_id={voice_id}")
            print(f"  preview={wav_path}")
        else:
            print(f"  voice_id={voice_id} (no preview audio in response)")
        # Small pause between creates to be polite to the API.
        time.sleep(0.5)

    registry["target_model"] = args.target_model
    registry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.registry.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nregistry written: {args.registry}")
    print("Next:")
    print(f"  export COSYVOICE_MODEL={args.target_model}")
    print("  export COSYVOICE_VOICE_PROFILE=warm_companion   # or another profile")
    print("  # optional explicit id:")
    default = registry["voices"].get("warm_companion", {}).get("voice_id")
    if default:
        print(f"  # export COSYVOICE_VOICE={default}")
    print("  python scripts/provider_smoke_test.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
