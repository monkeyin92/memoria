"""Exact owner-approved retention notice copy and idle windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.guardian.retention_notices import (
    NOTICE_ARCHIVE_IDLE_7D,
    NOTICE_ARCHIVE_IDLE_30D,
    NOTICE_ARCHIVE_REVIEW_90D,
    idle_notices_due,
    notice_text,
    review_notice_due,
)


def test_notice_copy_is_exact() -> None:
    assert notice_text("archive_idle_30d") == (
        "孩子已经有 150 天没和 memoria 聊天了。"
        "如果再过 30 天还没有使用，长期记忆档案会自动删除。"
        "想保留的话，让孩子再和 memoria 聊一聊就行。"
    )
    assert notice_text("archive_idle_7d") == (
        "7 天后，孩子的长期记忆档案会自动删除，删除后无法恢复。"
    )
    assert notice_text("archive_review_90d") == (
        "memoria 记住了孩子的一些喜好和事情，请花一分钟看一看，不需要的可以随时删除。"
    )
    assert notice_text("archive_idle_30d") == NOTICE_ARCHIVE_IDLE_30D
    assert notice_text("archive_idle_7d") == NOTICE_ARCHIVE_IDLE_7D
    assert notice_text("archive_review_90d") == NOTICE_ARCHIVE_REVIEW_90D


def test_idle_notice_windows() -> None:
    now = datetime(2026, 10, 13, tzinfo=UTC)
    at_150 = idle_notices_due(last_active_at=now - timedelta(days=150), now=now)
    assert len(at_150) == 1
    assert at_150[0].kind == "archive_idle_30d"
    assert at_150[0].text == NOTICE_ARCHIVE_IDLE_30D

    at_173 = idle_notices_due(last_active_at=now - timedelta(days=173), now=now)
    assert len(at_173) == 1
    assert at_173[0].kind == "archive_idle_7d"
    assert at_173[0].text == NOTICE_ARCHIVE_IDLE_7D

    fresh = idle_notices_due(last_active_at=now - timedelta(days=10), now=now)
    assert fresh == ()


def test_review_notice_every_90_days() -> None:
    now = datetime(2026, 10, 13, tzinfo=UTC)
    first = review_notice_due(last_review_notice_at=None, now=now)
    assert first is not None
    assert first.text == NOTICE_ARCHIVE_REVIEW_90D
    recent = review_notice_due(
        last_review_notice_at=now - timedelta(days=10), now=now
    )
    assert recent is None
    due = review_notice_due(
        last_review_notice_at=now - timedelta(days=90), now=now
    )
    assert due is not None
    assert due.kind == "archive_review_90d"
