from __future__ import annotations

import json
from pathlib import Path

from scripts.voice_soak_verdict import judge, write_manifest


def _run_with_slow_replies(run: Path) -> None:
    run.mkdir(exist_ok=True)
    write_manifest(
        run,
        scenario_hash="scenario",
        expected_step_tags=["t001-question"],
        required_logs=[],
    )
    rows = [
        {"kind": "start", "ts": 0.0},
        {"kind": "said", "tag": "t001-question", "ts": 1.0},
        {"kind": "reply", "tag": "t001-question", "latency_s": 2.66, "ts": 3.66},
        {"kind": "scenario_done", "ts": 4.0},
        {"kind": "post_scenario_observation", "verdict": "verified_idle", "ts": 5.0},
    ]
    (run / "timeline.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_reply_latency_record_only_keeps_numbers_and_marks_them_informational(
    tmp_path: Path,
) -> None:
    _run_with_slow_replies(tmp_path)

    verdict = judge(
        tmp_path,
        max_no_reply=0,
        max_reply_latency_p90_s=2.0,
        max_reply_latency_max_s=2.0,
        reply_latency_record_only=True,
    )

    assert verdict.passed is True
    assert verdict.failures == ()
    assert verdict.reply_latency_p90_s == 2.66
    assert verdict.reply_latency_max_s == 2.66
    assert verdict.informational == (
        "reply_latency_p90_s: 2.66 > 2.0 (record-only)",
        "reply_latency_max_s: 2.66 > 2.0 (record-only)",
    )
    payload = verdict.as_dict()
    assert payload["reply_latency_p90_s"] == 2.66
    assert payload["informational"] == list(verdict.informational)


def test_reply_latency_gate_still_fails_by_default(tmp_path: Path) -> None:
    _run_with_slow_replies(tmp_path)

    verdict = judge(
        tmp_path,
        max_no_reply=0,
        max_reply_latency_p90_s=2.0,
        max_reply_latency_max_s=2.0,
    )

    assert verdict.passed is False
    assert verdict.informational == ()
    assert "reply_latency_p90_s: 2.66 > 2.0" in verdict.failures
