"""Parent-facing retention notice copy for child_archive idle expiry.

Exact owner-approved wording. Notices never include child verbatim text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from services.guardian.child_memory import ARCHIVE_IDLE_TTL

NoticeKind = Literal["archive_idle_30d", "archive_idle_7d", "archive_review_90d"]

NOTICE_ARCHIVE_IDLE_30D: Final = (
    "孩子已经有 150 天没和 memoria 聊天了。"
    "如果再过 30 天还没有使用，长期记忆档案会自动删除。"
    "想保留的话，打开小程序就能导出。"
)
NOTICE_ARCHIVE_IDLE_7D: Final = (
    "7 天后，孩子的长期记忆档案会自动删除，删除后无法恢复。"
    "如果需要，可以现在导出。"
)
NOTICE_ARCHIVE_REVIEW_90D: Final = (
    "memoria 记住了孩子的一些喜好和事情，请花一分钟看一看，不需要的可以随时删除。"
)

_COPY: Final[dict[NoticeKind, str]] = {
    "archive_idle_30d": NOTICE_ARCHIVE_IDLE_30D,
    "archive_idle_7d": NOTICE_ARCHIVE_IDLE_7D,
    "archive_review_90d": NOTICE_ARCHIVE_REVIEW_90D,
}

IDLE_WARN_30D: Final = timedelta(days=150)
IDLE_WARN_7D: Final = timedelta(days=173)
REVIEW_EVERY: Final = timedelta(days=90)


@dataclass(frozen=True, slots=True)
class RetentionNotice:
    kind: NoticeKind
    text: str


def notice_text(kind: NoticeKind) -> str:
    return _COPY[kind]


def idle_notices_due(
    *,
    last_active_at: datetime,
    now: datetime | None = None,
) -> tuple[RetentionNotice, ...]:
    """Return idle-deletion warnings that apply at ``now`` for one subject."""

    stamped = (now or datetime.now(UTC)).astimezone(UTC)
    active = last_active_at.astimezone(UTC)
    idle_for = stamped - active
    due: list[RetentionNotice] = []
    if idle_for >= IDLE_WARN_7D and idle_for < ARCHIVE_IDLE_TTL:
        due.append(
            RetentionNotice(kind="archive_idle_7d", text=NOTICE_ARCHIVE_IDLE_7D)
        )
    elif idle_for >= IDLE_WARN_30D and idle_for < ARCHIVE_IDLE_TTL:
        due.append(
            RetentionNotice(kind="archive_idle_30d", text=NOTICE_ARCHIVE_IDLE_30D)
        )
    return tuple(due)


def review_notice_due(
    *,
    last_review_notice_at: datetime | None,
    now: datetime | None = None,
) -> RetentionNotice | None:
    """90-day review reminder; no response does not delete the archive."""

    stamped = (now or datetime.now(UTC)).astimezone(UTC)
    if last_review_notice_at is None:
        return RetentionNotice(
            kind="archive_review_90d", text=NOTICE_ARCHIVE_REVIEW_90D
        )
    if stamped - last_review_notice_at.astimezone(UTC) >= REVIEW_EVERY:
        return RetentionNotice(
            kind="archive_review_90d", text=NOTICE_ARCHIVE_REVIEW_90D
        )
    return None


__all__ = [
    "IDLE_WARN_30D",
    "IDLE_WARN_7D",
    "NOTICE_ARCHIVE_IDLE_30D",
    "NOTICE_ARCHIVE_IDLE_7D",
    "NOTICE_ARCHIVE_REVIEW_90D",
    "REVIEW_EVERY",
    "NoticeKind",
    "RetentionNotice",
    "idle_notices_due",
    "notice_text",
    "review_notice_due",
]
