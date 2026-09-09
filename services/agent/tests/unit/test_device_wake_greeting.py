"""Closed-set device wake greetings vary by local context, not free generation."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.delegation_coordinator import DelegationCoordinator
from services.agent.src.prompts import (
    DEVICE_WAKE_GREETING_PHRASES,
    DEVICE_WAKE_PHRASES,
    device_wake_phrase,
    hours_since_device_wake,
    is_allowlisted_device_phrase,
    remember_device_wake,
)

_TZ = ZoneInfo("Asia/Shanghai")


def _now(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).replace(tzinfo=_TZ)


def _phrases(session_prefix: str, **kwargs: object) -> set[str]:
    return {device_wake_phrase(f"{session_prefix}-{index}", **kwargs) for index in range(80)}


def test_all_wake_greeting_phrases_are_allowlisted() -> None:
    assert DEVICE_WAKE_PHRASES
    assert set(DEVICE_WAKE_PHRASES) <= DEVICE_WAKE_GREETING_PHRASES
    for phrase in DEVICE_WAKE_GREETING_PHRASES:
        assert is_allowlisted_device_phrase(phrase)
        intent = DelegationCoordinator.bridge_acknowledgement(
            phrase,
            fence=GenerationFence("wake-allowlist", 1, 1, 0),
            context_version=1,
            expires_at_ms=2_000,
            now_ms=1_000,
        )
        assert intent.tts_source == phrase


def test_naive_wake_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        device_wake_phrase("naive", now=datetime(2026, 9, 9, 8, 0))


def test_morning_weekday_can_use_period_greeting() -> None:
    now = _now("2026-09-09T08:00:00")
    phrases = _phrases("morning", now=now)
    assert phrases <= DEVICE_WAKE_GREETING_PHRASES
    assert phrases & {"早上好，我在。", "早呀，我来了。", "我在，今天也加油。"}


def test_night_weekday_can_use_period_greeting() -> None:
    now = _now("2026-09-09T23:10:00")
    phrases = _phrases("night", now=now)
    assert phrases <= DEVICE_WAKE_GREETING_PHRASES
    assert phrases & {"这么晚还醒着，我在。", "夜深了，我在。", "哎呀，好困呀。"}


def test_weekend_can_use_weekend_greeting() -> None:
    now = _now("2026-09-05T10:00:00")
    phrases = _phrases("weekend", now=now)
    assert phrases <= DEVICE_WAKE_GREETING_PHRASES
    assert phrases & {"周末好，我在。", "今天可以歇一歇，我在。"}


def test_rain_label_can_use_weather_greeting() -> None:
    now = _now("2026-09-09T15:00:00")
    phrases = _phrases("rain", now=now, weather_label="Rain")
    assert phrases <= DEVICE_WAKE_GREETING_PHRASES
    assert phrases & {"外面在下雨，我在。", "下雨了，我在这儿。"}
    unknown = _phrases("no-weather", now=now, weather_label="tornado")
    assert unknown.isdisjoint({"外面在下雨，我在。", "下雨了，我在这儿。"})


def test_companion_style_can_use_style_greeting() -> None:
    now = _now("2026-09-09T16:00:00")
    phrases = _phrases("style", now=now, companion_style_id="starlight")
    assert "我在，慢慢说就好。" in phrases
    assert phrases <= DEVICE_WAKE_GREETING_PHRASES


def test_long_absence_can_use_return_greeting() -> None:
    now = _now("2026-09-09T09:00:00")
    phrases = _phrases("absent", now=now, hours_since_last_wake=12.0)
    assert phrases & {"回来啦，我在。", "好久不见，我在。"}
    recent = _phrases("recent", now=now, hours_since_last_wake=1.0)
    assert recent.isdisjoint({"回来啦，我在。", "好久不见，我在。"})


def test_first_wake_matches_unparameterized_phrase() -> None:
    now = _now("2026-09-09T10:03:00")
    session_id = "first-wake-session"
    assert device_wake_phrase(session_id, now=now) == device_wake_phrase(
        session_id, now=now, hours_since_last_wake=None
    )
    assert device_wake_phrase(session_id, now=now) == device_wake_phrase(
        session_id, now=now, hours_since_last_wake=0.0
    )


def test_device_wake_recency_is_device_scoped() -> None:
    device_id = "wake-greeting-history-device"
    start = _now("2026-09-09T08:00:00")
    assert hours_since_device_wake(device_id, start) is None
    remember_device_wake(device_id, start)
    later = _now("2026-09-09T20:00:00")
    assert hours_since_device_wake(device_id, later) == 12.0
    assert hours_since_device_wake("other-wake-greeting-device", later) is None
