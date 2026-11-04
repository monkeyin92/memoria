from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.control_api.app.child_archive_companion_recall import (
    FACT_VERSION_PREFIX,
    grounded_items_from_archive_views,
    merge_archive_ahead_of_catalog,
)
from services.digital_self.response_planner import GroundedItem, SourceRef
from services.guardian.child_archive_review import build_fact_view

pytestmark = pytest.mark.hermetic


def _view(
    *,
    record_id: str,
    text: str,
    status: str,
    reason_code: str,
    provenance: str,
    fact_version: int = 1,
    field: str = "preference",
) -> object:
    return build_fact_view(
        record_id=record_id,
        field=field,
        text=text,
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
        retention_expires_at=None,
        payload={
            "provenance": provenance,
            "source_session_id": "sess-1",
            "rule_version": "preference-extract-v2",
            "fact_version": fact_version,
            "polarity": "positive",
        },
        status=status,
        reason_code=reason_code,
        handled_at=datetime(2026, 11, 4, tzinfo=UTC),
        confidence=1.0 if status == "confirmed" else 0.75,
    )


def test_grounded_items_carry_record_id_and_fact_version() -> None:
    confirmed = _view(
        record_id="charch:ok",
        text="喜欢画画",
        status="confirmed",
        reason_code="parent_confirm",
        provenance="parent_confirm",
        fact_version=3,
    )
    pending = _view(
        record_id="charch:pending",
        text="喜欢恐龙",
        status="candidate",
        reason_code="child_archive_auto_extract",
        provenance="auto_extract",
    )
    items = grounded_items_from_archive_views((pending, confirmed))
    assert len(items) == 1
    item = items[0]
    assert item.item_id == "charch:ok"
    assert item.kind == "memory_claim"
    assert item.content == "喜好：喜欢画画"
    assert item.source_refs[0].entry_id == "charch:ok"
    assert item.source_refs[0].source_event_ids == (f"{FACT_VERSION_PREFIX}3",)


def test_auto_extract_confirmed_without_parent_reason_stays_out() -> None:
    illegal = _view(
        record_id="charch:bad",
        text="喜欢恐龙",
        status="confirmed",
        reason_code="child_archive_auto_extract",
        provenance="auto_extract",
    )
    assert grounded_items_from_archive_views((illegal,)) == ()


def test_merge_puts_archive_ahead_of_catalog() -> None:
    archive = (
        GroundedItem(
            item_id="charch:1",
            kind="memory_claim",
            content="喜好：喜欢画画",
            source_refs=(SourceRef("memory_claim", "charch:1", ("fact_version:1",)),),
        ),
    )
    catalog = (
        GroundedItem(
            item_id="claim-1",
            kind="memory_claim",
            content="求学经历：杭州",
            source_refs=(SourceRef("memory_claim", "claim-1", ("event-1",)),),
        ),
    )
    merged = merge_archive_ahead_of_catalog(archive, catalog)
    assert [item.item_id for item in merged] == ["charch:1", "claim-1"]
