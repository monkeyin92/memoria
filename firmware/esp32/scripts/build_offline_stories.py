#!/usr/bin/env python3
"""Pack offline story placeholders into the board assets directory.

Product recordings replace the ffmpeg sine placeholders later. Stories are
Opus 16 kbps mono 16 kHz Ogg files under boards/memoria/esp-vocat/assets/stories/.
The firmware assets partition remains 8 MiB; this script refuses to pack when
the board assets tree would leave less than 0.5 MiB free relative to that budget
(after other mascot/prompt files already present).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BOARD_ASSETS = (
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
STORIES_DIR = BOARD_ASSETS / "stories"
ASSETS_PARTITION_BYTES = 8 * 1024 * 1024
MIN_FREE_MARGIN_BYTES = 512 * 1024

STORIES = (
    ("story_01", "小星星的枕头", 392),
    ("story_02", "月亮不着急", 349),
    ("story_03", "一只会听的熊", 294),
)


def _ffmpeg_placeholder(path: Path, *, seconds: float, freq: int, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            "-c:a",
            "libopus",
            "-b:a",
            "16k",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(path),
        ],
        check=True,
    )
    meta = path.with_suffix(".ffmeta")
    meta.write_text(f";FFMETADATA1\ntitle={title}\n", encoding="utf-8")
    tmp = path.with_suffix(".tmp.ogg")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-i",
            str(meta),
            "-map_metadata",
            "1",
            "-c:a",
            "copy",
            str(tmp),
        ],
        check=True,
    )
    meta.unlink(missing_ok=True)
    tmp.replace(path)


def _tree_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=8.0, help="placeholder length")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)

    if not args.check_only:
        for story_id, _title, freq in STORIES:
            _ffmpeg_placeholder(
                STORIES_DIR / f"{story_id}.ogg",
                seconds=args.seconds,
                freq=freq,
                title=f"{story_id}_placeholder",
            )
        # Wake/night prompts live next to mascots, not under stories/.
        _ffmpeg_placeholder(
            BOARD_ASSETS / "wo_zai_ne.ogg", seconds=1.2, freq=440, title="wo_zai_ne_placeholder"
        )
        _ffmpeg_placeholder(
            BOARD_ASSETS / "night_soothe.ogg",
            seconds=2.0,
            freq=330,
            title="night_soothe_placeholder",
        )
        manifest = {
            "schema_version": 1,
            "codec": "opus",
            "bitrate_bps": 16000,
            "sample_rate_hz": 16000,
            "stories": [
                {
                    "id": story_id,
                    "title": title,
                    "file": f"{story_id}.ogg",
                    "placeholder": True,
                }
                for story_id, title, _ in STORIES
            ],
        }
        STORIES_DIR.mkdir(parents=True, exist_ok=True)
        (STORIES_DIR / "index.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    total = _tree_bytes(BOARD_ASSETS)
    free = ASSETS_PARTITION_BYTES - total
    print(f"board_assets_bytes={total}")
    print(f"assets_partition_bytes={ASSETS_PARTITION_BYTES}")
    print(f"free_margin_bytes={free}")
    if free < MIN_FREE_MARGIN_BYTES:
        print(
            f"error: free margin {free} < required {MIN_FREE_MARGIN_BYTES}",
            file=sys.stderr,
        )
        return 1
    missing = [STORIES_DIR / f"{story_id}.ogg" for story_id, _, _ in STORIES]
    missing += [BOARD_ASSETS / "wo_zai_ne.ogg", BOARD_ASSETS / "night_soothe.ogg"]
    for path in missing:
        if not path.is_file() or path.stat().st_size <= 0:
            print(f"error: missing asset {path}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
