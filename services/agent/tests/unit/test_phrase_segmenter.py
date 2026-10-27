from __future__ import annotations

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.phrase_segmenter import (
    PhraseSegmenter,
    normalize_for_tts,
    segment_all,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("可以，我先帮你看一下。", ["可以，我先帮你看一下。"]),
        (
            "明天下午可能有雨，出门建议带伞。晚上温度会低一些。",
            ["明天下午可能有雨，", "出门建议带伞。", "晚上温度会低一些。"],
        ),
        (
            "订单号是20260715001234，请确认。",
            ["订单号是20260715001234，请确认。"],
        ),
    ],
)
def test_phrase_segmenter(text: str, expected: list[str]) -> None:
    assert segment_all(text) == expected


def test_does_not_alter_order_id() -> None:
    parts = segment_all("订单号是20260715001234，请确认。")
    assert "20260715001234" in "".join(parts)


@pytest.mark.parametrize(
    "text",
    [
        "那我讲一个。\n有只小兔子",
        "那我讲一个。\n\n有只小兔子",
        "那我讲一个。\n。有只小兔子",
    ],
)
def test_normalize_for_tts_does_not_duplicate_terminal_punctuation(text: str) -> None:
    normalized, _canonical = normalize_for_tts(text)
    assert "。。" not in normalized
    assert normalized == "那我讲一个。有只小兔子"


def test_streaming_chunks_do_not_duplicate_terminal_punctuation() -> None:
    seg = PhraseSegmenter(GenerationFence("story-punct", 1, 1, 0))
    parts: list[str] = []
    for token in ("那我讲一个。", "\n", "有只小兔子"):
        parts.extend(s.text for s in seg.push_token(token))
    parts.extend(s.text for s in seg.flush(end_of_stream=True))
    joined = "".join(parts)
    assert "。。" not in joined
    assert joined == "那我讲一个。有只小兔子"
    assert parts == ["那我讲一个。", "有只小兔子"]


def test_d08_streaming_first_phrase_4_to_6_chars_cut_on_weak_boundary() -> None:
    """D-08: first phrase cuts on weak boundary with 4-6 chars for snappier TTFB."""
    # 4 characters + weak boundary
    seg = PhraseSegmenter(GenerationFence("d08-4char", 1, 1, 0))
    emitted = seg.push_token("我知道啦，")
    assert [s.text for s in emitted] == ["我知道啦，"]

    # 5 characters + weak boundary
    seg = PhraseSegmenter(GenerationFence("d08-5char", 1, 1, 0))
    emitted = seg.push_token("好的没问题，")
    assert [s.text for s in emitted] == ["好的没问题，"]

    # 6 characters + weak boundary
    seg = PhraseSegmenter(GenerationFence("d08-6char", 1, 1, 0))
    emitted = seg.push_token("今天天气真好，")
    assert [s.text for s in emitted] == ["今天天气真好，"]

    # Under 4 characters: should NOT cut on weak boundary alone
    seg = PhraseSegmenter(GenerationFence("d08-under4", 1, 1, 0))
    emitted = seg.push_token("好啊，")
    assert emitted == []


def test_d08_streaming_first_phrase_hard_wait_cut_at_4_chars() -> None:
    """At FIRST_HARD_WAIT_MS, four characters cut even without punctuation."""
    t0 = 1_000_000_000
    seg = PhraseSegmenter(GenerationFence("d08-hardwait", 1, 1, 0))

    emitted = seg.push_token("我们在", now_ns=t0)
    assert emitted == []

    emitted = seg.push_token("听", now_ns=t0 + 260_000_000)
    assert [s.text for s in emitted] == ["我们在听"]

