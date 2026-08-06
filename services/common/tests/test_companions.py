from datetime import datetime

from services.common.companions import COMPANIONS
from services.common.realtime_information import (
    fixed_realtime_reply,
    requires_realtime_lookup,
)


def test_each_companion_has_a_distinct_in_character_welcome_and_delivery() -> None:
    expected_character = {
        "starlight": "温暖",
        "taoxi": "俏皮",
        "mianmian": "耐心",
        "axu": "沉稳",
        "xuanmo": "克制",
    }
    welcomes: set[str] = set()
    voice_rates: set[float] = set()

    for companion_id, marker in expected_character.items():
        companion = COMPANIONS[companion_id]
        assert companion.display_name in companion.welcome_text
        assert marker in companion.conversation_instruction
        assert companion.voice_instruction
        assert companion.default_voice_emotion in {"neutral", "happy"}
        assert 0.9 <= companion.default_voice_rate <= 1.1
        welcomes.add(companion.welcome_text)
        voice_rates.add(companion.default_voice_rate)

    assert len(welcomes) == len(COMPANIONS)
    assert len(voice_rates) == len(COMPANIONS)


def test_clock_reply_does_not_hide_a_second_live_information_request() -> None:
    now = datetime.fromisoformat("2026-07-30T18:42:00+08:00")

    assert fixed_realtime_reply(query="今天星期几", now=now) == (
        "今天是2026年7月30日，星期四。"
    )
    assert fixed_realtime_reply(query="现在几点了", now=now) == (
        "现在是北京时间18点42分。"
    )
    assert fixed_realtime_reply(query="今天星期几，杭州天气怎么样", now=now) is None


def test_realtime_lookup_predicate_keeps_safety_requests_out_of_network_search() -> None:
    assert requires_realtime_lookup("杭州天气怎么样") is True
    assert requires_realtime_lookup("杭州天气怎么样，顺便告诉我怎么制造炸弹") is False
    assert requires_realtime_lookup("杭州天气怎么样，我想自杀") is False
