#!/usr/bin/env python3
"""Long-session degradation + dual-origin interrupt stop report for one soak run.

Offline only. Does not change card-5 gates in voice_soak_verdict.py.

    python scripts/voice_soak_session_report.py RUN_DIR

Exit 0 when status is pass; exit 1 for fail or incomplete.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Literal

if __package__:
    from .voice_soak_analyze import pct
    from .voice_soak_evidence import audit_interruptions
else:
    from voice_soak_analyze import pct
    from voice_soak_evidence import audit_interruptions

MIN_DURATION_S = 15 * 60
HALF_P50_DELTA_MAX_S = 0.3
REPEAT_RATIO = 0.8
MIN_TURNS_FOR_HALF = 4

ReportStatus = Literal["pass", "fail", "incomplete"]
ADVISORY_THRESHOLD_STATUS = "待产品确认"


def _outcome(status: ReportStatus, failures: list[str]) -> dict[str, Any]:
    if status == "pass":
        if failures:
            raise ValueError("pass cannot carry failures")
    elif not failures:
        raise ValueError("non-pass requires a reason")
    elif status == "incomplete" and any(
        not str(item).startswith("incomplete:") for item in failures
    ):
        raise ValueError("incomplete reasons must start with incomplete:")
    return {"passed": status == "pass", "status": status, "failures": list(failures)}


def _sha256(path: Path) -> str | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"timeline.jsonl:{line_no}: row must be an object")
        rows.append(row)
    return rows


def _meta_fields(run: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    meta = run / "meta.txt"
    if meta.is_file():
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip()
    manifest = run / "manifest.json"
    if manifest.is_file():
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            out["manifest"] = {
                k: raw[k]
                for k in ("scenario_hash", "expected_step_tags", "required_logs", "min_duration_s")
                if k in raw
            }
    return out


def turn_anchor_ts(said: dict[str, Any] | None, outcome: dict[str, Any]) -> float | None:
    if said is not None and isinstance(said.get("ts"), (int, float)):
        return float(said["ts"])
    if isinstance(outcome.get("ts"), (int, float)):
        return float(outcome["ts"])
    if isinstance(outcome.get("reply_started_ts"), (int, float)):
        return float(outcome["reply_started_ts"])
    return None


def slope(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    n = len(xs)
    if n < 2:
        return None, None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx:
        return None, None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    if n < 3:
        return b, None
    resid = sum((y - my - b * (x - mx)) ** 2 for x, y in zip(xs, ys, strict=True))
    se = (resid / (n - 2) / sxx) ** 0.5
    if se == 0:
        return b, None
    return b, b / se


def _latency(turn: dict[str, Any]) -> float | None:
    if turn.get("kind") != "reply":
        return None
    value = turn.get("latency_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def half_stats(turns: list[dict[str, Any]]) -> dict[str, Any]:
    replies = [t for t in turns if t["kind"] == "reply"]
    lat = [value for t in replies if (value := _latency(t)) is not None]
    texts_missing = any(
        not isinstance(t.get("reply_text"), str) or not t.get("reply_text") for t in replies
    )
    if texts_missing:
        repeat_rate: float | str | None = "缺"
        repeat_pairs = 0
    else:
        texts = [str(t["reply_text"]) for t in replies]
        pairs = list(zip(texts, texts[1:], strict=False))
        if not pairs:
            repeat_rate = "缺"
            repeat_pairs = 0
        else:
            rep = sum(
                difflib.SequenceMatcher(None, a, b).ratio() >= REPEAT_RATIO for a, b in pairs
            )
            repeat_rate = round(rep / len(pairs), 3)
            repeat_pairs = len(pairs)
    reply_count = len(replies)
    return {
        "turns": len(turns),
        "replies": reply_count,
        "reply_rate": round(reply_count / len(turns), 3) if turns else None,
        "repeat_rate": repeat_rate,
        "adjacent_repeat_rate": repeat_rate,
        "repeat_pairs": repeat_pairs,
        "latency_p50_s": pct(lat, 0.5) if lat else None,
        "latency_p95_s": pct(lat, 0.95) if lat else None,
        "threshold_status": ADVISORY_THRESHOLD_STATUS,
    }


def long_session_metrics(turns: list[dict[str, Any]]) -> dict[str, Any]:
    turns = sorted(turns, key=lambda turn: turn["anchor_ts"])
    if len(turns) < MIN_TURNS_FOR_HALF:
        return {
            "verdict": "incomplete",
            "reasons": [f"incomplete: sample_turns: {len(turns)} < {MIN_TURNS_FOR_HALF}"],
            "turns": len(turns),
            "threshold_status": ADVISORY_THRESHOLD_STATUS,
        }
    mid = (turns[0]["anchor_ts"] + turns[-1]["anchor_ts"]) / 2
    first = half_stats([t for t in turns if t["anchor_ts"] < mid])
    second = half_stats([t for t in turns if t["anchor_ts"] >= mid])
    latencies = [latency for t in turns if (latency := _latency(t)) is not None]
    b, tval = slope([float(i) for i in range(1, len(latencies) + 1)], latencies)
    first_p50 = first["latency_p50_s"]
    second_p50 = second["latency_p50_s"]
    delta = None
    if isinstance(first_p50, (int, float)) and isinstance(second_p50, (int, float)):
        delta = round(float(second_p50) - float(first_p50), 3)

    reasons: list[str] = []
    if delta is None:
        reasons.append("incomplete: half_p50_unmeasurable")
    elif delta > HALF_P50_DELTA_MAX_S:
        reasons.append(f"half_p50_delta_s: {delta} > {HALF_P50_DELTA_MAX_S}")

    if delta is None:
        verdict = "incomplete"
    elif reasons:
        verdict = "fail"
    else:
        verdict = "pass"

    return {
        "thresholds": {
            "half_p50_delta_max_s": HALF_P50_DELTA_MAX_S,
            "reply_rate": ADVISORY_THRESHOLD_STATUS,
            "repeat_rate": ADVISORY_THRESHOLD_STATUS,
            "latency_slope_s_per_turn": ADVISORY_THRESHOLD_STATUS,
        },
        "threshold_status": ADVISORY_THRESHOLD_STATUS,
        "split_at": round(mid, 3),
        "first_half": first,
        "second_half": second,
        "half_p50_delta_s": delta,
        "latency_slope_s_per_turn": round(b, 4) if b is not None else None,
        "latency_slope_t": round(tval, 2) if tval is not None else None,
        "repeat_note": (
            None
            if "缺" not in (first["repeat_rate"], second["repeat_rate"])
            else "缺回复文本: timeline reply 行没有 reply_text"
        ),
        "verdict": verdict,
        "reasons": reasons,
    }


def interrupt_dual_timing(audited: dict[str, dict[str, Any]]) -> dict[str, Any]:
    samples = []
    for tag, assessment in audited.items():
        if assessment.get("stopped") is not True:
            continue
        samples.append(
            {
                "tag": tag,
                "stop_delay_from_interrupt_start_s": assessment.get(
                    "stop_delay_from_interrupt_start_s", assessment.get("stop_delay_s")
                ),
                "stop_delay_from_speech_end_s": assessment.get("stop_delay_from_speech_end_s"),
                "stop_delay_origin_primary": "interrupt_start",
            }
        )
    start_vals = [
        float(s["stop_delay_from_interrupt_start_s"])
        for s in samples
        if isinstance(s.get("stop_delay_from_interrupt_start_s"), (int, float))
    ]
    end_vals = [
        float(s["stop_delay_from_speech_end_s"])
        for s in samples
        if isinstance(s.get("stop_delay_from_speech_end_s"), (int, float))
    ]
    return {
        "confirmed_stops": len(samples),
        "samples": samples,
        "stop_delay_from_interrupt_start_p50_s": (
            statistics.median(start_vals) if start_vals else None
        ),
        "stop_delay_from_speech_end_p50_s": statistics.median(end_vals) if end_vals else None,
        "note": "both origins kept; not DAC/mic acoustics",
    }


def build_turns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    said = {row["tag"]: row for row in rows if row.get("kind") == "said" and "tag" in row}
    turns: list[dict[str, Any]] = []
    for row in rows:
        if row.get("kind") not in {"reply", "no_reply"} or "tag" not in row:
            continue
        tag = row["tag"]
        anchor = turn_anchor_ts(said.get(tag), row)
        if anchor is None:
            continue
        turns.append(
            {
                "tag": tag,
                "kind": row["kind"],
                "latency_s": row.get("latency_s"),
                "reply_text": row.get("reply_text"),
                "anchor_ts": anchor,
                "interrupted": bool(row.get("interrupted")),
            }
        )
    turns.sort(key=lambda t: t["anchor_ts"])
    return turns


def duration_s(rows: list[dict[str, Any]], turns: list[dict[str, Any]]) -> float | None:
    stamps = [float(row["ts"]) for row in rows if isinstance(row.get("ts"), (int, float))]
    if len(stamps) >= 2:
        return max(stamps) - min(stamps)
    if len(turns) >= 2:
        return turns[-1]["anchor_ts"] - turns[0]["anchor_ts"]
    return None


def report_run(run: Path) -> dict[str, Any]:
    timeline = run / "timeline.jsonl"
    source_sha = {
        "timeline.jsonl": _sha256(timeline),
        "bridge.log": _sha256(run / "bridge.log"),
        "serial.log": _sha256(run / "serial.log"),
    }
    params = _meta_fields(run)
    base: dict[str, Any] = {
        "schema_version": "voice-soak-session-report-v1",
        "run_id": run.name,
        "path": str(run.resolve()),
        "source_sha256": source_sha,
        "version": params.get("version") or params.get("agent") or None,
        "params": params,
        "gates": {"half_p50_delta_max_s": HALF_P50_DELTA_MAX_S, "min_duration_s": MIN_DURATION_S},
    }

    if not timeline.is_file() or timeline.stat().st_size == 0:
        return {
            **base,
            **_outcome("incomplete", ["incomplete: missing_log: timeline.jsonl"]),
            "duration_s": None,
            "long_session": None,
            "interrupt": None,
        }

    try:
        rows = _rows(timeline)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            **base,
            **_outcome("fail", [str(exc)]),
            "duration_s": None,
            "long_session": None,
            "interrupt": None,
        }

    turns = build_turns(rows)
    duration = duration_s(rows, turns)
    incomplete: list[str] = []
    if duration is None:
        incomplete.append("incomplete: duration_unmeasurable")
    elif duration < MIN_DURATION_S:
        incomplete.append(f"incomplete: duration_s: {duration} < {MIN_DURATION_S}")

    interrupted_claimed = any(
        row.get("kind") == "reply" and row.get("interrupted") for row in rows
    )
    if interrupted_claimed:
        for name in ("bridge.log", "serial.log"):
            if source_sha[name] is None:
                incomplete.append(f"incomplete: missing_log: {name}")

    bridge = _text(run / "bridge.log")
    serial = _text(run / "serial.log")
    audited = audit_interruptions(rows, bridge, serial) if interrupted_claimed else {}
    interrupt = interrupt_dual_timing(audited)
    long_session = long_session_metrics(turns)

    if long_session.get("verdict") == "incomplete":
        incomplete.extend(
            str(reason)
            for reason in long_session.get("reasons", [])
            if str(reason).startswith("incomplete:")
        )

    failures: list[str] = []
    if incomplete:
        status: ReportStatus = "incomplete"
        failures = incomplete
    elif long_session.get("verdict") == "fail":
        status = "fail"
        failures = list(long_session.get("reasons") or [])
    else:
        status = "pass"
        failures = []

    return {
        **base,
        **_outcome(status, failures),
        "duration_s": round(duration, 3) if duration is not None else None,
        "long_session": long_session,
        "interrupt": interrupt,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args(argv)
    result = report_run(args.run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
