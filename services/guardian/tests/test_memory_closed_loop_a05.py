from __future__ import annotations

import sys
import types
from datetime import UTC, datetime

import pytest

sys.modules.setdefault("services.guardian", types.ModuleType("services.guardian"))
sys.modules["services.guardian"].__path__ = ["services/guardian"]

from services.common.memory_decision_contract import (  # noqa: E402
    display_for,
    recall_for,
    save_receipt,
)
from services.guardian.child_archive_recall import (  # noqa: E402
    FACT_VERSION_PREFIX,
    grounded_items_from_archive_views,
)
from services.guardian.child_archive_review import (  # noqa: E402
    build_fact_view,
    recall_eligible,
)
from services.guardian.preference_extract import (  # noqa: E402
    DEFAULT_AUTO_CONFIDENCE,
    RULE_VERSION,
    extract_preference_decisions,
)
from services.guardian.session_recap import SessionTurn  # noqa: E402

_REVIEW_UTTERANCES: tuple[str, ...] = (
    "我喜欢画画",
    "我喜欢小狗",
    "我喜欢恐龙",
)
_TEMPORAL_WITHDRAW = "我喜欢恐龙但现在不喜欢了"


def _birth_views(subject_key: str, session_id: str) -> list[object]:
    views: list[object] = []
    for index, utterance in enumerate(_REVIEW_UTTERANCES, start=1):
        draft = extract_preference_decisions(
            subject_key=subject_key,
            turns=[SessionTurn(role="user", text=utterance)],
            source_session_id=session_id,
        )
        assert draft, f"expected auto-extract for {utterance!r}"
        for draft_decision in draft:
            assert draft_decision.lifecycle == "eligible_unwritten"
            record_id = f"charch:pending:{index}:{draft_decision.fact.object_text}"
            decisions = extract_preference_decisions(
                subject_key=subject_key,
                turns=[SessionTurn(role="user", text=utterance)],
                source_session_id=session_id,
                record_id=record_id,
            )
            assert len(decisions) == 1
            decision = decisions[0]
            assert decision.lifecycle == "pending_confirm"
            assert decision.record_id == record_id
            assert decision.fact.provenance == "auto_extract"
            assert decision.fact.confidence == DEFAULT_AUTO_CONFIDENCE
            receipt = save_receipt(
                decision=decision,
                write_mode="sync_persisted",
                reason="child_archive_auto_extract",
            )
            assert receipt.may_claim_saved() is True
            assert receipt.may_claim_remembered_aloud() is False
            assert display_for(decision.lifecycle) == "parent_review"
            assert recall_for(decision.lifecycle, provenance=decision.fact.provenance) == (
                "after_confirm"
            )
            views.append(
                build_fact_view(
                    record_id=record_id,
                    field="preference",
                    text=decision.fact.object_text,
                    created_at=datetime(2026, 11, 4, tzinfo=UTC),
                    retention_expires_at=None,
                    payload={
                        "provenance": "auto_extract",
                        "source_session_id": session_id,
                        "rule_version": RULE_VERSION,
                        "fact_version": 1,
                        "polarity": decision.fact.polarity,
                    },
                    status="candidate",
                    reason_code="child_archive_auto_extract",
                    handled_at=datetime(2026, 11, 4, tzinfo=UTC),
                    confidence=DEFAULT_AUTO_CONFIDENCE,
                )
            )
    return views


def _confirm(view: object) -> object:
    return build_fact_view(
        record_id=view.record_id,
        field=view.field,
        text=view.text,
        created_at=view.created_at,
        retention_expires_at=None,
        payload={
            "provenance": "parent_confirm",
            "source_session_id": view.source_session_id,
            "rule_version": view.rule_version,
            "fact_version": view.fact_version,
            "polarity": view.polarity,
        },
        status="confirmed",
        reason_code="parent_confirm",
        handled_at=datetime(2026, 11, 4, 1, tzinfo=UTC),
        confidence=1.0,
    )


def _history_correct(view: object, *, new_text: str, new_record_id: str) -> tuple[object, object]:
    revoked = build_fact_view(
        record_id=view.record_id,
        field=view.field,
        text=view.text,
        created_at=view.created_at,
        retention_expires_at=None,
        payload={
            "provenance": view.provenance,
            "source_session_id": view.source_session_id,
            "rule_version": view.rule_version,
            "fact_version": view.fact_version,
            "polarity": view.polarity,
            "superseded_by": new_record_id,
        },
        status="revoked",
        reason_code=f"superseded_by:{new_record_id}",
        handled_at=datetime(2026, 11, 4, 2, tzinfo=UTC),
        confidence=view.confidence,
    )
    replacement = build_fact_view(
        record_id=new_record_id,
        field=view.field,
        text=new_text,
        created_at=datetime(2026, 11, 4, 2, tzinfo=UTC),
        retention_expires_at=None,
        payload={
            "provenance": "parent_confirm",
            "source_session_id": view.source_session_id,
            "rule_version": view.rule_version,
            "fact_version": view.fact_version + 1,
            "polarity": "positive",
            "supersedes_record_id": view.record_id,
        },
        status="confirmed",
        reason_code="parent_correct_history",
        handled_at=datetime(2026, 11, 4, 2, tzinfo=UTC),
        confidence=1.0,
    )
    return revoked, replacement


