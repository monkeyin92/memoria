"""Implicit AIGC OpusTags schema for shipped audio."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from services.common.aigc_opus import (
    IMPLICIT_AIGC_TAG_KEYS,
    REQUIRED_AIGC_TAG_KEYS,
    audio_packets_digest,
    pcm_digest,
    require_aigc_tags,
    rewrite_opus_comments,
)

BOARD_NIGHT = (
    Path(__file__).resolve().parents[3]
    / "firmware"
    / "esp32"
    / "overlay"
    / "files"
    / "main"
    / "boards"
    / "memoria"
    / "esp-vocat"
    / "assets"
    / "night_soothe.ogg"
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


def test_rewrite_opus_comments_keeps_audio_and_pcm_digests() -> None:
    before = BOARD_NIGHT.read_bytes()
    before_packets = audio_packets_digest(before)
    before_pcm = pcm_digest(before)
    after = rewrite_opus_comments(
        before,
        aigc={
            "CONTENT_ID": "night_soothe",
            "SERVICE_PROVIDER": "Volcengine Doubao",
            "AI_GENERATED": "true",
            "ARTIST": "zh_female_tianmeitaozi_uranus_bigtts",
            "DATE": "2026-10-14",
            "CONTENT_PRODUCER": "Memoria",
        },
    )
    assert audio_packets_digest(after) == before_packets
    assert pcm_digest(after) == before_pcm
    tags = require_aigc_tags(after[:65536], content_id="night_soothe", implicit=True)
    assert tags["service_provider"] == "Volcengine Doubao"
