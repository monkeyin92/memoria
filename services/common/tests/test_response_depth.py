from __future__ import annotations

from services.common.response_depth import ResponseDepth, response_depth_for


def test_realtime_planning_defaults_to_a_brief_conclusion() -> None:
    policy = response_depth_for("帮我规划一下明天从南京到上海最快路线", realtime=True)

    assert policy.depth is ResponseDepth.BRIEF
    assert "结论" in policy.instruction


def test_explicit_detail_request_overrides_realtime_brief_default() -> None:
    policy = response_depth_for(
        "请详细说明明天从南京到上海的完整出行方案",
        realtime=True,
    )

    assert policy.depth is ResponseDepth.EXTENDED


def test_controlled_standard_turn_stays_brief_but_explicit_detail_is_preserved() -> None:
    assert response_depth_for("今天过得怎么样", controlled=True).depth is ResponseDepth.BRIEF
    assert (
        response_depth_for("请详细讲一个故事", controlled=True).depth
        is ResponseDepth.EXTENDED
    )
