"""Implicit AIGC OpusTags schema for shipped audio."""

from __future__ import annotations

import struct

import pytest
from services.common.aigc_opus import (
    IMPLICIT_AIGC_TAG_KEYS,
    REQUIRED_AIGC_TAG_KEYS,
    require_aigc_tags,
)


def _ogg_with_comments(comments: list[str]) -> bytes:
    vendor = b"memoria-test"
    body = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor
    body += struct.pack("<I", len(comments))
    for comment in comments:
        raw = comment.encode("utf-8")
        body += struct.pack("<I", len(raw)) + raw
    return b"OggS" + b"\x00" * 24 + body


def test_core_tags_accept_appendix_e_minimum() -> None:
    head = _ogg_with_comments(
        [
            "CONTENT_ID=demo",
            "SERVICE_PROVIDER=Memoria",
            "AI_GENERATED=true",
        ]
    )
    tags = require_aigc_tags(head, content_id="demo")
    assert tags == {
        "content_id": "demo",
        "service_provider": "Memoria",
        "ai_generated": True,
    }


def test_implicit_tags_require_voice_date_and_producer() -> None:
    head = _ogg_with_comments(
        [
            "CONTENT_ID=demo",
            "SERVICE_PROVIDER=Memoria",
            "AI_GENERATED=true",
            "ARTIST=zh_female_tianmeitaozi_uranus_bigtts",
            "DATE=2026-10-15",
            "CONTENT_PRODUCER=Memoria",
        ]
    )
    tags = require_aigc_tags(head, content_id="demo", implicit=True)
    assert tags["voice_id"] == "zh_female_tianmeitaozi_uranus_bigtts"
    assert tags["date"] == "2026-10-15"
    assert tags["content_producer"] == "Memoria"
    assert IMPLICIT_AIGC_TAG_KEYS[-3:] == ("ARTIST", "DATE", "CONTENT_PRODUCER")
    assert REQUIRED_AIGC_TAG_KEYS == ("CONTENT_ID", "SERVICE_PROVIDER", "AI_GENERATED")


def test_implicit_tags_reject_partial_label() -> None:
    head = _ogg_with_comments(
        [
            "CONTENT_ID=demo",
            "SERVICE_PROVIDER=Memoria",
            "AI_GENERATED=true",
            "ARTIST=zh_female_tianmeitaozi_uranus_bigtts",
        ]
    )
    with pytest.raises(ValueError, match="DATE, CONTENT_PRODUCER"):
        require_aigc_tags(head, content_id="demo", implicit=True)