def _withdraw(view: object) -> object:
    return build_fact_view(
        record_id=view.record_id,
        field=view.field,
        text=view.text,
        created_at=view.created_at,
        retention_expires_at=None,
        payload={
            "provenance": view.provenance,
            "source_session_id": view.source_session_id,
            "rule_version": view.rule_version,
            "fact_version": view.fact_version,
            "polarity": view.polarity,
        },
        status="revoked",
        reason_code="parent_withdraw",
        handled_at=datetime(2026, 11, 4, 3, tzinfo=UTC),
        confidence=view.confidence,
    )


def _default_list(views: list[object]) -> list[object]:
    return [view for view in views if view.status != "revoked"]


def test_closed_loop_write_display_recall_correct_withdraw() -> None:
    pending = _birth_views("child-a05", "sess-a05-write")
    assert len(pending) == 3
    for view in pending:
        assert view.display_lifecycle == "auto_pending"
        assert view.display_label == "自动提取·待确认"
        assert "已确认" not in view.display_label or view.display_label.startswith("自动")
        assert view.recall_eligible is False
        assert recall_eligible(view.display_lifecycle) is False

    assert grounded_items_from_archive_views(pending) == ()

    temporal = extract_preference_decisions(
        subject_key="child-a05",
        turns=[SessionTurn(role="user", text=_TEMPORAL_WITHDRAW)],
        source_session_id="sess-a05-write",
    )
    assert not temporal

    confirmed = [_confirm(view) for view in pending]
    for view in confirmed:
        assert view.display_lifecycle == "parent_confirmed"
        assert view.display_label == "家长已确认"
        assert view.recall_eligible is True

    items_after_confirm = grounded_items_from_archive_views(confirmed)
    assert [item.item_id for item in items_after_confirm] == [
        view.record_id for view in confirmed
    ]
    assert all(
        item.source_refs[0].source_event_ids == (f"{FACT_VERSION_PREFIX}1",)
        for item in items_after_confirm
    )

    target = confirmed[0]
    revoked, replacement = _history_correct(
        target,
        new_text="喜欢水彩",
        new_record_id="charch:corrected:水彩",
    )
    assert revoked.recall_eligible is False
    assert replacement.fact_version == target.fact_version + 1
    assert replacement.supersedes_record_id == target.record_id
    assert replacement.recall_eligible is True

    after_correct = [replacement, confirmed[1], confirmed[2], revoked]
    parent_list = _default_list(after_correct)
    assert {view.record_id for view in parent_list} == {
        replacement.record_id,
        confirmed[1].record_id,
        confirmed[2].record_id,
    }
    assert target.record_id not in {view.record_id for view in parent_list}

    recall_after_correct = grounded_items_from_archive_views(after_correct)
    assert [item.item_id for item in recall_after_correct] == [
        replacement.record_id,
        confirmed[1].record_id,
        confirmed[2].record_id,
    ]
    assert recall_after_correct[0].content == "喜好：喜欢水彩"
    assert recall_after_correct[0].source_refs[0].source_event_ids == (
        f"{FACT_VERSION_PREFIX}{replacement.fact_version}",
    )
    assert target.text not in {item.content for item in recall_after_correct}

    withdrawn = _withdraw(confirmed[1])
    assert withdrawn.display_lifecycle == "withdrawn"
    assert withdrawn.display_label == "已撤回"
    assert withdrawn.recall_eligible is False

    after_withdraw = [replacement, withdrawn, confirmed[2]]
    assert grounded_items_from_archive_views(after_withdraw) == grounded_items_from_archive_views(
        [replacement, confirmed[2]]
    )
    assert withdrawn.record_id not in {
        item.item_id for item in grounded_items_from_archive_views(after_withdraw)
    }

    listed_after_withdraw = _default_list(after_withdraw)
    grounded_after_withdraw = grounded_items_from_archive_views(listed_after_withdraw)
    assert {item.item_id for item in grounded_after_withdraw} == {
        replacement.record_id,
        confirmed[2].record_id,
    }
    assert withdrawn.record_id not in {item.item_id for item in grounded_after_withdraw}
    assert target.record_id not in {item.item_id for item in grounded_after_withdraw}


@pytest.mark.parametrize("utterance", _REVIEW_UTTERANCES)
def test_review_utterances_birth_pending_not_confirmed(utterance: str) -> None:
    draft = extract_preference_decisions(
        subject_key="child-a05",
        turns=[SessionTurn(role="user", text=utterance)],
        source_session_id="sess-birth",
    )
    assert draft
    for decision in draft:
        assert decision.lifecycle == "eligible_unwritten"
        assert decision.fact.provenance == "auto_extract"
        assert decision.fact.confidence is not None
        assert decision.fact.confidence < 1.0
    durable = extract_preference_decisions(
        subject_key="child-a05",
        turns=[SessionTurn(role="user", text=utterance)],
        source_session_id="sess-birth",
        record_id=f"charch:birth:{utterance}",
    )
    assert durable
    for decision in durable:
        assert decision.lifecycle == "pending_confirm"
        assert decision.fact.provenance == "auto_extract"
