from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.cost_ledger.parse import LedgerParseError, parse_ledger_sample
from scripts.cost_ledger.reconcile import reconcile_sample

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "household_cost_ledger.py"
FIXTURES = ROOT / "tests" / "fixtures" / "cost_ledger"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_matched_sample_reconciles_without_invented_prices() -> None:
    sample = parse_ledger_sample(_load("sample-matched.json"))
    report = reconcile_sample(sample)

    assert report.invented_prices is False
    assert report.intensity_distribution == {"low": 1, "mid": 1, "high": 1}
    assert report.gaps == ()

    by_id = {row.household_id: row for row in report.households}
    low = by_id["hh_low0001"]
    assert low.contribution_verification == "bill_matched"
    assert low.contribution_minor is not None
    assert low.variable_ops_amount_minor == 32000

    mid = by_id["hh_mid0001"]
    assert mid.flagged_quantity["cancel_still_billed"] == 3000
    assert mid.flagged_quantity["free_tier"] == 1048576
    assert mid.flagged_quantity["retry"] == 300

    high = by_id["hh_high001"]
    assert high.flagged_quantity["generated_unplayed"] == 9000
    assert high.flagged_quantity["speculation"] == 20000

    assert any(line.category == "rd_fixed" for line in report.unallocated_ops)


def test_missing_bills_emit_weiyan_gaps_without_fake_totals() -> None:
    sample = parse_ledger_sample(_load("sample-missing-bills.json"))
    report = reconcile_sample(sample)

    assert report.invented_prices is False
    assert report.intensity_distribution["low"] == 1
    assert report.intensity_distribution["mid"] == 1
    assert report.intensity_distribution["high"] == 1

    gap_codes = {gap.code for gap in report.gaps}
    assert "vendor_bill_missing" in gap_codes
    assert "ops_未验" in gap_codes
    assert "receipt_未验" in gap_codes

    for row in report.households:
        assert row.contribution_minor is None
        assert row.contribution_verification == "未验"
        for cloud in row.cloud_by_category:
            assert cloud.verification == "未验"
            assert cloud.bill_amount_minor is None


def test_parse_rejects_invented_amount_on_weiyan_bill() -> None:
    payload = _load("sample-missing-bills.json")
    payload["vendor_bills"] = [
        {
            "bill_id": "fake",
            "vendor": "volcengine",
            "category": "asr",
            "unit": "seconds",
            "quantity": 1,
            "amount_minor": 99,
            "currency": "CNY",
            "verification": "未验",
        }
    ]
    try:
        parse_ledger_sample(payload)
    except LedgerParseError as exc:
        assert "invented amount_minor" in str(exc)
    else:
        raise AssertionError("expected LedgerParseError")


def test_cli_prints_report_for_matched_fixture() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURES / "sample-matched.json")],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr
    assert "household_cost_ledger=OK invented_prices=false gaps=0" in completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["invented_prices"] is False
    assert payload["intensity_distribution"] == {"low": 1, "mid": 1, "high": 1}


def test_cli_keeps_missing_bill_sample_ok_with_gaps() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURES / "sample-missing-bills.json")],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr
    assert "invented_prices=false" in completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["gaps"]
    assert all(row["contribution_minor"] is None for row in payload["households"])
