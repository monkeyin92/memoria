#!/usr/bin/env python3
"""Metadata-only AIGC OpusTags rewrite for every board-asset Ogg.

Rewrites Vorbis comments only (no re-encode). Verifies audio-packet SHA-256
unchanged. When ffmpeg can decode the clip, also verifies PCM SHA-256 is
unchanged. Writes a verification table under
docs/acceptance/run-20261015-aigc-labels/.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.common.aigc_opus import (  # noqa: E402
    CANONICAL_AI_GENERATED,
    CANONICAL_SERVICE_PROVIDER,
    IMPLICIT_AIGC_TAG_KEYS,
    aigc_tag_map,
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
    / "metadata-retag-voice-id.json"
)

VOICE_ID = "zh_female_tianmeitaozi_uranus_bigtts"
CONTENT_PRODUCER = "Memoria"
DATE = "2026-10-14"
INTRO_DATE = "2026-10-15"
LLM_DATE = "2026-10-15"
DROP_PLACEHOLDER_TITLE_STEMS = frozenset({"llm_thinking", "llm_unavailable"})


def _date_for(stem: str) -> str:
    if stem.startswith("aigc_") or stem in DROP_PLACEHOLDER_TITLE_STEMS:
        return INTRO_DATE if stem.startswith("aigc_") else LLM_DATE
    return DATE


def _aigc_map(stem: str) -> dict[str, str]:
    return {
        "CONTENT_ID": stem,
        "SERVICE_PROVIDER": CANONICAL_SERVICE_PROVIDER,
        "AI_GENERATED": CANONICAL_AI_GENERATED,
        "ARTIST": VOICE_ID,
        "VOICE_ID": VOICE_ID,
        "DATE": _date_for(stem),
        "CONTENT_PRODUCER": CONTENT_PRODUCER,
    }


def _safe_pcm_digest(data: bytes) -> str | None:
    try:
        return pcm_digest(data)
    except ValueError:
        return None


def _safe_packets_digest(data: bytes) -> str | None:
    try:
        return audio_packets_digest(data)
    except ValueError:
        return None


def _rewrite_corrupt_placeholder_tags(data: bytes, *, aigc: dict[str, str]) -> bytes:
    """Rewrite OpusTags on the #255-style truncated placeholder blobs.

    Those files are not valid Ogg page streams, so ``rewrite_opus_comments``
    cannot run. Tags are still readable via the OpusTags substring; rebuild
    that substring only and leave the leading OggS header bytes untouched.
    """

    at = data.find(b"OpusTags")
    if at < 0:
        raise ValueError("no OpusTags header")

    vendor = b"memoria-placeholder"
    comments = [(key, aigc[key]) for key in IMPLICIT_AIGC_TAG_KEYS]
    body = bytearray(b"OpusTags")
    body += struct.pack("<I", len(vendor))
    body += vendor
    body += struct.pack("<I", len(comments))
    for key, value in comments:
        raw = f"{key}={value}".encode()
        body += struct.pack("<I", len(raw))
        body += raw
    return data[:at] + bytes(body)


def _retag_one(path: Path) -> dict[str, object]:
    before = path.read_bytes()
    content_id = path.stem
    before_packets = _safe_packets_digest(before)
    before_pcm = _safe_pcm_digest(before)
    drop_keys = (
        frozenset({"TITLE"}) if content_id in DROP_PLACEHOLDER_TITLE_STEMS else None
    )
    aigc = _aigc_map(content_id)
    if before_packets is None:
        if content_id not in DROP_PLACEHOLDER_TITLE_STEMS:
            raise RuntimeError(f"{path}: not a parseable Ogg Opus stream")
        after = _rewrite_corrupt_placeholder_tags(before, aigc=aigc)
        method = "corrupt_placeholder_tags_only"
        after_packets = None
    else:
        after = rewrite_opus_comments(before, aigc=aigc, drop_keys=drop_keys)
        method = "opus_tags_metadata_only"
        after_packets = audio_packets_digest(after)
        if after_packets != before_packets:
            raise RuntimeError(f"{path}: audio packet digest changed")
    after_pcm = _safe_pcm_digest(after)
    if before_pcm is not None and after_pcm != before_pcm:
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
        "pcm_verified": before_pcm is not None,
        "audio_unchanged": before_packets is not None,
        "method": method,
        "service_provider": CANONICAL_SERVICE_PROVIDER,
        "artist": VOICE_ID,
        "voice_id": VOICE_ID,
        "date": _date_for(content_id),
        "content_producer": CONTENT_PRODUCER,
        "dropped_title": drop_keys is not None and "TITLE" in drop_keys,
        "tags_before": aigc_tag_map(before[:65536]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assets-root",
        type=Path,
        default=BOARD_ASSETS,
        help="board assets root containing device oggs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute digests and rewrite in memory without writing files",
    )
    args = parser.parse_args(argv)
    paths = sorted(path for path in args.assets_root.rglob("*.ogg") if path.is_file())
    if not paths:
        print(f"no oggs under {args.assets_root}", file=sys.stderr)
        return 1
    rows: list[dict[str, object]] = []
    for path in paths:
        rel = path.relative_to(args.assets_root).as_posix()
        if args.dry_run:
            before = path.read_bytes()
            drop_keys = (
                frozenset({"TITLE"}) if path.stem in DROP_PLACEHOLDER_TITLE_STEMS else None
            )
            before_packets = _safe_packets_digest(before)
            if before_packets is None:
                after = _rewrite_corrupt_placeholder_tags(before, aigc=_aigc_map(path.stem))
                after_packets = None
            else:
                after = rewrite_opus_comments(
                    before, aigc=_aigc_map(path.stem), drop_keys=drop_keys
                )
                after_packets = audio_packets_digest(after)
                assert before_packets == after_packets
            before_pcm = _safe_pcm_digest(before)
            after_pcm = _safe_pcm_digest(after)
            if before_pcm is not None:
                assert before_pcm == after_pcm
            row = {
                "path": rel,
                "audio_packets_sha256_before": before_packets,
                "audio_packets_sha256_after": after_packets,
                "pcm_sha256_before": before_pcm,
                "pcm_sha256_after": after_pcm,
            }
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
        "service_provider": CANONICAL_SERVICE_PROVIDER,
        "ai_generated": CANONICAL_AI_GENERATED,
        "artist": VOICE_ID,
        "voice_id": VOICE_ID,
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
