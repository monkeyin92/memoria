#!/usr/bin/env python3
"""Pack offline story and line placeholders into the board assets directory.

Product recordings replace the ffmpeg sine placeholders later. Stories are
Opus 16 kbps mono 16 kHz Ogg files under boards/memoria/esp-vocat/assets/stories/.
Offline lines live under assets/offline_lines/ with a host-side index.json.
Upstream packs DEFAULT_ASSETS_EXTRA_FILES by basename, so GetAssetData keys are
flat names such as offline-wake-no-network-v1.ogg (CONFIG_MMAP_FILE_NAME_LENGTH=32).

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
OFFLINE_LINES_DIR = BOARD_ASSETS / "offline_lines"
ASSETS_PARTITION_BYTES = 8 * 1024 * 1024
MIN_FREE_MARGIN_BYTES = 512 * 1024
MAX_ASSET_NAME_LEN = 32

STORIES = (
    ("story_01", "小星星的枕头", 392),
    ("story_02", "月亮不着急", 349),
    ("story_03", "大熊和森林里的悄悄话", 294),
)

# id, Chinese text, placeholder sine frequency (Hz). Numbering matches offline-lines v2.1.
OFFLINE_LINES = (
    ("offline-wake-no-network-v1", "我现在连不上网。", 410),
    ("offline-wake-ask-parent-v1", "请爸爸妈妈看看家里的网络。", 420),
    ("offline-wake-story-hint-v2", "不联网也能听故事。", 430),
    ("offline-mid-drop-v2", "网络断开了，刚才的话先停一下。", 440),
    ("offline-mid-ask-parent-v1", "请爸爸妈妈帮忙看看网络。", 450),
    ("offline-mid-retry-v1", "我在试着重新连网，等一等。", 460),
    ("offline-reconnecting-v1", "还在连网，再等一小会儿。", 470),
    ("online-restored-v1", "网络连好了。", 480),
    ("online-restored-chat-v1", "现在可以和我聊天了。", 490),
    ("online-restored-in-story-v2", "网络连好了，故事讲完再聊。", 500),
    ("offline-menu-intro-v2", "这里有三个故事。", 510),
    ("offline-menu-how-v1", "点一下屏幕，换下一个。", 520),
    ("offline-menu-confirm-how-v2", "按住屏幕不放，就开始播放。", 530),
    ("offline-menu-item-01-v2", "第一个故事：小星星的枕头。", 540),
    ("offline-menu-item-02-v2", "第二个故事：月亮不着急。", 550),
    ("offline-menu-item-03-v2", "第三个故事：大熊和森林里的悄悄话。", 560),
    ("offline-menu-choose-v2", "选好了，就按住屏幕。", 570),
    ("offline-menu-start-v1", "好的，现在开始。", 580),
    ("offline-menu-end-v1", "讲完啦。点一下屏幕，再选一个。", 590),
    ("offline-menu-idle-v1", "我先休息一下，点屏幕就能叫醒我。", 600),
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


def _asset_name_ok(filename: str) -> bool:
    return 0 < len(filename) <= MAX_ASSET_NAME_LEN


def write_manifests() -> None:
    stories_manifest = {
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
        json.dumps(stories_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines_manifest = {
        "schema_version": 1,
        "codec": "opus",
        "bitrate_bps": 16000,
        "sample_rate_hz": 16000,
        "lines": [
            {
                "id": line_id,
                "text": text,
                "file": f"{line_id}.ogg",
                "placeholder": True,
            }
            for line_id, text, _ in OFFLINE_LINES
        ],
    }
    OFFLINE_LINES_DIR.mkdir(parents=True, exist_ok=True)
    (OFFLINE_LINES_DIR / "index.json").write_text(
        json.dumps(lines_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def check_assets() -> int:
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

    errors: list[str] = []
    for story_id, _, _ in STORIES:
        name = f"{story_id}.ogg"
        path = STORIES_DIR / name
        if not _asset_name_ok(name):
            errors.append(f"filename too long ({len(name)}): {name}")
        if not path.is_file() or path.stat().st_size <= 0:
            errors.append(f"missing asset {path}")

    for line_id, _, _ in OFFLINE_LINES:
        name = f"{line_id}.ogg"
        path = OFFLINE_LINES_DIR / name
        if not _asset_name_ok(name):
            errors.append(f"filename too long ({len(name)}): {name}")
        if not path.is_file() or path.stat().st_size <= 0:
            errors.append(f"missing asset {path}")

    for name in ("wo_zai_ne.ogg", "night_soothe.ogg"):
        path = BOARD_ASSETS / name
        if not path.is_file() or path.stat().st_size <= 0:
            errors.append(f"missing asset {path}")

    manifest_path = OFFLINE_LINES_DIR / "index.json"
    if not manifest_path.is_file():
        errors.append(f"missing manifest {manifest_path}")
    else:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        ids = [entry["id"] for entry in data.get("lines", [])]
        expected = [line_id for line_id, _, _ in OFFLINE_LINES]
        if ids != expected:
            errors.append(f"offline_lines index ids mismatch: {ids!r} != {expected!r}")
        for entry in data.get("lines", []):
            line_id = entry["id"]
            if not entry.get("placeholder", False):
                errors.append(f"manifest entry {line_id} must set placeholder:true")
            filename = entry.get("file") or f"{line_id}.ogg"
            if not _asset_name_ok(filename):
                errors.append(f"manifest filename too long ({len(filename)}): {filename}")
            if not (OFFLINE_LINES_DIR / filename).is_file():
                errors.append(f"manifest id {line_id} has no asset {filename}")

    stories_manifest = STORIES_DIR / "index.json"
    if stories_manifest.is_file():
        data = json.loads(stories_manifest.read_text(encoding="utf-8"))
        title_by_id = {s["id"]: s["title"] for s in data.get("stories", [])}
        if title_by_id.get("story_03") != "大熊和森林里的悄悄话":
            errors.append("story_03 title must be 大熊和森林里的悄悄话")

    for message in errors:
        print(f"error: {message}", file=sys.stderr)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=8.0, help="story placeholder length")
    parser.add_argument(
        "--line-seconds", type=float, default=1.5, help="offline line placeholder length"
    )
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
        _ffmpeg_placeholder(
            BOARD_ASSETS / "wo_zai_ne.ogg", seconds=1.2, freq=440, title="wo_zai_ne_placeholder"
        )
        _ffmpeg_placeholder(
            BOARD_ASSETS / "night_soothe.ogg",
            seconds=2.0,
            freq=330,
            title="night_soothe_placeholder",
        )
        for line_id, _text, freq in OFFLINE_LINES:
            _ffmpeg_placeholder(
                OFFLINE_LINES_DIR / f"{line_id}.ogg",
                seconds=args.line_seconds,
                freq=freq,
                title=f"{line_id}_placeholder",
            )
        write_manifests()

    return check_assets()


if __name__ == "__main__":
    raise SystemExit(main())
