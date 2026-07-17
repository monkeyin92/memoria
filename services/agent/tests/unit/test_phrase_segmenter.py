from __future__ import annotations

import pytest
from services.agent.src.orchestration.phrase_segmenter import segment_all


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
