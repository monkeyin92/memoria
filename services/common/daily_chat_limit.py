from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

DAILY_CHAT_SECONDS_KEY: Final = "daily_chat_seconds"
DAILY_CHAT_MINUTE_CHOICES: Final[tuple[int, ...]] = (15, 30, 45, 60)


@dataclass(frozen=True, slots=True)
class DailyChatBudget:
    minutes: int

    def __post_init__(self) -> None:
        if type(self.minutes) is not int or self.minutes not in DAILY_CHAT_MINUTE_CHOICES:
            raise ValueError(f"daily chat minutes must be one of {DAILY_CHAT_MINUTE_CHOICES}")

    @property
    def seconds(self) -> int:
        return self.minutes * 60

    def admits(self, used_seconds: int) -> bool:
        return used_seconds < self.seconds


DEFAULT_DAILY_CHAT_BUDGET: Final = DailyChatBudget(30)


def parse_daily_chat_budget(extras: Iterable[Sequence[str]]) -> DailyChatBudget | None:
    for key, value in extras:
        if key != DAILY_CHAT_SECONDS_KEY:
            continue
        if not (value.isascii() and value.isdigit()) or int(value) % 60:
            raise ValueError(f"{DAILY_CHAT_SECONDS_KEY} must be whole minutes in seconds")
        return DailyChatBudget(int(value) // 60)
    return None


def effective_daily_chat_budget(extras: Iterable[Sequence[str]]) -> DailyChatBudget:
    return parse_daily_chat_budget(extras) or DEFAULT_DAILY_CHAT_BUDGET


def with_daily_chat_budget(
    extras: Iterable[Sequence[str]], budget: DailyChatBudget | None
) -> tuple[tuple[str, str], ...]:
    kept = {key: value for key, value in extras if key != DAILY_CHAT_SECONDS_KEY}
    if budget is not None:
        kept[DAILY_CHAT_SECONDS_KEY] = str(budget.seconds)
    return tuple(sorted(kept.items()))
