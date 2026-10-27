"""Unit tests for story grounding, local story priority, canon retrieval, and voice volume control."""

from __future__ import annotations

from services.agent.src.live_lookup_router import keyword_requires_live_media_lookup
from services.agent.src.orchestration.prosody import speech_plan_for_turn
from services.common.companion_response_safety import fixed_companion_reply
from services.common.story_grounding import (
    DEFAULT_VOLUME_STEP,
    VOLUME_DOWN_REPLY,
    VOLUME_UP_REPLY,
    build_grounded_story_system_instruction,
    build_ip_story_search_query,
    build_local_story_prompt_instruction,
    extract_story_ip,
    is_continuation_story_request,
    is_pure_volume_command,
    is_story_request,
    is_volume_command,
    match_local_story,
)


def test_is_story_request_detection() -> None:
    assert is_story_request("讲一个睡前故事")
    assert is_story_request("给我讲个故事吧")
    assert is_story_request("我想听汪汪队的故事")
    assert is_story_request("放个绘本故事")
    assert is_story_request("再讲一个")
    assert is_story_request("换一个故事")
    assert not is_story_request("今天天气怎么样")
    assert not is_story_request("声音大一点")
    assert not is_story_request("明天早上七点叫我起床")


def test_is_continuation_story_request() -> None:
    assert is_continuation_story_request("再讲一个")
    assert is_continuation_story_request("还想听一个")
    assert is_continuation_story_request("换一个故事")
    assert is_continuation_story_request("再来一个")
    assert not is_continuation_story_request("讲一个小星星的枕头")
    assert not is_continuation_story_request("声音小一点")


def test_match_local_story_explicit_aliases() -> None:
    assert match_local_story("我想听小星星的枕头") == "story_01"
    assert match_local_story("讲月亮不着急的故事") == "story_02"
    assert match_local_story("讲大熊和森林里的悄悄话") == "story_03"
    assert match_local_story("放夜晚安抚") == "night_soothe"
    assert match_local_story("帮我哄睡") == "night_soothe"


def test_match_local_story_prioritizes_generic_bedtime_when_no_ip_named() -> None:
    # Generic bedtime requests without an external IP resolve to story_01
    assert match_local_story("讲个睡前故事", prioritize_generic=True) == "story_01"
    assert match_local_story("我想听睡前故事", prioritize_generic=True) == "story_01"
    assert match_local_story("讲个睡前故事", prioritize_generic=False) is None

    # An external IP story must NOT match local generic story
    assert match_local_story("我想听汪汪队的故事", prioritize_generic=True) is None
    assert match_local_story("讲一个小猪佩奇的故事", prioritize_generic=True) is None


def test_extract_story_ip_from_known_child_ips() -> None:
    assert extract_story_ip("我想听汪汪队的故事") == "汪汪队"
    assert extract_story_ip("讲一个汪汪队立大功") == "汪汪队"
    assert extract_story_ip("我想听阿奇的故事") == "汪汪队"
    assert extract_story_ip("讲一个小猪佩奇") == "小猪佩奇"
    assert extract_story_ip("我想听奥特曼的故事") == "奥特曼"
    assert extract_story_ip("讲迪迦奥特曼") == "奥特曼"
    assert extract_story_ip("讲海底小纵队") == "海底小纵队"
    assert extract_story_ip("我想听超级飞侠乐迪") == "超级飞侠"
    assert extract_story_ip("讲熊出没的故事") == "熊出没"
    assert extract_story_ip("讲喜羊羊与灰太狼的故事") == "喜羊羊"


def test_extract_story_ip_generic_regex() -> None:
    assert extract_story_ip("我想听孙悟空的故事") == "孙悟空"
    assert extract_story_ip("讲一个哪吒的故事") == "哪吒"
    assert extract_story_ip("给我讲个哪吒的故事吧") == "哪吒"
    assert extract_story_ip("我想听白雪公主的故事") == "白雪公主"
    assert extract_story_ip("我想听孙悟空的故事") == "孙悟空"


def test_extract_story_ip_excludes_common_story_words_and_local_titles() -> None:
    assert extract_story_ip("讲一个睡前故事") is None
    assert extract_story_ip("讲个好听的故事") is None
    assert extract_story_ip("讲一个小星星的枕头") is None
    assert extract_story_ip("讲月亮不着急") is None
    assert extract_story_ip("请给我讲一个森林里的故事") is None
    assert extract_story_ip("讲一个关于友谊的故事") is None
    assert extract_story_ip("讲个城堡里的故事") is None
    assert extract_story_ip("给我讲个小故事吧") is None
    assert extract_story_ip("讲个小故事") is None
    assert extract_story_ip("讲一个小故事") is None
    assert extract_story_ip("我想听一个小故事") is None
    assert extract_story_ip("我想听个小故事") is None
    assert extract_story_ip("讲个新故事") is None
    assert extract_story_ip("我想听一个新的故事") is None
    assert extract_story_ip("讲个有趣的故事") is None
    assert extract_story_ip("讲个短故事") is None
    assert extract_story_ip("讲个大故事") is None


