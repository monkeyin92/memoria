"""Appendix-E AIGC OpusTags on Ogg Opus clips."""

from __future__ import annotations

import struct
from collections.abc import Mapping
from typing import Final

REQUIRED_AIGC_TAG_KEYS: Final = ("CONTENT_ID", "SERVICE_PROVIDER", "AI_GENERATED")
OPUS_TAGS_SCAN_BYTES: Final = 64 * 1024


def opus_tag_pairs(head: bytes) -> list[tuple[str, str]]:
    if not head.startswith(b"OggS"):
        raise ValueError("not an Ogg stream")
    at = head.find(b"OpusTags")
    if at < 0:
        raise ValueError("no OpusTags header")
    pos = at + len(b"OpusTags")
    pairs: list[tuple[str, str]] = []
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
            if sep:
                pairs.append((key, value))
    except (struct.error, UnicodeDecodeError) as exc:
        raise ValueError("truncated OpusTags") from exc
    return pairs


def aigc_tag_map(head: bytes) -> dict[str, str]:
    return {key.upper(): value for key, value in opus_tag_pairs(head)}


def require_aigc_tags(
    head: bytes,
    *,
    content_id: str,
) -> dict[str, object]:
    """Return normalised AIGC tags or raise ``ValueError`` when the clip is unlabelled."""

    tags = aigc_tag_map(head)
    missing = [key for key in REQUIRED_AIGC_TAG_KEYS if not tags.get(key, "").strip()]
    if missing:
        raise ValueError("missing AIGC tags: " + ", ".join(missing))
    if tags.get("CONTENT_ID", "").strip() != content_id:
        raise ValueError(f"CONTENT_ID must be {content_id}")
    if tags.get("AI_GENERATED", "").strip().lower() not in {"true", "1", "yes"}:
        raise ValueError("AI_GENERATED must be true")
    if not tags.get("SERVICE_PROVIDER", "").strip():
        raise ValueError("SERVICE_PROVIDER must be non-empty")
    return {
        "content_id": tags["CONTENT_ID"].strip(),
        "service_provider": tags["SERVICE_PROVIDER"].strip(),
        "ai_generated": True,
    }


def title_is_placeholder(head: bytes) -> bool:
    for key, value in opus_tag_pairs(head):
        if key.lower() == "title" and value.endswith("_placeholder"):
            return True
    return False


def aigc_label_from_mapping(tags: Mapping[str, object]) -> dict[str, object]:
    return {
        "content_id": str(tags["content_id"]),
        "service_provider": str(tags["service_provider"]),
        "ai_generated": bool(tags["ai_generated"]),
    }


__all__ = [
    "OPUS_TAGS_SCAN_BYTES",
    "REQUIRED_AIGC_TAG_KEYS",
    "aigc_label_from_mapping",
    "aigc_tag_map",
    "opus_tag_pairs",
    "require_aigc_tags",
    "title_is_placeholder",
]
