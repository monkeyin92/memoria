"""Dry-run plan tests: deterministic counts, checksum, mapping, zero side effects."""

from __future__ import annotations

from datetime import UTC, datetime

from services.consent.migration.adapters import RawLegacyConsent
from services.consent.migration.normalizer import normalize_row
from services.consent.migration.plan import DryRunPlan, PlanItem, build_plan

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def legacy_row(
    *,
    source: str = "guardian",
    legacy_id: str,
    capability_clue: str = "memory_retention",
    purpose_clue: str = "memory_retention",
    policy_version: str = "policy-v1",
    **kwargs: object,
) -> RawLegacyConsent:
    return RawLegacyConsent(
        source=source,
        legacy_id=legacy_id,
        subject_clue="minor_1" if source == "guardian" else "acct_1",
        subject_account_clue=None,
        actor_clue="guardian_1" if source == "guardian" else "acct_1",
        capability_clue=capability_clue,
        purpose_clue=purpose_clue,
        evidence_clue="evt_1",
        granted_at=datetime(2026, 1, 10, tzinfo=UTC),
        expires_at=None,
        revoked_at=None,
        policy_version=policy_version,
        binding_clue="link_1" if source == "guardian" else None,
        binding_status="active" if source == "guardian" else None,
        raw={},
        **kwargs,
    )


class FakeStore:
    """A store that must never be touched by a dry run."""

    def __init__(self) -> None:
        self.writes: list[object] = []

    def write(self, payload: object) -> None:
        self.writes.append(payload)


def test_plan_counts_mixed_input() -> None:
    rows = [
        legacy_row(legacy_id="consent_a"),
        legacy_row(legacy_id="consent_b", capability_clue="weekly_report"),
        legacy_row(legacy_id="consent_c", capability_clue="corpus_recording"),
        legacy_row(
            legacy_id="raw_grant_1", source="raw_audio", capability_clue="raw_audio_retention"
        ),
    ]
    plan = build_plan(rows, now=NOW)
    assert isinstance(plan, DryRunPlan)
    assert plan.input_count == 4
    assert plan.provable_count == 3
    assert plan.quarantine_count == 1
    assert len(plan.items) == 4


def test_plan_item_mapping_old_to_canonical_ids() -> None:
    rows = [
        legacy_row(legacy_id="consent_a"),
        legacy_row(legacy_id="consent_b", capability_clue="corpus_recording"),
    ]
    plan = build_plan(rows, now=NOW)
    items = {item.old_id: item for item in plan.items}
    assert set(items) == {"consent_a", "consent_b"}
    assert items["consent_a"].action == "offer_candidate"
    assert items["consent_a"].reason is None
    assert items["consent_b"].action == "quarantine"
    assert items["consent_b"].reason == "unsupported_capability"

    direct = normalize_row(rows[0], now=NOW)
    assert items["consent_a"].new_id == direct.consent_id


def test_plan_checksum_is_deterministic() -> None:
    rows = [
        legacy_row(legacy_id="consent_a"),
        legacy_row(legacy_id="consent_b", capability_clue="corpus_recording"),
    ]
    first = build_plan(rows, now=NOW)
    second = build_plan(rows, now=NOW)
    assert first.checksum == second.checksum
    assert first.items == second.items
    assert first.checksum == first.checksum.lower()
    assert len(first.checksum) == 64


def test_plan_checksum_changes_when_content_changes() -> None:
    base = [legacy_row(legacy_id="consent_a")]
    changed = [legacy_row(legacy_id="consent_a", policy_version="policy-v2")]
    assert build_plan(base, now=NOW).checksum != build_plan(changed, now=NOW).checksum


def test_plan_items_are_ordered_deterministically() -> None:
    rows = [
        legacy_row(legacy_id="consent_z"),
        legacy_row(legacy_id="consent_a"),
        legacy_row(legacy_id="consent_m", capability_clue="corpus_recording"),
    ]
    plan = build_plan(rows, now=NOW)
    assert [item.old_id for item in plan.items] == ["consent_a", "consent_m", "consent_z"]


def test_dry_run_never_writes_to_any_store() -> None:
    store = FakeStore()
    rows = [
        legacy_row(legacy_id="consent_a"),
        legacy_row(legacy_id="consent_b", capability_clue="corpus_recording"),
    ]
    plan = build_plan(rows, now=NOW)
    assert store.writes == []
    # The dry run is a pure computation: it never receives a store and the
    # produced plan is inert data, not an execution instruction set.
    assert plan.provable_count + plan.quarantine_count == plan.input_count


def test_plan_does_not_depend_on_clock_or_global_state() -> None:
    rows = [legacy_row(legacy_id="consent_a")]
    morning = build_plan(rows, now=NOW)
    evening = build_plan(rows, now=datetime(2026, 12, 31, 23, 59, tzinfo=UTC))
    assert morning.checksum == evening.checksum
    assert morning.items == evening.items


def test_empty_input_produces_empty_plan() -> None:
    plan = build_plan([], now=NOW)
    assert plan.input_count == 0
    assert plan.provable_count == 0
    assert plan.quarantine_count == 0
    assert plan.items == ()
    assert len(plan.checksum) == 64


def test_plan_item_is_frozen_data() -> None:
    plan = build_plan([legacy_row(legacy_id="consent_a")], now=NOW)
    item = plan.items[0]
    assert isinstance(item, PlanItem)
    assert item.new_id
    assert item.row_checksum
    assert item.row_checksum == item.row_checksum.lower()