def test_build_ip_story_search_query() -> None:
    query = build_ip_story_search_query("汪汪队")
    assert query == "汪汪队立大功 剧情概要 角色设定 主要故事"

    query_peppa = build_ip_story_search_query("小猪佩奇")
    assert query_peppa == "小猪佩奇 剧情概要 角色设定 主要故事"

    query_generic = build_ip_story_search_query("葫芦兄弟")
    assert query_generic == "葫芦兄弟 剧情概要 角色设定 主要故事"


def test_build_grounded_story_system_instruction_with_canon() -> None:
    instruction = build_grounded_story_system_instruction(
        "汪汪队",
        "阿奇驾驶警车，毛毛驾驶消防车灭火救人。",
    )
    assert "《汪汪队立大功》" in instruction
    assert "阿奇驾驶警车" in instruction
    assert "严禁瞎编" in instruction
    assert "严格遵守上述《汪汪队立大功》的官方角色设定" in instruction


def test_build_grounded_story_system_instruction_fallback_to_known_canon() -> None:
    instruction = build_grounded_story_system_instruction("汪汪队", "")
    assert "莱德队长" in instruction
    assert "阿奇" in instruction
    assert "毛毛" in instruction
    assert "天天" in instruction


def test_build_local_story_prompt_instruction() -> None:
    inst_01 = build_local_story_prompt_instruction("story_01")
    assert "《小星星的枕头》" in inst_01
    assert "小青蛙" in inst_01

    inst_02 = build_local_story_prompt_instruction("story_02")
    assert "《月亮不着急》" in inst_02
    assert "小兔子" in inst_02


def test_volume_command_parsing() -> None:
    is_vol, delta, reply = is_volume_command("声音大一点")
    assert is_vol is True
    assert delta == DEFAULT_VOLUME_STEP
    assert reply == VOLUME_UP_REPLY

    is_vol, delta, reply = is_volume_command("声音小一点")
    assert is_vol is True
    assert delta == -DEFAULT_VOLUME_STEP
    assert reply == VOLUME_DOWN_REPLY

    is_vol, delta, reply = is_volume_command("把音量调大")
    assert is_vol is True
    assert delta == DEFAULT_VOLUME_STEP

    is_vol, delta, reply = is_volume_command("太吵了小声点")
    assert is_vol is True
    assert delta == -DEFAULT_VOLUME_STEP

    is_vol, delta, reply = is_volume_command("讲个故事")
    assert is_vol is False
    assert delta == 0
    assert reply is None

    # A pitch request shares the words but must not move the volume.
    assert is_volume_command("请把音调调低一点")[0] is False
    assert is_volume_command("请把音调调高一点")[0] is False


def test_is_pure_volume_command() -> None:
    assert is_pure_volume_command("声音大一点")[0] is True
    assert is_pure_volume_command("请帮我声音小一点")[0] is True
    assert is_pure_volume_command("音量调大")[0] is True
    assert is_pure_volume_command("声音小一点，给我讲个故事")[0] is False


def test_fixed_companion_reply_handles_pure_volume_immediately() -> None:
    assert fixed_companion_reply(query="声音大一点", is_companion=True) == VOLUME_UP_REPLY
    assert fixed_companion_reply(query="声音小一点", is_companion=True) == VOLUME_DOWN_REPLY
    assert fixed_companion_reply(query="请帮我把音量调小", is_companion=True) == VOLUME_DOWN_REPLY
    assert fixed_companion_reply(query="声音小一点", is_companion=False) is None


def test_prosody_carries_volume_in_pitch_and_keeps_it_across_turns() -> None:
    # 1. Start from neutral (0), increase volume by 1 step -> +DEFAULT_VOLUME_STEP
    plan_up = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="声音大一点",
        companion_id="taoxi",
    )
    assert plan_up.pitch // 16 * DEFAULT_VOLUME_STEP == DEFAULT_VOLUME_STEP

    # 2. Next turn asks for a story without volume command -> retains +DEFAULT_VOLUME_STEP
    plan_kept = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="给我讲个故事",
        companion_id="taoxi",
        previous=plan_up,
    )
    assert plan_kept.pitch // 16 * DEFAULT_VOLUME_STEP == DEFAULT_VOLUME_STEP

    # 3. Next turn asks to decrease volume -> goes from +15 down to 0
    plan_down = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="声音小一点",
        companion_id="taoxi",
        previous=plan_kept,
    )
    assert plan_down.pitch // 16 * DEFAULT_VOLUME_STEP == 0

    # 4. Another decrease turn -> goes from 0 down to -15
    plan_down_again = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="声音小一点",
        companion_id="taoxi",
        previous=plan_down,
    )
    assert plan_down_again.pitch // 16 * DEFAULT_VOLUME_STEP == -DEFAULT_VOLUME_STEP


def test_live_lookup_router_triggers_for_ip_story() -> None:
    # IP stories trigger live lookup to search canon lore
    assert keyword_requires_live_media_lookup("我想听汪汪队的故事") is True
    assert keyword_requires_live_media_lookup("讲一个小猪佩奇") is True

    # Pre-stored local stories and generic stories do not trigger live lookup
    assert keyword_requires_live_media_lookup("讲一个小星星的枕头") is False
    assert keyword_requires_live_media_lookup("讲一个睡前故事") is False
