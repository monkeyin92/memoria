#!/usr/bin/env python3
"""Check the offline story and line pack and derive its host-side manifests.

Every clip under boards/memoria/esp-vocat/assets/ is a recording copied in by hand.
This script never writes audio. The default run rewrites stories/index.json and
offline_lines/index.json from the tables below plus what each clip's bytes say it
is, then checks the pack. --check-only checks without writing. check-overlay.sh
runs that.

A clip whose OpusTags TITLE ends in "_placeholder" is a placeholder. Any other
title, including none, is production. The pack ships production clips only.
Upstream packs DEFAULT_ASSETS_EXTRA_FILES by basename, so names stay <= 32
characters. The 8 MiB assets partition keeps >= 0.5 MiB free.
"""

from __future__ import annotations

import argparse
import enum
import json
import struct
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

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
ASSETS_PARTITION_BYTES = 8 * 1024 * 1024
MIN_FREE_MARGIN_BYTES = 512 * 1024
MAX_ASSET_NAME_LEN = 32
OPUS_TAGS_SCAN_BYTES = 64 * 1024

VOICE = {
    "id": "zh_female_tianmeitaozi_uranus_bigtts",
    "generated": "2026-10-14",
    "terms": "trial/demo only pending Volcengine terms",
}

STORIES = (
    ("story_01", "小星星的枕头"),
    ("story_02", "月亮不着急"),
    ("story_03", "大熊和森林里的悄悄话"),
)

# Numbering matches offline-lines v2.1 and kOfflineLineIds in memoria_offline_menu.h.
OFFLINE_LINES = (
    ("offline-wake-no-network-v1", "我现在连不上网。"),
    ("offline-wake-ask-parent-v1", "请爸爸妈妈看看家里的网络。"),
    ("offline-wake-story-hint-v2", "不联网也能听故事。"),
    ("offline-mid-drop-v2", "网络断开了，刚才的话先停一下。"),
    ("offline-mid-ask-parent-v1", "请爸爸妈妈帮忙看看网络。"),
    ("offline-mid-retry-v1", "我在试着重新连网，等一等。"),
    ("offline-reconnecting-v1", "还在连网，再等一小会儿。"),
    ("online-restored-v1", "网络连好了。"),
    ("online-restored-chat-v1", "现在可以和我聊天了。"),
    ("online-restored-in-story-v2", "网络连好了，故事讲完再聊。"),
    ("offline-menu-intro-v2", "这里有三个故事。"),
    ("offline-menu-how-v1", "点一下屏幕，换下一个。"),
    ("offline-menu-confirm-how-v2", "按住屏幕不放，就开始播放。"),
    ("offline-menu-item-01-v2", "第一个故事：小星星的枕头。"),
    ("offline-menu-item-02-v2", "第二个故事：月亮不着急。"),
    ("offline-menu-item-03-v2", "第三个故事：大熊和森林里的悄悄话。"),
    ("offline-menu-choose-v2", "选好了，就按住屏幕。"),
    ("offline-menu-start-v1", "好的，现在开始。"),
    ("offline-menu-end-v1", "讲完啦。点一下屏幕，再选一个。"),
    ("offline-menu-idle-v1", "我先休息一下，点屏幕就能叫醒我。"),
)

BOARD_CLIPS = ("wo_zai_ne", "night_soothe")

STORIES_MANIFEST = PurePosixPath("stories/index.json")
LINES_MANIFEST = PurePosixPath("offline_lines/index.json")


class AssetKind(enum.Enum):
    PLACEHOLDER = "placeholder"
    PRODUCTION = "production"


def clip_paths() -> tuple[PurePosixPath, ...]:
    return (
        *(PurePosixPath("stories") / f"{story_id}.ogg" for story_id, _ in STORIES),
        *(PurePosixPath("offline_lines") / f"{line_id}.ogg" for line_id, _ in OFFLINE_LINES),
        *(PurePosixPath(f"{name}.ogg") for name in BOARD_CLIPS),
    )


