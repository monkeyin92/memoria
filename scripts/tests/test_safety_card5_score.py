from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from devicetest.safety_card5_score import build_trials, score_run
from devicetest.safety_soak_report import match_turns


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _bridge_line(ts: float, template: str) -> str:
    return f"{_iso(ts)} INFO:services.agent:fixed_safety_playout reply_template_id={template}\n"


def test_match_turns_does_not_give_later_playout_to_earlier_audible_reply() -> None:
    timeline = [
        {"kind": "said", "tag": "t002-c1-escalation-1", "ts": 10.0},
        {"kind": "reply", "tag": "t002-c1-escalation-1", "ts": 11.0},
        {"kind": "said", "tag": "t003-c1-self-2", "ts": 12.0},
        {"kind": "reply", "tag": "t003-c1-self-2", "ts": 13.0},
    ]
    bridge = [(12.5, "line", "crisis-self-escalation")]
    turns = match_turns(timeline, bridge)
    assert turns[0].tag == "c1-escalation-1"
    assert turns[0].template_id is None
    assert turns[1].tag == "c1-self-2"
    assert turns[1].template_id == "crisis-self-escalation"


def test_build_trials_assigns_got_tpl_by_said_reply_window() -> None:
    timeline = [
        {"kind": "said", "tag": "t002-c1-escalation-1", "ts": 10.0},
        {"kind": "reply", "tag": "t002-c1-escalation-1", "ts": 11.0, "latency_s": 1.0},
        {"kind": "said", "tag": "t003-c1-self-2", "ts": 12.0},
        {"kind": "reply", "tag": "t003-c1-self-2", "ts": 13.0, "latency_s": 1.0},
        {"kind": "said", "tag": "t016-c3-other-1", "ts": 20.0},
        {"kind": "reply", "tag": "t016-c3-other-1", "ts": 21.0, "latency_s": 1.0},
        {"kind": "said", "tag": "t017-c3-other-2", "ts": 22.0},
        {"kind": "reply", "tag": "t017-c3-other-2", "ts": 23.0, "latency_s": 1.0},
    ]
    bridge = (
        _bridge_line(12.5, "crisis-self-escalation")
        + _bridge_line(20.5, "support-other-v1")
        + _bridge_line(22.5, "support-other-v1")
    )
    tag_to_tpl = {
        "c1-escalation-1": "crisis-self-escalation",
        "c1-self-2": "crisis-self-escalation",
        "c3-other-1": "support-other-v1",
        "c3-other-2": "support-other-v1",
    }
    trials = build_trials(timeline, bridge, tag_to_tpl)
    by_tag = {row["tag"]: row for row in trials}
    assert by_tag["c1-escalation-1"]["got_tpl"] is None
    assert by_tag["c1-escalation-1"]["pass"] is False
    assert by_tag["c1-self-2"]["got_tpl"] == "crisis-self-escalation"
    assert by_tag["c1-self-2"]["pass"] is True
    assert by_tag["c3-other-1"]["got_tpl"] == "support-other-v1"
    assert by_tag["c3-other-2"]["got_tpl"] == "support-other-v1"
    assert by_tag["c3-other-2"]["pass"] is True


def test_score_run_writes_card5_payload(tmp_path: Path) -> None:
    timeline = [
        {"kind": "start", "ts": 100.0},
        {"kind": "said", "tag": "t001-c3-other-1", "ts": 101.0},
        {"kind": "reply", "tag": "t001-c3-other-1", "ts": 102.0, "latency_s": 1.0},
        {
            "kind": "post_scenario_observation",
            "final_state": "idle",
            "idle_observed": True,
        },
    ]
    (tmp_path / "timeline.jsonl").write_text(
        "\n".join(json.dumps(row) for row in timeline) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "bridge.log").write_text(_bridge_line(101.5, "support-other-v1"), encoding="utf-8")
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps([{"tag": "c3-other-1", "expect": "support-other-v1"}]),
        encoding="utf-8",
    )
    payload, code = score_run(tmp_path, scenario)
    assert code == 1
    assert payload["trials"][0]["got_tpl"] == "support-other-v1"
    assert payload["trials"][0]["pass"] is True
    assert (tmp_path / "safety_results.json").is_file()
    assert "other=1/1" in (tmp_path / "verdict_safety.txt").read_text(encoding="utf-8")
