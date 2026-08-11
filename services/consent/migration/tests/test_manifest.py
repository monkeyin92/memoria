"""Rollback manifest tests: jsonl output, executed:false, reproducible."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from services.consent.migration.adapters import RawLegacyConsent
from services.consent.migration.manifest import MANIFEST_NOTE, render_manifest
from services.consent.migration.plan import build_plan

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def legacy_row(
    *,
    source: str = "guardian",
    legacy_id: str,
    capability_clue: str = "memory_retention",
    purpose_clue: str = "memory_retention",
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
        policy_version="policy-v1",
        binding_clue="link_1" if source == "guardian" else None,
        binding_status="active" if source == "guardian" else None,
        raw={},
    )


def test_manifest_emits_one_jsonl_line_per_plan_item() -> None:
    plan = build_plan(
        [
            legacy_row(legacy_id="consent_a"),
            legacy_row(legacy_id="consent_b", capability_clue="corpus_recording"),
        ],
        now=NOW,
    )
    rendered = render_manifest(plan)
    lines = rendered.splitlines()
    assert len(lines) == 2
    assert all(json.loads(line) for line in lines)


def test_manifest_lines_carry_full_rollback_record() -> None:
    plan = build_plan(
        [
            legacy_row(legacy_id="consent_a"),
            legacy_row(legacy_id="consent_b", capability_clue="corpus_recording"),
        ],
        now=NOW,
    )
    by_old_id = {
        json.loads(line)["old_id"]: json.loads(line) for line in render_manifest(plan).splitlines()
    }
    assert set(by_old_id) == {"consent_a", "consent_b"}
    for entry in by_old_id.values():
        assert set(entry) == {"old_id", "new_id", "action", "checksum", "executed", "note"}
        assert entry["executed"] is False
        assert entry["note"] == MANIFEST_NOTE
        assert entry["action"] in {"offer_candidate", "quarantine"}
        assert entry["checksum"]
    assert by_old_id["consent_a"]["action"] == "offer_candidate"
    assert by_old_id["consent_b"]["action"] == "quarantine"


def test_manifest_checksum_matches_plan_row_checksum() -> None:
    plan = build_plan([legacy_row(legacy_id="consent_a")], now=NOW)
    entry = json.loads(render_manifest(plan))
    assert entry["checksum"] == plan.items[0].row_checksum
    assert entry["new_id"] == plan.items[0].new_id


def test_manifest_is_reproducible() -> None:
    plan = build_plan(
        [
            legacy_row(legacy_id="consent_a"),
            legacy_row(legacy_id="consent_b", capability_clue="corpus_recording"),
        ],
        now=NOW,
    )
    assert render_manifest(plan) == render_manifest(plan)


def test_manifest_lines_are_ordered_by_old_id() -> None:
    plan = build_plan(
        [
            legacy_row(legacy_id="consent_z"),
            legacy_row(legacy_id="consent_a"),
        ],
        now=NOW,
    )
    lines = render_manifest(plan).splitlines()
    assert [json.loads(line)["old_id"] for line in lines] == ["consent_a", "consent_z"]


def test_manifest_never_marks_anything_executed() -> None:
    plan = build_plan(
        [
            legacy_row(legacy_id="consent_a"),
            legacy_row(legacy_id="consent_b", capability_clue="corpus_recording"),
            legacy_row(legacy_id="consent_c", capability_clue="weekly_report"),
        ],
        now=NOW,
    )
    for line in render_manifest(plan).splitlines():
        assert json.loads(line)["executed"] is False


def test_empty_plan_produces_empty_manifest() -> None:
    plan = build_plan([], now=NOW)
    assert render_manifest(plan) == ""


def test_manifest_contains_migration_not_executed_note() -> None:
    plan = build_plan([legacy_row(legacy_id="consent_a")], now=NOW)
    assert MANIFEST_NOTE == "迁移未执行"
    assert json.loads(render_manifest(plan))["note"] == "迁移未执行"