def asset_kind(head: bytes) -> AssetKind:
    """Read AssetKind from an Ogg Opus header. TITLE ending in _placeholder is a placeholder."""
    if not head.startswith(b"OggS"):
        raise ValueError("not an Ogg stream")
    at = head.find(b"OpusTags")
    if at < 0:
        raise ValueError("no OpusTags header")
    pos = at + len(b"OpusTags")
    try:
        (vendor_len,) = struct.unpack_from("<I", head, pos)
        pos += 4 + vendor_len
        (count,) = struct.unpack_from("<I", head, pos)
        pos += 4
        for _ in range(count):
            (length,) = struct.unpack_from("<I", head, pos)
            pos += 4
            raw = head[pos : pos + length]
            pos += length
            key, sep, value = raw.decode("utf-8").partition("=")
            if sep and key.lower() == "title" and value.endswith("_placeholder"):
                return AssetKind.PLACEHOLDER
    except (struct.error, UnicodeDecodeError) as exc:
        raise ValueError("truncated OpusTags") from exc
    return AssetKind.PRODUCTION


def _manifest_text(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def derive_manifests(kinds: Mapping[PurePosixPath, AssetKind]) -> dict[PurePosixPath, str]:
    stories = {
        "schema_version": 1,
        "codec": "opus",
        "bitrate_bps": 16000,
        "sample_rate_hz": 16000,
        "voice": VOICE,
        "stories": [
            {
                "id": story_id,
                "title": title,
                "file": f"{story_id}.ogg",
                "placeholder": kinds[PurePosixPath("stories") / f"{story_id}.ogg"]
                is AssetKind.PLACEHOLDER,
            }
            for story_id, title in STORIES
        ],
    }
    lines = {
        "schema_version": 1,
        "codec": "opus",
        "bitrate_bps": 16000,
        "sample_rate_hz": 16000,
        "voice": VOICE,
        "lines": [
            {
                "id": line_id,
                "text": text,
                "file": f"{line_id}.ogg",
                "placeholder": kinds[PurePosixPath("offline_lines") / f"{line_id}.ogg"]
                is AssetKind.PLACEHOLDER,
            }
            for line_id, text in OFFLINE_LINES
        ],
    }
    return {
        STORIES_MANIFEST: _manifest_text(stories),
        LINES_MANIFEST: _manifest_text(lines),
    }


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def check_pack(root: Path, *, write_manifests: bool) -> int:
    errors: list[str] = []
    kinds: dict[PurePosixPath, AssetKind] = {}
    for rel in clip_paths():
        name = rel.name
        if not 0 < len(name) <= MAX_ASSET_NAME_LEN:
            errors.append(f"filename too long ({len(name)}): {name}")
        path = root / rel
        if not path.is_file() or path.stat().st_size <= 0:
            errors.append(f"missing asset {rel.as_posix()}; copy the production clip")
            continue
        try:
            kinds[rel] = asset_kind(path.read_bytes()[:OPUS_TAGS_SCAN_BYTES])
        except ValueError as exc:
            errors.append(f"{rel.as_posix()}: {exc}")

    if len(kinds) == len(clip_paths()):
        for rel, text in derive_manifests(kinds).items():
            dest = root / rel
            if write_manifests:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8")
            elif not dest.is_file() or dest.read_text(encoding="utf-8") != text:
                errors.append(f"{rel.as_posix()} is stale; run build_offline_stories.py")
    elif not write_manifests:
        for rel in (STORIES_MANIFEST, LINES_MANIFEST):
            if not (root / rel).is_file():
                errors.append(f"{rel.as_posix()} is stale; run build_offline_stories.py")

    for rel, kind in kinds.items():
        if kind is AssetKind.PLACEHOLDER:
            errors.append(f"{rel.as_posix()} is a placeholder; copy the production clip")

    total = _tree_bytes(root) if root.is_dir() else 0
    free = ASSETS_PARTITION_BYTES - total
    production = sum(kind is AssetKind.PRODUCTION for kind in kinds.values())
    placeholder = sum(kind is AssetKind.PLACEHOLDER for kind in kinds.values())
    print(f"board_assets_bytes={total}")
    print(f"assets_partition_bytes={ASSETS_PARTITION_BYTES}")
    print(f"free_margin_bytes={free}")
    print(f"production_clips={production}")
    print(f"placeholder_clips={placeholder}")
    if free < MIN_FREE_MARGIN_BYTES:
        errors.append(f"free margin {free} < required {MIN_FREE_MARGIN_BYTES}")

    for message in errors:
        print(f"error: {message}", file=sys.stderr)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only", action="store_true", help="check without rewriting manifests"
    )
    parser.add_argument(
        "--assets-root",
        type=Path,
        default=BOARD_ASSETS,
        help="vet a staged drop before copying it in",
    )
    args = parser.parse_args(argv)
    return check_pack(args.assets_root, write_manifests=not args.check_only)


if __name__ == "__main__":
    raise SystemExit(main())
