import pytest
from services.common.daily_chat_limit import DailyChatBudget
from services.common.session_length import (
    DEFAULT_MAX_SESSION_SECONDS,
    issued_session_seconds,
    session_seconds_from_minutes,
)


def test_the_default_session_is_fifteen_minutes() -> None:
    assert DEFAULT_MAX_SESSION_SECONDS == 900
    assert issued_session_seconds(None) == 900


@pytest.mark.parametrize("seconds", [600, 601, 900, 1799, 1800])
def test_a_stored_length_inside_ten_to_thirty_minutes_is_issued_as_stored(seconds: int) -> None:
    assert issued_session_seconds(seconds) == seconds


@pytest.mark.parametrize("seconds", [0, 1, 599, 1801, 2700, 3600, -600])
def test_a_stored_length_outside_ten_to_thirty_minutes_is_issued_as_fifteen(seconds: int) -> None:
    assert issued_session_seconds(seconds) == 900


@pytest.mark.parametrize("stored", [True, False, 900.0, "900", "1200", b"900", [900]])
def test_a_stored_length_of_the_wrong_type_is_issued_as_fifteen(stored: object) -> None:
    assert issued_session_seconds(stored) == 900


@pytest.mark.parametrize(("minutes", "seconds"), [(10, 600), (15, 900), (30, 1800)])
def test_the_bind_form_accepts_ten_to_thirty_minutes(minutes: int, seconds: int) -> None:
    assert session_seconds_from_minutes(minutes) == seconds
    assert issued_session_seconds(session_seconds_from_minutes(minutes)) == seconds


@pytest.mark.parametrize("minutes", [0, 9, 31, 45, -10, True, 10.0, "15", None])
def test_the_bind_form_rejects_minutes_outside_the_range(minutes: object) -> None:
    assert session_seconds_from_minutes(minutes) is None
    assert issued_session_seconds(session_seconds_from_minutes(minutes)) == 900


def test_the_session_range_leaves_the_daily_budget_choices_alone() -> None:
    assert DailyChatBudget(45).seconds == 2700
