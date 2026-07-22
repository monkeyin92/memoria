from datetime import UTC, datetime

from services.archive.domain import EvidenceEvent
from services.common.evidence_policy import (
    classify_prompt_kind,
    confirmed_projection_contribution_for,
    contribution_for,
)


def _event(
    *,
    speaker_class: str = "owner",
    event_type: str = "speech.utterance_finalized",
    payload: dict[str, object] | None = None,
) -> EvidenceEvent:
    return EvidenceEvent(
        event_id="evidence-policy-test",
        account_id="account",
        event_type=event_type,
        occurred_at=datetime(2026, 7, 22, tzinfo=UTC),
        speaker_class=speaker_class,  # type: ignore[arg-type]
        source="test",
        payload=payload or {"owner_projection_eligible": True, "interaction_mode": "companion"},
    )


def test_only_canonical_owner_sources_contribute_and_prompt_weight_is_server_policy() -> None:
    assert contribution_for(_event()).weight == "strong"
    assert contribution_for(_event(payload={"owner_projection_eligible": True, "interaction_mode": "companion", "prompt_kind": "leading"})).weight == "weak"
    assert contribution_for(_event(payload={"owner_projection_eligible": True, "interaction_mode": "companion", "prompt_kind": "structured"})).weight == "weak"
    assert contribution_for(_event(speaker_class="assistant")).accepted is False
    assert contribution_for(_event(speaker_class="guest")).accepted is False
    assert contribution_for(_event(speaker_class="uncertain")).accepted is False
    assert contribution_for(_event(payload={"interaction_mode": "companion"})).accepted is False
    assert contribution_for(_event(payload={"owner_projection_eligible": False, "interaction_mode": "companion"})).accepted is False
    assert contribution_for(_event(payload={"owner_projection_eligible": True, "interaction_mode": "self_preview", "simulated_output": True})).accepted is False


def test_owner_actions_allow_only_positive_whitelist_and_never_negative_feedback() -> None:
    positive = _event(
        event_type="owner.action_recorded",
        payload={"action_type": "decision_review", "owner_projection_eligible": True},
    )
    negative = _event(
        event_type="owner.action_recorded",
        payload={"action_type": "not_me", "owner_projection_eligible": True},
    )
    assert contribution_for(positive).accepted is True
    assert contribution_for(negative).accepted is False
    assert contribution_for(negative).reason == "negative_action"


def test_prompt_kind_classification_is_conservative() -> None:
    assert classify_prompt_kind("你是不是更喜欢安静？") == "leading"
    assert classify_prompt_kind("你选工作还是家庭？") == "structured"
    assert classify_prompt_kind("请从工作和家庭里选一个。") == "structured"
    assert classify_prompt_kind("你更喜欢安静，对吗？") == "leading"
    assert classify_prompt_kind("那件事对你有什么影响？") == "open"
    assert classify_prompt_kind("请说说那段经历。") == "open"
    assert classify_prompt_kind("我明白了。") == "spontaneous"


def test_only_confirmed_projection_reads_grandfather_legacy_owner_speech() -> None:
    legacy = _event(payload={"text": "这是旧版已确认的主人原话。"})
    assert contribution_for(legacy).accepted is False
    migrated = confirmed_projection_contribution_for(legacy)
    assert migrated.accepted is True
    assert migrated.reason == "legacy_confirmed_projection"
    assert migrated.weight == "normal"
    assert confirmed_projection_contribution_for(
        _event(payload={"owner_projection_eligible": False})
    ).accepted is False
    assert confirmed_projection_contribution_for(
        _event(payload={"interaction_mode": "self_preview"})
    ).accepted is False
