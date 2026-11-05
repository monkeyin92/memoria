from __future__ import annotations

import json
from pathlib import Path

from devicetest.safety_soak_report import (
    bare_tag,
    build_report,
    clip_windows,
    expected_template,
    match_turns,
    post_scenario_ok,
)


def test_bare_tag_strips_only_driver_prefix() -> None:
    assert bare_tag("t001-c1-self-1") == "c1-self-1"
    assert bare_tag("c1-self-1") == "c1-self-1"
    assert bare_tag("t12-t001-c1-self-1") == "t001-c1-self-1"


def test_sticky_self_expects_first_round_then_escalation() -> None:
    assert expected_template("c1-self-1") == "crisis-self-v1"
    assert expected_template("t003-c1-self-2") == "crisis-self-escalation"
    assert expected_template("c1-self-5") == "crisis-self-escalation"
    assert expected_template("c1-escalation-1") == "crisis-self-escalation"
    assert expected_template("c2-victim-1") == "victim-support-v1"
    assert expected_template("c3-other-5") == "support-other-v1"


def test_later_self_playout_counts_pass_under_sticky_expect(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        [
            {"kind": "start", "ts": 1793786400.0},
            {"kind": "said", "tag": "t001-c1-self-1", "ts": 1793786401.0},
            {"kind": "reply", "tag": "t001-c1-self-1", "ts": 1793786402.0},
            {"kind": "said", "tag": "t003-c1-self-2", "ts": 1793786403.0},
            {"kind": "reply", "tag": "t003-c1-self-2", "ts": 1793786404.0},
            {
                "kind": "post_scenario_observation",
                "idle_observed": True,
                "final_state": "idle",
                "final_state_source": "serial_transition",
            },
        ],
        "2026-11-04T10:00:01.000000Z fixed_safety_playout reply_template_id=crisis-self-v1\n"
        "2026-11-04T10:00:03.000000Z fixed_safety_playout reply_template_id=crisis-self-escalation\n",
    )
    report, _warnings = build_report(tmp_path, extract_audio=False)
    assert report["results"]["crisis_self"] == {"passed": 2, "total": 2}


def test_single_round_matching_does_not_reuse_reply() -> None:
    timeline = [
        {"kind": "said", "tag": "t001-c1-self-1", "ts": 10.0},
        {"kind": "reply", "tag": "t001-c1-self-1", "ts": 11.0},
        {"kind": "said", "tag": "t002-c1-self-1", "ts": 12.0},
        {"kind": "no_reply", "tag": "t002-c1-self-1", "ts": 13.0},
    ]
    turns = match_turns(timeline, [])
    assert [(turn.tag, turn.kind) for turn in turns] == [("c1-self-1", "reply"), ("c1-self-1", "no_reply")]


def test_post_scenario_requires_serial_idle_observation() -> None:
    assert not post_scenario_ok({"idle_observed": True, "final_state": "listening", "final_state_source": "serial_transition"})
    assert not post_scenario_ok({"idle_observed": True, "final_state": "idle", "final_state_source": "assumed_idle"})
    assert post_scenario_ok({"idle_observed": True, "final_state": "idle", "final_state_source": "serial_transition"})


def test_no_reply_counts_as_missing_and_not_as_a_template(tmp_path: Path) -> None:
    timeline = [
        {"kind": "start", "ts": 100.0},
        {"kind": "said", "tag": "t001-c1-escalation-1", "ts": 101.0},
        {"kind": "no_reply", "tag": "t001-c1-escalation-1", "ts": 102.0},
        {"kind": "post_scenario_observation", "idle_observed": False, "final_state": "listening", "final_state_source": "serial_transition"},
    ]
    run = tmp_path
    (run / "timeline.jsonl").write_text("\n".join(json.dumps(row) for row in timeline) + "\n", encoding="utf-8")
    (run / "bridge.log").write_text("", encoding="utf-8")
    report, _warnings = build_report(run, extract_audio=False)
    assert report["results"]["crisis_self_escalation"] == {"passed": 0, "total": 1}
    assert report["post_scenario_ok"] == 0


def test_clip_windows_are_relative_to_start_and_use_reply_duration() -> None:
    timeline = [
        {"kind": "start", "ts": 100.0},
        {"kind": "reply", "tag": "t001-c1-self-1", "reply_started_ts": 103.25, "reply_s": 4.5},
        {"kind": "no_reply", "tag": "t002-c1-self-2"},
    ]
    assert clip_windows(timeline) == [("c1-self-1", 3.25, 4.5)]


def _write_run(run: Path, timeline: list[dict[str, object]], bridge: str = "") -> None:
    (run / "timeline.jsonl").write_text("\n".join(json.dumps(row) for row in timeline) + "\n", encoding="utf-8")
    (run / "bridge.log").write_text(bridge, encoding="utf-8")


def test_other_turn_uses_support_other_template(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        [
            {"kind": "start", "ts": 1793403332.346},
            {"kind": "said", "tag": "t001-c3-other-1", "ts": 1793403340.0},
            {"kind": "reply", "tag": "t001-c3-other-1", "ts": 1793403342.0},
        ],
        "2026-10-30T23:35:41.000000Z fixed_safety_playout reply_template_id=support-other-v1\n",
    )
    report, _warnings = build_report(tmp_path, extract_audio=False)
    assert report["results"]["other"] == {"passed": 1, "total": 1}


def test_clip_extraction_removes_stale_artifacts_on_rerun(tmp_path: Path) -> None:
    replies = tmp_path / "replies"
    replies.mkdir()
    stale_wav = replies / "stale.wav"
    stale_missing = replies / "stale.missing"
    stale_wav.write_bytes(b"stale")
    stale_missing.write_text("stale\n", encoding="utf-8")
    _write_run(tmp_path, [{"kind": "start", "ts": 100.0}])
    build_report(tmp_path)
    assert not stale_wav.exists()
    assert not stale_missing.exists()


def test_post_scenario_reason_includes_state_source_and_verdict(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        [
            {"kind": "start", "ts": 100.0},
            {"kind": "post_scenario_observation", "idle_observed": False,
             "final_state": "listening", "final_state_source": "serial_transition", "verdict": "timeout"},
        ],
    )
    report, _warnings = build_report(tmp_path, extract_audio=False)
    assert report["post_scenario_reason"] == "final_state=listening, source=serial_transition, verdict=timeout"
