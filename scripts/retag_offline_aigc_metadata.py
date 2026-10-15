#!/usr/bin/env python3
"""Metadata-only AIGC OpusTags rewrite for the 25 audited production offline clips.

Rewrites Vorbis comments only (no re-encode). Verifies audio-packet SHA-256 and
ffmpeg-decoded PCM SHA-256 are unchanged. Writes a verification table under
docs/acceptance/run-20261015-aigc-labels/.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.common.aigc_opus import (  # noqa: E402
    audio_packets_digest,
    pcm_digest,
    require_aigc_tags,
    rewrite_opus_comments,
)

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
REPORT = (
    REPO
    / "docs"
    / "acceptance"
    / "run-20261015-aigc-labels"
    / "metadata-retag-25.json"
)

SERVICE_PROVIDER = "Volcengine Doubao"
VOICE_ID = "zh_female_tianmeitaozi_uranus_bigtts"
CONTENT_PRODUCER = "Memoria"
DATE = "2026-10-14"

PRODUCTION_RELS = (
    "stories/story_01.ogg",
    "stories/story_02.ogg",
    "stories/story_03.ogg",
    "wo_zai_ne.ogg",
    "night_soothe.ogg",
    "offline_lines/offline-wake-no-network-v1.ogg",
    "offline_lines/offline-wake-ask-parent-v1.ogg",
    "offline_lines/offline-wake-story-hint-v2.ogg",
    "offline_lines/offline-mid-drop-v2.ogg",
    "offline_lines/offline-mid-ask-parent-v1.ogg",
    "offline_lines/offline-mid-retry-v1.ogg",
    "offline_lines/offline-reconnecting-v1.ogg",
    "offline_lines/online-restored-v1.ogg",
    "offline_lines/online-restored-chat-v1.ogg",
    "offline_lines/online-restored-in-story-v2.ogg",
    "offline_lines/offline-menu-intro-v2.ogg",
    "offline_lines/offline-menu-how-v1.ogg",
    "offline_lines/offline-menu-confirm-how-v2.ogg",
    "offline_lines/offline-menu-item-01-v2.ogg",
    "offline_lines/offline-menu-item-02-v2.ogg",
    "offline_lines/offline-menu-item-03-v2.ogg",
    "offline_lines/offline-menu-choose-v2.ogg",
    "offline_lines/offline-menu-start-v1.ogg",
    "offline_lines/offline-menu-end-v1.ogg",
    "offline_lines/offline-menu-idle-v1.ogg",
)


def _retag_one(path: Path) -> dict[str, object]:
    before = path.read_bytes()
    content_id = path.stem
    before_packets = audio_packets_digest(before)
    before_pcm = pcm_digest(before)
    after = rewrite_opus_comments(
        before,
        aigc={
            "CONTENT_ID": content_id,
            "SERVICE_PROVIDER": SERVICE_PROVIDER,
            "AI_GENERATED": "true",
            "ARTIST": VOICE_ID,
            "DATE": DATE,
            "CONTENT_PRODUCER": CONTENT_PRODUCER,
        },
    )
    after_packets = audio_packets_digest(after)
    after_pcm = pcm_digest(after)
    if after_packets != before_packets:
        raise RuntimeError(f"{path}: audio packet digest changed")
    if after_pcm != before_pcm:
        raise RuntimeError(f"{path}: PCM digest changed")
    require_aigc_tags(after[:65536], content_id=content_id, implicit=True)
    if after != before:
        path.write_bytes(after)
    return {
        "path": str(path.relative_to(REPO)),
        "content_id": content_id,
        "bytes_before": len(before),
        "bytes_after": len(after),
        "audio_packets_sha256_before": before_packets,
        "audio_packets_sha256_after": after_packets,
        "pcm_sha256_before": before_pcm,
        "pcm_sha256_after": after_pcm,
        "audio_unchanged": True,
        "method": "opus_tags_metadata_only",
        "service_provider": SERVICE_PROVIDER,
        "artist": VOICE_ID,
        "date": DATE,
        "content_producer": CONTENT_PRODUCER,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assets-root",
        type=Path,
        default=BOARD_ASSETS,
        help="board assets root containing the 25 production oggs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute digests and rewrite in memory without writing files",
    )
    args = parser.parse_args(argv)
    rows: list[dict[str, object]] = []
    for rel in PRODUCTION_RELS:
        path = args.assets_root / rel
        if not path.is_file():
            print(f"missing {rel}", file=sys.stderr)
            return 1
        if args.dry_run:
            before = path.read_bytes()
            after = rewrite_opus_comments(
                before,
                aigc={
                    "CONTENT_ID": path.stem,
                    "SERVICE_PROVIDER": SERVICE_PROVIDER,
                    "AI_GENERATED": "true",
                    "ARTIST": VOICE_ID,
                    "DATE": DATE,
                    "CONTENT_PRODUCER": CONTENT_PRODUCER,
                },
            )
            row = {
                "path": rel,
                "audio_packets_sha256_before": audio_packets_digest(before),
                "audio_packets_sha256_after": audio_packets_digest(after),
                "pcm_sha256_before": pcm_digest(before),
                "pcm_sha256_after": pcm_digest(after),
            }
            assert row["audio_packets_sha256_before"] == row["audio_packets_sha256_after"]
            assert row["pcm_sha256_before"] == row["pcm_sha256_after"]
            rows.append(row)
            print(f"ok dry-run {rel}")
        else:
            row = _retag_one(path)
            rows.append(row)
            print(
                f"ok {rel} packets={row['audio_packets_sha256_after']} "
                f"pcm={row['pcm_sha256_after']}"
            )
    payload = {
        "count": len(rows),
        "method": "opus_tags_metadata_only",
        "service_provider": SERVICE_PROVIDER,
        "artist": VOICE_ID,
        "date": DATE,
        "content_producer": CONTENT_PRODUCER,
        "files": rows,
    }
    if not args.dry_run:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {REPORT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
