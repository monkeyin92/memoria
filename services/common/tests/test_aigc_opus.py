"""Implicit AIGC OpusTags schema for shipped audio."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from services.common.aigc_opus import (
    CANONICAL_AI_GENERATED,
    CANONICAL_SERVICE_PROVIDER,
    IMPLICIT_AIGC_TAG_KEYS,
    REQUIRED_AIGC_TAG_KEYS,
    audio_packets_digest,
    opus_tag_pairs,
    pcm_digest,
    require_aigc_tags,
    rewrite_opus_comments,
)

BOARD_CLIP = (
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
    / "offline_lines"
    / "llm_unavailable.ogg"
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
            f"SERVICE_PROVIDER={CANONICAL_SERVICE_PROVIDER}",
            f"AI_GENERATED={CANONICAL_AI_GENERATED}",
            "ARTIST=zh_female_shaoergushi_uranus_bigtts",
            "VOICE_ID=zh_female_shaoergushi_uranus_bigtts",
            "DATE=2026-10-15",
            "CONTENT_PRODUCER=Memoria",
        ]
    )
    tags = require_aigc_tags(head, content_id="demo", implicit=True)
    assert tags["artist"] == "zh_female_shaoergushi_uranus_bigtts"
    assert tags["voice_id"] == "zh_female_shaoergushi_uranus_bigtts"
    assert tags["date"] == "2026-10-15"
    assert tags["content_producer"] == "Memoria"
    assert IMPLICIT_AIGC_TAG_KEYS[-4:] == (
        "ARTIST",
        "VOICE_ID",
        "DATE",
        "CONTENT_PRODUCER",
    )
    assert REQUIRED_AIGC_TAG_KEYS == ("CONTENT_ID", "SERVICE_PROVIDER", "AI_GENERATED")


def test_implicit_tags_reject_partial_label() -> None:
    head = _ogg_with_comments(
        [
            "CONTENT_ID=demo",
            "SERVICE_PROVIDER=Memoria",
            "AI_GENERATED=true",
            "ARTIST=zh_female_shaoergushi_uranus_bigtts",
        ]
    )
    with pytest.raises(ValueError, match="VOICE_ID, DATE, CONTENT_PRODUCER"):
        require_aigc_tags(head, content_id="demo", implicit=True)


def test_rewrite_opus_comments_keeps_audio_and_pcm_digests() -> None:
    before = BOARD_CLIP.read_bytes()
    before_packets = audio_packets_digest(before)
    before_pcm = pcm_digest(before)
    after = rewrite_opus_comments(
        before,
        aigc={
            "CONTENT_ID": "llm_unavailable",
            "SERVICE_PROVIDER": CANONICAL_SERVICE_PROVIDER,
            "AI_GENERATED": CANONICAL_AI_GENERATED,
            "ARTIST": "zh_female_shaoergushi_uranus_bigtts",
            "VOICE_ID": "zh_female_shaoergushi_uranus_bigtts",
            "DATE": "2026-10-14",
            "CONTENT_PRODUCER": "Memoria",
        },
    )
    assert audio_packets_digest(after) == before_packets
    assert pcm_digest(after) == before_pcm
    tags = require_aigc_tags(after[:65536], content_id="llm_unavailable", implicit=True)
    assert tags["service_provider"] == CANONICAL_SERVICE_PROVIDER
    assert tags["voice_id"] == "zh_female_shaoergushi_uranus_bigtts"


def test_legacy_implicit_label_is_optional_for_the_addendum_keys() -> None:
    head = _ogg_with_comments(
        [
            "CONTENT_ID=demo",
            f"SERVICE_PROVIDER={CANONICAL_SERVICE_PROVIDER}",
            f"AI_GENERATED={CANONICAL_AI_GENERATED}",
            "ARTIST=zh_female_shaoergushi_uranus_bigtts",
            "VOICE_ID=zh_female_shaoergushi_uranus_bigtts",
            "DATE=2026-10-15",
            "CONTENT_PRODUCER=Memoria",
        ]
    )
    tags = require_aigc_tags(head, content_id="demo", implicit=True)
    assert tags["content_propagator"] == ""
    assert tags["label"] is False


def test_implicit_label_writes_propagator_and_label_for_new_clips() -> None:
    from services.common.aigc_opus import (
        CONTENT_PROPAGATOR_KEY,
        LABEL_EXPLICIT,
        LABEL_KEY,
        implicit_label,
    )

    before = BOARD_CLIP.read_bytes()
    after = rewrite_opus_comments(
        before,
        aigc=implicit_label(
            content_id="llm_unavailable",
            aigc={
                "SERVICE_PROVIDER": CANONICAL_SERVICE_PROVIDER,
                "ARTIST": "zh_female_shaoergushi_uranus_bigtts",
                "VOICE_ID": "zh_female_shaoergushi_uranus_bigtts",
                "DATE": "2026-10-15",
            },
            content_producer="Memoria",
            content_propagator="Memoria Propagator",
        ),
    )
    tags = require_aigc_tags(after[:65536], content_id="llm_unavailable", implicit=True)
    assert tags["content_propagator"] == "Memoria Propagator"
    assert tags["label"] is True
    raw = dict(opus_tag_pairs(after[:65536]))
    assert raw[CONTENT_PROPAGATOR_KEY] == "Memoria Propagator"
    assert raw[LABEL_KEY] == LABEL_EXPLICIT


def _implicit_head(*, service_provider: str, voice_id: str) -> bytes:
    return _ogg_with_comments(
        [
            "CONTENT_ID=demo",
            f"SERVICE_PROVIDER={service_provider}",
            "AI_GENERATED=true",
            f"ARTIST={voice_id}",
            f"VOICE_ID={voice_id}",
            "DATE=2026-10-22",
            "CONTENT_PRODUCER=Memoria",
        ]
    )


def test_implicit_tags_accept_stepfun_wenrounansheng() -> None:
    tags = require_aigc_tags(
        _implicit_head(service_provider="StepFun", voice_id="wenrounansheng"),
        content_id="demo",
        implicit=True,
    )
    assert tags["service_provider"] == "StepFun"
    assert tags["voice_id"] == "wenrounansheng"


def test_implicit_tags_reject_stepfun_on_a_doubao_voice() -> None:
    with pytest.raises(ValueError, match="SERVICE_PROVIDER must be 'Volcengine Doubao'"):
        require_aigc_tags(
            _implicit_head(
                service_provider="StepFun",
                voice_id="zh_female_shaoergushi_uranus_bigtts",
            ),
            content_id="demo",
            implicit=True,
        )


def test_implicit_tags_reject_doubao_on_wenrounansheng() -> None:
    with pytest.raises(ValueError, match="SERVICE_PROVIDER must be 'StepFun'"):
        require_aigc_tags(
            _implicit_head(service_provider="Volcengine Doubao", voice_id="wenrounansheng"),
            content_id="demo",
            implicit=True,
        )


def test_implicit_tags_reject_an_unknown_provider_and_an_unknown_voice() -> None:
    with pytest.raises(ValueError, match="SERVICE_PROVIDER must be 'Volcengine Doubao'"):
        require_aigc_tags(
            _implicit_head(
                service_provider="Memoria",
                voice_id="zh_female_shaoergushi_uranus_bigtts",
            ),
            content_id="demo",
            implicit=True,
        )
    with pytest.raises(ValueError, match="has no service provider"):
        require_aigc_tags(
            _implicit_head(service_provider="StepFun", voice_id="not-a-voice"),
            content_id="demo",
            implicit=True,
        )


def test_identity_defaults_to_the_producer_and_accepts_configuration() -> None:
    from services.common.aigc_opus import aigc_identity_from_env

    assert aigc_identity_from_env({}) == ("Memoria", "Memoria")
    assert aigc_identity_from_env(
        {"AIGC_CONTENT_PRODUCER": "Acme", "AIGC_CONTENT_PROPAGATOR": "Acme Ops"}
    ) == ("Acme", "Acme Ops")
    assert aigc_identity_from_env({"AIGC_CONTENT_PRODUCER": "Acme"}) == (
        "Acme",
        "Acme",
    )
