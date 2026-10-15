"""A guardian's daily device-chat allowance for a minor.

Stored in the guardian's session ``ConsentParams.extras`` and signed into the
minor's ``MAX_SESSION_SECONDS`` obligation extras under one key, as whole
minutes written in seconds. Nothing else reads or writes that key.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

DAILY_CHAT_SECONDS_KEY: Final = "daily_chat_seconds"
DAILY_CHAT_MINUTE_CHOICES: Final[tuple[int, ...]] = (15, 30, 45, 60)


@dataclass(frozen=True, slots=True)
class DailyChatBudget:
    """Minutes from ``DAILY_CHAT_MINUTE_CHOICES``. Anything else raises ValueError."""

    minutes: int

    def __post_init__(self) -> None:
        if type(self.minutes) is not int or self.minutes not in DAILY_CHAT_MINUTE_CHOICES:
            raise ValueError(f"daily chat minutes must be one of {DAILY_CHAT_MINUTE_CHOICES}")

    @property
    def seconds(self) -> int:
        return self.minutes * 60

    def admits(self, used_seconds: int) -> bool:
        """A new session may start while today's use is below the budget.

        Only admission consults the budget. An admitted session runs to its
        per-session cap even when that passes the budget.
        """
        return used_seconds < self.seconds


DEFAULT_DAILY_CHAT_BUDGET: Final = DailyChatBudget(30)


def parse_daily_chat_budget(extras: Iterable[Sequence[str]]) -> DailyChatBudget | None:
    """The budget under ``DAILY_CHAT_SECONDS_KEY``, or None when the key is absent.

    A present value that is not ASCII digits, not whole minutes, or not one of
    the choices raises ValueError.
    """
    for key, value in extras:
        if key != DAILY_CHAT_SECONDS_KEY:
            continue
        if not (value.isascii() and value.isdigit()) or int(value) % 60:
            raise ValueError(f"{DAILY_CHAT_SECONDS_KEY} must be whole minutes in seconds")
        return DailyChatBudget(int(value) // 60)
    return None


def effective_daily_chat_budget(extras: Iterable[Sequence[str]]) -> DailyChatBudget:
    """What applies: the guardian's budget, else the 30-minute default."""
    return parse_daily_chat_budget(extras) or DEFAULT_DAILY_CHAT_BUDGET


def with_daily_chat_budget(
    extras: Iterable[Sequence[str]], budget: DailyChatBudget | None
) -> tuple[tuple[str, str], ...]:
    """``extras`` with the key set to ``budget``, or dropped for None. Other keys kept, sorted."""
    kept = {key: value for key, value in extras if key != DAILY_CHAT_SECONDS_KEY}
    if budget is not None:
        kept[DAILY_CHAT_SECONDS_KEY] = str(budget.seconds)
    return tuple(sorted(kept.items()))
