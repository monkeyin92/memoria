"""Per-child daily device chat usage. The only reader and writer of ``daily_chat_usage``.

A usage day is the calendar day in ``MEMORIA_TIMEZONE`` (Asia/Shanghai) on this
process's clock, so the reset is local midnight. Only today's total is ever read.
A session that straddles midnight credits today with the part after midnight,
and every write prunes earlier days.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from services.control_api.app.database.backend import DbConnection

if TYPE_CHECKING:
    from services.control_api.app.database import MemoryStore

__all__ = [
    "ChatUsageConflictError",
    "ChatUsageLedger",
    "ChatUsageToday",
    "delete_chat_usage",
]


class ChatUsageConflictError(ValueError):
    """An accrual id came back with another subject or another duration."""


@dataclass(frozen=True, slots=True)
class ChatUsageToday:
    """A subject's device chat so far on ``day``."""

    day: date
    seconds: int

    @property
    def minutes(self) -> int:
        """Rounded down, so a budget of N minutes is spent exactly when this reaches N."""
        return self.seconds // 60


class ChatUsageLedger:
    def __init__(
        self,
        store: MemoryStore,
        *,
        zone: ZoneInfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store, self._zone, self._clock = store, zone, clock

    def record(self, *, accrual_id: str, subject_id: str, elapsed_seconds: int) -> None:
        """Credit one admitted session that ended now. Idempotent per ``accrual_id``.

        A repeated report changes nothing, even after midnight. The same
        ``accrual_id`` with another subject or duration raises
        ChatUsageConflictError.
        """
        now = self._now()
        today = now.date().isoformat()
        since_midnight = now - now.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds = min(elapsed_seconds, math.ceil(since_midnight.total_seconds()))
        with self._store.connection() as connection:
            if seconds > 0:
                connection.execute(
                    """
                    INSERT INTO daily_chat_usage (
                        accrual_id, subject_id, usage_day, elapsed_seconds, seconds, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (accrual_id) DO NOTHING
                    """,
                    (accrual_id, subject_id, today, elapsed_seconds, seconds, now.isoformat()),
                )
            stored = connection.execute(
                "SELECT subject_id, elapsed_seconds FROM daily_chat_usage WHERE accrual_id = ?",
                (accrual_id,),
            ).fetchone()
            if stored is not None and tuple(stored) != (subject_id, elapsed_seconds):
                raise ChatUsageConflictError(accrual_id)
            connection.execute("DELETE FROM daily_chat_usage WHERE usage_day < ?", (today,))

    def used_today(self, subject_id: str) -> ChatUsageToday:
        """The subject's total on today's local date, 0 without a row."""
        today = self._now().date()
        with self._store.connection() as connection:
            total = connection.execute(
                "SELECT COALESCE(SUM(seconds), 0) FROM daily_chat_usage"
                " WHERE subject_id = ? AND usage_day = ?",
                (subject_id, today.isoformat()),
            ).fetchone()[0]
        return ChatUsageToday(day=today, seconds=int(total))

    def _now(self) -> datetime:
        return self._clock().astimezone(self._zone)


def delete_chat_usage(connection: DbConnection, *, subject_id: str) -> int:
    """Erase a subject's rows inside the caller's transaction. Returns the count."""
    return connection.execute(
        "DELETE FROM daily_chat_usage WHERE subject_id = ?", (subject_id,)
    ).rowcount
