"""P2-06: the companionship scenario set runs, and its baseline is pinned."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services.companionship.evaluation import (
    OfflineControlAdapter,
    load_companionship_dataset,
    run_companionship_evaluation,
)

FIXED = Path(__file__).parents[1] / "evaluation" / "companionship_eval_zh_v1.json"


@pytest.mark.asyncio
async def test_fixed_companionship_set_passes_through_real_doors() -> None:
    """Every scenario is driven through the shipped HTTP surface, not a stub.

    What it proves: the authority-issued profile for a confirmed subject keeps
    the category fence, a second session stays on that subject, confirming a
    subject supersedes the previous signed profile, guardian review opens only
    with retention consent, and revoking that consent closes the review doors.
    What it does not prove: device admission or model wording.
    """
    dataset = load_companionship_dataset(FIXED)
    assert {case.scenario for case in dataset.cases} == {
        "student_frustration",
        "plan_continuation",
        "review_scope",
        "subject_switch",
        "consent_revoked",
    }
    saved = dict(os.environ)
    try:
        report = await run_companionship_evaluation(dataset, OfflineControlAdapter())
    finally:
        os.environ.clear()
        os.environ.update(saved)

    failures = {item.case_id: item.failures for item in report.observations if not item.passed}
    assert failures == {}
    assert report.accuracy == 1.0
    assert report.gate_violations == 0
    # 未成年轮廓上出现私密记忆能力即为越权读取风险；基线必须为 0。
    assert report.unauthorized_recall == 0
    # 同一条件下每个场景都要跑通真实门，延迟用于与后续改动对照。
    assert report.latency_p50_ms > 0


def test_load_dataset_reads_declared_probe_sequences(tmp_path: Path) -> None:
    """Each case names the doors it runs. A sequence outside the table is rejected."""

    dataset = load_companionship_dataset(FIXED)
    assert dataset.version == "companionship-eval-zh-v1.1"
    probes = {case.case_id: case.probes for case in dataset.cases}
    assert probes["under14-frustration"] == ("profile_fence", "subject_continuity")
    assert probes["14-17-frustration"] == ("profile_fence", "subject_continuity")
    assert probes["adult-plan-continuation"] == ("profile_fence", "subject_continuity")
    assert probes["under14-subject-switch-fence"] == ("profile_fence", "superseded_profile")
    assert probes["14-17-consent-revoked"] == ("profile_fence", "consent_revoke_fail_closed")
    assert probes["under14-review-allowed"] == ("profile_fence", "review_allow")
    assert probes["14-17-review-denied"] == ("profile_fence", "review_deny")

    raw = json.loads(FIXED.read_text(encoding="utf-8"))
    raw["cases"] = [
        {
            "case_id": "review-without-a-door",
            "scenario": "review_scope",
            "category": "under_14",
            "expect_service_mode": "student_minor",
            "expect_subject_category": "minor",
            "probes": ["profile_fence"],
        }
    ]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="probe sequence"):
        load_companionship_dataset(broken)
