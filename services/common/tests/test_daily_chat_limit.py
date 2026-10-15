import pytest
from services.common.daily_chat_limit import (
    DailyChatBudget,
    effective_daily_chat_budget,
    parse_daily_chat_budget,
    with_daily_chat_budget,
)


@pytest.mark.parametrize("minutes", [14, 20, 61, 0, True, 30.0, "30"])
def test_budget_rejects_minutes_outside_the_choices(minutes: object) -> None:
    with pytest.raises(ValueError):
        DailyChatBudget(minutes)


def test_budget_admits_a_new_session_only_below_the_allowance() -> None:
    budget = DailyChatBudget(30)

    assert budget.seconds == 1800
    assert budget.admits(1799) is True
    assert budget.admits(1800) is False


def test_parse_reads_the_key_among_other_extras() -> None:
    extras = (("data_classification", "ephemeral"), ("daily_chat_seconds", "2700"))

    assert parse_daily_chat_budget(extras) == DailyChatBudget(45)
    assert parse_daily_chat_budget([("data_classification", "ephemeral")]) is None


@pytest.mark.parametrize("value", ["1801", "+900", "abc", "１８００", "1200", "0", ""])
def test_parse_rejects_a_malformed_value(value: str) -> None:
    with pytest.raises(ValueError):
        parse_daily_chat_budget([("daily_chat_seconds", value)])


def test_effective_budget_falls_back_to_thirty_minutes() -> None:
    assert effective_daily_chat_budget(()) == DailyChatBudget(30)
    assert effective_daily_chat_budget([("daily_chat_seconds", "3600")]) == DailyChatBudget(60)


def test_with_budget_sets_or_drops_the_key_and_keeps_other_extras() -> None:
    extras = (("zone", "z"), ("daily_chat_seconds", "900"), ("app", "a"))

    assert with_daily_chat_budget(extras, DailyChatBudget(45)) == (
        ("app", "a"),
        ("daily_chat_seconds", "2700"),
        ("zone", "z"),
    )
    assert with_daily_chat_budget(extras, None) == (("app", "a"), ("zone", "z"))
