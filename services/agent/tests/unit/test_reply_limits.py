"""Spoken reply limits: a child and an elder get shorter ones; everyone else is unchanged.

2026-10-01 soak: a child asking "天空为什么是蓝色的？" got a 273-character answer with "瑞利散射" and a
numbered list, 60 seconds of speech, because the general limit is 320 characters (560 for a requested
long answer) whatever the listener.
"""

from __future__ import annotations

import pytest
from services.agent.src.reply_pipeline import (
    AUDIENCE_REPLY_LIMITS,
    MAX_CONTROLLED_VOICE_REPLY_CHARS,
    MAX_CONTROLLED_VOICE_REPLY_SENTENCES,
    MAX_REALTIME_REPLY_CHARS,
    MAX_REALTIME_REPLY_SENTENCES,
    MAX_VOICE_REPLY_CHARS,
    MAX_VOICE_REPLY_CHARS_LONGFORM,
    MAX_VOICE_REPLY_SENTENCES,
    MAX_VOICE_REPLY_SENTENCES_LONGFORM,
    reply_limits_for,
)
from services.common.response_depth import ResponseDepth


@pytest.mark.parametrize("audience", [None, "adult_companion", "family_shared", "unknown_safe"])
def test_other_listeners_keep_the_general_limits(audience: str | None) -> None:
    standard = ResponseDepth.STANDARD
    assert reply_limits_for(standard, barge_in_enabled=True, realtime=False, audience=audience) == (
        MAX_VOICE_REPLY_CHARS,
        MAX_VOICE_REPLY_SENTENCES,
    )
    assert reply_limits_for(standard, barge_in_enabled=False, realtime=False, audience=audience) == (
        MAX_CONTROLLED_VOICE_REPLY_CHARS,
        MAX_CONTROLLED_VOICE_REPLY_SENTENCES,
    )
    assert reply_limits_for(
        ResponseDepth.EXTENDED, barge_in_enabled=True, realtime=False, audience=audience
    ) == (MAX_VOICE_REPLY_CHARS_LONGFORM, MAX_VOICE_REPLY_SENTENCES_LONGFORM)
    assert reply_limits_for(
        ResponseDepth.BRIEF, barge_in_enabled=True, realtime=True, audience=audience
    ) == (MAX_REALTIME_REPLY_CHARS, MAX_REALTIME_REPLY_SENTENCES)


@pytest.mark.parametrize("audience", ["student_minor", "senior_companion"])
def test_a_child_and_an_elder_get_short_replies_unless_they_asked_for_more(audience: str) -> None:
    (standard_chars, standard_sentences), (extended_chars, extended_sentences) = AUDIENCE_REPLY_LIMITS[
        audience
    ]
    for depth in (ResponseDepth.STANDARD, ResponseDepth.BRIEF):
        chars, sentences = reply_limits_for(depth, barge_in_enabled=True, realtime=False, audience=audience)
        assert (chars, sentences) == (standard_chars, standard_sentences)
    assert reply_limits_for(
        ResponseDepth.EXTENDED, barge_in_enabled=True, realtime=False, audience=audience
    ) == (extended_chars, extended_sentences)
    # never longer than the general limit for the same turn
    assert standard_chars < MAX_VOICE_REPLY_CHARS and extended_chars < MAX_VOICE_REPLY_CHARS_LONGFORM
    # about 25 s and 60 s of speech at ~4 characters a second
    assert standard_chars <= 110 and extended_chars <= 300


def test_a_realtime_answer_is_never_longer_than_the_audience_cap() -> None:
    chars, sentences = reply_limits_for(
        ResponseDepth.BRIEF, barge_in_enabled=True, realtime=True, audience="student_minor"
    )
    assert chars == min(MAX_REALTIME_REPLY_CHARS, AUDIENCE_REPLY_LIMITS["student_minor"][0][0])
    assert sentences == min(MAX_REALTIME_REPLY_SENTENCES, AUDIENCE_REPLY_LIMITS["student_minor"][0][1])


def _policy_for(service_mode: str) -> object:
    from dataclasses import replace

    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.runtime_profile import parse_runtime_profile
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        TEST_VERIFY_KEY,
        canonical_wire_payload,
    )

    base = ModePolicy.companion_for_test(
        policy_version="test-policy",
        private_context=False,
        owner_evidence=False,
        tools=False,
        voice_profile=False,
        shadow_low_sensitivity_persona=False,
    )
    base = replace(base, companion_style_id="axu")
    subject = {
        "student_minor": {},
        "senior_companion": {"subject_category": "adult", "age_band": "adult"},
        "adult_companion": {"subject_category": "adult", "age_band": "adult"},
    }[service_mode]
    parsed = parse_runtime_profile(
        canonical_wire_payload(service_mode=service_mode, **subject), verify_key=TEST_VERIFY_KEY
    )
    assert parsed is not None
    return replace(base, runtime_profile=parsed)


@pytest.mark.parametrize(
    ("service_mode", "expected"),
    [
        ("student_minor", "我是阿序，是你的机器人朋友。你想聊什么，都可以跟我说。"),
        ("senior_companion", "我是阿序，是陪您聊天的机器人。您想聊什么，都可以跟我说。"),
        ("adult_companion", "我是阿序，直接清晰地梳理信息，但不替用户做决定。"),
    ],
)
def test_the_local_safe_plan_introduces_the_companion_to_the_listener(
    service_mode: str, expected: str
) -> None:
    from types import SimpleNamespace

    from services.agent.src.agent import build_local_safe_plan
    from services.agent.src.contracts.ids import GenerationFence

    plan = build_local_safe_plan(
        policy=_policy_for(service_mode),  # type: ignore[arg-type]
        fence=GenerationFence("s", 1, 1, 0),
        speaker=SimpleNamespace(classification="owner"),
        reason="test",
        tts_model="seed-tts-2.0",
        query="你叫什么名字？",
    )
    assert plan.direct_text == expected


def test_the_reply_pipeline_hands_the_listener_to_the_depth_policy_and_the_limits() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "src" / "reply_pipeline.py"
    ).read_text(encoding="utf-8")
    assert "audience=audience,\n            )\n" in source  # response_depth_for(..., audience=audience)
    assert "reply_limits_for(" in source and "profile.profile.service_mode" in source
