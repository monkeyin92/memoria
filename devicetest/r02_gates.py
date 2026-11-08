#!/usr/bin/env python3
"""Reduce one R02 suite run to the per-word gate tables with explicit denominators.

Gates come from the Notion R02 card and the 内测准入门禁 v1 page: stop words 8 x 10
with per-word 10/10 clean silences and P90(speech end -> stop) <= 0.8 s; backchannel
6 x 10 with zero false stops; content barge-in >= 9/10, stop <= 1.0 s and the new
answer's first syllable <= 2.0 s. An attempt the evidence script marks unscorable
(too late to stop) is reported, never counted as a pass.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from devicetest.build_r02_scenarios import BACKCHANNEL_WORDS, STOP_WORDS
from scripts.voice_soak_evidence import audit_interruptions

TAG_RE = re.compile(r"^(?:t\d{3}-)?(stop|back)(\d{2})-([a-z0-9-]+)-(\d{2})$")
CONTENT_RE = re.compile(r"^(?:t\d{3}-)?content-(\d{2})$")
UNSCORABLE_REASONS = frozenset({"interrupt_too_late_to_stop", "interrupt_started_after_playback"})
STOP_P90_LIMIT_S = 0.8
CONTENT_STOP_LIMIT_S = 1.0
CONTENT_ANSWER_LIMIT_S = 2.0
TRIALS_PER_WORD = 10


def nearest_rank_p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(0.9 * len(ordered)))
    return round(ordered[rank - 1], 2)


def _rows(run: Path) -> list[dict[str, Any]]:
    path = run / "timeline.jsonl"
    if not path.is_file():
        raise SystemExit(f"missing {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _audited_rows(run: Path) -> list[dict[str, Any]]:
    rows = _rows(run)
    bridge_path = run / "bridge.log"
    serial_path = run / "serial.log"
    bridge = bridge_path.read_text(encoding="utf-8", errors="replace") if bridge_path.is_file() else ""
    serial = serial_path.read_text(encoding="utf-8", errors="replace") if serial_path.is_file() else ""
    assessed = audit_interruptions(rows, bridge, serial)
    return [{**row, **assessed[row["tag"]]} if row.get("tag") in assessed else row for row in rows]


def _word_groups(rows: list[dict[str, Any]], prefix: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("kind") != "reply":
            continue
        match = TAG_RE.match(str(row.get("tag", "")))
        if match and match.group(1) == prefix:
            groups[f"{match.group(2)}-{match.group(3)}"].append(row)
    return groups


def _expected(prefix: str, words: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    return [(f"{index:02d}-{slug}", text) for index, (slug, text) in enumerate(words, 1)]


def reduce_stop(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups = _word_groups(rows, "stop")
    table = []
    for key, word in _expected("stop", STOP_WORDS):
        trials = groups.get(key, [])
        scorable = [r for r in trials if r.get("stop_reason") not in UNSCORABLE_REASONS]
        too_late = len(trials) - len(scorable)
        stopped = [r for r in scorable if r.get("stop_verdict") == "stopped"]
        unverified = [r for r in scorable if r.get("stop_verdict") == "unverified"]
        silent = [r for r in scorable if r.get("silence_verdict") == "silent"]
        reply_resumed = [r for r in scorable if r.get("silence_verdict") == "reply_resumed"]
        silence_unverified = [
            r for r in scorable if r.get("silence_verdict", "unverified") == "unverified"
        ]
        delays = [r["stop_delay_from_speech_end_s"] for r in stopped if isinstance(r.get("stop_delay_from_speech_end_s"), int | float)]
        p90 = nearest_rank_p90([float(value) for value in delays])
        passed = (
            len(scorable) == TRIALS_PER_WORD
            and len(stopped) == TRIALS_PER_WORD
            and not unverified
            and len(silent) == TRIALS_PER_WORD
            and p90 is not None
            and p90 <= STOP_P90_LIMIT_S
        )
        table.append(
            {
                "word": word,
                "key": key,
                "trials": len(trials),
                "scorable": len(scorable),
                "too_late": too_late,
                "stopped": len(stopped),
                "unverified": len(unverified),
                "silent": len(silent),
                "reply_resumed": len(reply_resumed),
                "silence_unverified": len(silence_unverified),
                "p90_speech_end_s": p90,
                "pass": passed,
            }
        )
    return {"gate": "stop", "limit_s": STOP_P90_LIMIT_S, "rows": table, "pass": all(r["pass"] for r in table)}


def reduce_backchannel(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups = _word_groups(rows, "back")
    table = []
    for key, word in _expected("back", BACKCHANNEL_WORDS):
        trials = groups.get(key, [])
        false_stops = [r for r in trials if r.get("stop_verdict") == "stopped"]
        still_playing = [r for r in trials if r.get("still_speaking_after_interrupt") is True]
        unverified = [r for r in trials if r.get("stop_verdict") == "unverified"]
        passed = (
            len(trials) == TRIALS_PER_WORD
            and not false_stops
            and len(still_playing) == TRIALS_PER_WORD
            and not unverified
        )
        table.append(
            {
                "word": word,
                "key": key,
                "trials": len(trials),
                "false_stops": len(false_stops),
                "still_playing": len(still_playing),
                "unverified": len(unverified),
                "pass": passed,
            }
        )
    return {"gate": "backchannel", "rows": table, "pass": all(r["pass"] for r in table)}


def reduce_content(rows: list[dict[str, Any]]) -> dict[str, Any]:
    trials = [r for r in rows if r.get("kind") == "reply" and CONTENT_RE.match(str(r.get("tag", "")))]
    scorable = [r for r in trials if r.get("stop_reason") not in UNSCORABLE_REASONS]
    too_late = len(trials) - len(scorable)
    stopped = [r for r in scorable if r.get("stop_verdict") == "stopped"]
    stop_delays = [float(r["stop_delay_from_speech_end_s"]) for r in stopped if isinstance(r.get("stop_delay_from_speech_end_s"), int | float)]
    answer_delays = [float(r["next_first_syllable_s"]) for r in stopped if isinstance(r.get("next_first_syllable_s"), int | float)]
    stop_p90 = nearest_rank_p90(stop_delays)
    answer_p90 = nearest_rank_p90(answer_delays)
    passed = (
        len(scorable) == TRIALS_PER_WORD
        and len(stopped) >= 9
        and stop_p90 is not None
        and stop_p90 <= CONTENT_STOP_LIMIT_S
        and answer_p90 is not None
        and answer_p90 <= CONTENT_ANSWER_LIMIT_S
    )
    return {
        "gate": "content",
        "trials": len(trials),
        "scorable": len(scorable),
        "too_late": too_late,
        "stopped": len(stopped),
        "stop_p90_speech_end_s": stop_p90,
        "answer_p90_s": answer_p90,
        "limits": {"stop_p90_s": CONTENT_STOP_LIMIT_S, "answer_p90_s": CONTENT_ANSWER_LIMIT_S, "min_stopped": 9},
        "pass": passed,
    }


CRISIS_TAG_RE = re.compile(r"^(?:t\d{3}-)?crisis-(\d{2})$")
CRISIS_TRIALS = 5


def reduce_crisis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    trials = [
        r
        for r in rows
        if r.get("kind") == "reply" and CRISIS_TAG_RE.match(str(r.get("tag", ""))) is not None
    ]
    stopped = [r for r in trials if r.get("stop_verdict") == "stopped"]
    keeping = [r for r in trials if r.get("still_speaking_after_interrupt") is True and r.get("stop_verdict") != "stopped"]
    quiet = [r for r in trials if r.get("quiet_after_reply") is True]
    unverified = [r for r in trials if r.get("stop_verdict") == "unverified"]
    passed = len(trials) == CRISIS_TRIALS and not stopped and len(keeping) == CRISIS_TRIALS and len(quiet) == CRISIS_TRIALS
    return {
        "gate": "crisis",
        "trials": len(trials),
        "kept_playing": len(keeping),
        "stopped": len(stopped),
        "quiet_after_reply": len(quiet),
        "unverified": len(unverified),
        "pass": passed,
    }


REDUCERS = {"a": reduce_stop, "b": reduce_backchannel, "c": reduce_content, "d": reduce_crisis}
SUITE_NAMES = {"a": "stop", "b": "backchannel", "c": "content", "d": "crisis"}


def render_markdown(suite: str, summary: dict[str, Any], run: Path) -> str:
    lines = [f"# R02 suite {suite} ({SUITE_NAMES[suite]}) — run {run}", ""]
    if suite == "a":
        lines += [
            "| word | trials | scorable | too_late | stopped | silent | reply_resumed | silence_unverified | P90 speech end | pass |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in summary["rows"]:
            p90 = "n/a" if row["p90_speech_end_s"] is None else f"{row['p90_speech_end_s']:.2f} s"
            lines.append(
                f"| {row['word']} | {row['trials']} | {row['scorable']} | {row['too_late']} | "
                f"{row['stopped']} | {row['silent']} | {row['reply_resumed']} | "
                f"{row['silence_unverified']} | {p90} | {'PASS' if row['pass'] else 'FAIL'} |"
            )
        lines += [
            "",
            f"Gate: each word 10/10 scorable stops, 10/10 clean silences, and "
            f"P90(speech end -> stop) <= {STOP_P90_LIMIT_S:.1f} s.",
        ]
    elif suite == "b":
        lines += [
            "| word | trials | false stops | still playing | unverified | pass |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in summary["rows"]:
            lines.append(
                f"| {row['word']} | {row['trials']} | {row['false_stops']} | {row['still_playing']} | "
                f"{row['unverified']} | {'PASS' if row['pass'] else 'FAIL'} |"
            )
        lines += ["", "Gate: each word 10/10 keeps the reply playing, zero false stops, zero unverified."]
    elif suite == "c":
        lines += [
            f"- trials: {summary['trials']} (scorable {summary['scorable']}, too_late {summary['too_late']})",
            f"- stopped: {summary['stopped']}/10 (need >= 9)",
            f"- stop P90: {summary['stop_p90_speech_end_s']} s (limit {CONTENT_STOP_LIMIT_S:.1f} s)",
            f"- new-question first syllable P90: {summary['answer_p90_s']} s (limit {CONTENT_ANSWER_LIMIT_S:.1f} s)",
        ]
    else:
        lines += [
            f"- trials: {summary['trials']}/5",
            f"- kept playing after the stop attempt: {summary['kept_playing']}/5",
            f"- stopped by the soft stop: {summary['stopped']} (must be 0)",
            f"- quiet after the reply: {summary['quiet_after_reply']}/5",
            f"- unverified: {summary['unverified']}",
        ]
    lines += ["", f"**Suite {suite}: {'PASS' if summary['pass'] else 'FAIL'}**", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--suite", choices=("a", "b", "c", "d", "all"), default="all")
    ap.add_argument("--out", type=Path, default=None, help="markdown output path")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()
    rows = _audited_rows(args.run)
    suites = ("a", "b", "c", "d") if args.suite == "all" else (args.suite,)
    summaries = [REDUCERS[suite](rows) for suite in suites]
    markdown = "\n".join(render_markdown(suite, summary, args.run) for suite, summary in zip(suites, summaries, strict=True))
    print(markdown)
    if args.out is not None:
        args.out.write_text(markdown, encoding="utf-8")
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(summaries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(summary["pass"] for summary in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
