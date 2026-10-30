#!/usr/bin/env python3
"""Add the implicit AIGC RIFF INFO label to the fixed-safety clips.

The PCM ``data`` chunk is copied byte for byte.  The script rewrites each WAV
with ``LIST/INFO`` fields (IART/ISFT/ICMT/ICRD/IENG) and regenerates
``manifest.json`` with the post-label file hash plus the unchanged PCM hash.

Usage::

    uv run python scripts/label_fixed_safety_wavs.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from services.agent.src.providers.fixed_safety_audio import (
    DEFAULT_FIXED_SAFETY_AUDIO_DIR,
    FIXED_SAFETY_AUDIO_VOICE,
    approved_templates,
    verify_fixed_safety_audio_directory,
    write_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        default=DEFAULT_FIXED_SAFETY_AUDIO_DIR,
        help="directory holding the approved WAVs",
    )
    parser.add_argument(
        "--synthesized-on",
        default=None,
        help="override the synthesis date for every clip; default keeps each clip's date",
    )
    parser.add_argument(
        "--voice",
        default=FIXED_SAFETY_AUDIO_VOICE,
        help="reviewed StepFun voice id",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate the current files without rewriting them",
    )
    args = parser.parse_args(argv)
    directory = Path(args.directory)
    missing = [
        template.template_id
        for template in approved_templates()
        if not (directory / f"{template.template_id}.wav").is_file()
    ]
    if missing:
        sys.stderr.write(f"missing fixed-safety WAVs: {', '.join(missing)}\n")
        return 1
    if args.check_only:
        verify_fixed_safety_audio_directory(directory)
        print(f"fixed safety audio OK: {directory}")
        return 0
    manifest = write_manifest(
        directory,
        voice=args.voice,
        synthesized_on=args.synthesized_on,
    )
    verify_fixed_safety_audio_directory(directory)
    for entry in manifest["templates"]:
        print(
            f"{entry['template_id']}: audio_sha256={entry['audio_sha256']} "
            f"pcm_sha256={entry['pcm_sha256']}"
        )
    print(f"wrote {directory / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
