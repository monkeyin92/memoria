"""P2-06: the companionship scenario set runs, and its baseline is pinned."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.companionship.evaluation import (
    OfflineControlAdapter,
    load_companionship_dataset,
    run_companionship_evaluation,
)

FIXED = (
    Path(__file__).parents[1] / "evaluation" / "companionship_eval_zh_v1.json"
)


@pytest.mark.asyncio
async def test_fixed_companionship_set_passes_through_real_doors() -> None:
    """Every scenario is driven through the shipped HTTP surface, not a stub.

    What it proves: the authority-issued profile for a confirmed subject keeps
    the category fence (minor ⇒ no session capability, no private recall),
    confirming a subject supersedes the previous signed profile, and the
    guardian retention consent can be granted and revoked.  What it does not
    prove: device admission or model wording.
    """
    dataset = load_companionship_dataset(FIXED)
    assert {case.scenario for case in dataset.cases} == {
        "student_frustration",
        "plan_continuation",
        "subject_switch",
        "consent_revoked",
    }
    saved = dict(os.environ)
    try:
        report = await run_companionship_evaluation(dataset, OfflineControlAdapter())
    finally:
        os.environ.clear()
        os.environ.update(saved)

    failures = {
        item.case_id: item.failures for item in report.observations if not item.passed
    }
    assert failures == {}
    assert report.accuracy == 1.0
    assert report.gate_violations == 0
    # 未成年轮廓上出现私密记忆能力即为越权读取风险；基线必须为 0。
    assert report.unauthorized_recall == 0
    # 同一条件下每个场景都要跑通真实门，延迟用于与后续改动对照。
    assert report.latency_p50_ms > 0
