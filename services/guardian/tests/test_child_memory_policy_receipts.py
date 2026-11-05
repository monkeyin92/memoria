"""Stable policy_receipt_id keys for session summary and archive writes."""

from __future__ import annotations

import pytest
from services.guardian.child_memory import (
    archive_fact_policy_receipt,
    dialogue_summary_policy_receipt,
)


def test_dialogue_summary_receipt_is_stable_and_subject_scoped() -> None:
    first = dialogue_summary_policy_receipt(
        subject_id="child-a", session_id="sess-1"
    )
    again = dialogue_summary_policy_receipt(
        subject_id="child-a", session_id="sess-1"
    )
    other_session = dialogue_summary_policy_receipt(
        subject_id="child-a", session_id="sess-2"
    )
    other_subject = dialogue_summary_policy_receipt(
        subject_id="child-b", session_id="sess-1"
    )
    assert first == again
    assert first != other_session
    assert first != other_subject
    assert first.startswith("child-dialogue-summary:")
    assert len(first) <= 128


def test_dialogue_summary_receipt_hashes_when_parts_overflow() -> None:
    subject = "s" * 80
    session = "x" * 80
    receipt = dialogue_summary_policy_receipt(subject_id=subject, session_id=session)
    assert len(receipt) <= 128
    assert receipt.startswith("child-dialogue-summary:")
    assert receipt == dialogue_summary_policy_receipt(
        subject_id=subject, session_id=session
    )


def test_archive_fact_receipt_is_stable_per_field_text() -> None:
    first = archive_fact_policy_receipt(
        subject_id="child-a", field="preference", text="喜欢恐龙"
    )
    again = archive_fact_policy_receipt(
        subject_id="child-a", field="preference", text=" 喜欢恐龙 "
    )
    other = archive_fact_policy_receipt(
        subject_id="child-a", field="preference", text="喜欢画画"
    )
    assert first == again
    assert first != other
    assert len(first) <= 128


def test_policy_receipt_rejects_empty_parts() -> None:
    with pytest.raises(ValueError):
        dialogue_summary_policy_receipt(subject_id="child", session_id=" ")
    with pytest.raises(ValueError):
        archive_fact_policy_receipt(subject_id="child", field="preference", text="")
