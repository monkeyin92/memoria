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


# --- 2026-10-01: a child's "为什么" is not a request for a lecture ---------------------------------


def test_a_why_question_is_an_extended_answer_for_an_adult_as_before() -> None:
    assert response_depth_for("天空为什么是蓝色的？").depth is ResponseDepth.EXTENDED
    assert response_depth_for("天空为什么是蓝色的？", audience="adult_companion").depth is (
        ResponseDepth.EXTENDED
    )


def test_a_why_question_gets_a_short_standard_answer_for_a_child_and_an_elder() -> None:
    for audience in ("student_minor", "senior_companion"):
        for query in ("天空为什么是蓝色的？", "这个怎么做？", "你给我解释原因吧"):
            policy = response_depth_for(query, audience=audience)
            assert policy.depth is ResponseDepth.STANDARD, (audience, query)
            assert "两三句短话" in policy.instruction and "不要列条目" in policy.instruction


def test_an_explicit_request_for_more_is_still_extended_for_a_child_and_an_elder() -> None:
    for audience in ("student_minor", "senior_companion"):
        for query in ("给我讲个故事吧", "请详细说说", "再多讲一点", "继续"):
            policy = response_depth_for(query, audience=audience)
            assert policy.depth is ResponseDepth.EXTENDED, (audience, query)
            assert "最多约一分钟" in policy.instruction


def test_short_audiences_keep_brief_and_controlled_behaviour() -> None:
    assert response_depth_for("一句话说完", audience="student_minor").depth is ResponseDepth.BRIEF
    assert (
        response_depth_for("今天过得怎么样", controlled=True, audience="senior_companion").depth
        is ResponseDepth.BRIEF
    )
    assert response_depth_for("今天天气怎么样", realtime=True, audience="student_minor").depth is (
        ResponseDepth.BRIEF
    )


def test_every_way_to_ask_for_a_story_is_an_extended_answer_but_talking_about_one_is_not() -> None:
    """2026-10-01: only "讲个故事" counted, so "给我讲一个故事吧" was cut off after four sentences."""

    for audience in (None, "student_minor", "senior_companion"):
        for query in (
            "给我讲一个故事吧。",
            "讲故事",
            "我想听故事",
            "来个故事",
            "再讲一个故事",
            "说个睡前故事",
            "讲一个很短的故事",
            "讲个童话吧",
            "给我读一个故事",
        ):
            assert response_depth_for(query, audience=audience).depth is ResponseDepth.EXTENDED, (
                audience,
                query,
            )
        for query in ("我今天听了一个故事", "这个故事不好玩", "讲一个很短的笑话吧", "你会讲笑话吗"):
            assert response_depth_for(query, audience=audience).depth is ResponseDepth.STANDARD, (
                audience,
                query,
            )
