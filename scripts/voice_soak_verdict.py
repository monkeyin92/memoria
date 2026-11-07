#!/usr/bin/env python3
"""Pass, fail, or mark incomplete one voice_soak run from its logs. No device, no network.

Completeness (manifest + step/log/interrupt coverage) is checked before metric gates.
A run missing required steps, logs, or valid interrupt samples is incomplete, never pass.

The tail-idle verdict, silence steps, and spoken-stop audit follow the rules the
driver and voice_soak_evidence.py already use.

    python scripts/voice_soak_verdict.py outputs/acceptance/run-<stamp>-soak
    python scripts/voice_soak_verdict.py RUN --max-no-reply N \
        --max-reply-latency-p90 SECONDS --max-reply-latency-max SECONDS
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

if __package__:
    from .voice_soak_analyze import pct
    from .voice_soak_evidence import UNVERIFIABLE_STOP_REASONS, audit_interruptions
else:
    from voice_soak_analyze import pct
    from voice_soak_evidence import UNVERIFIABLE_STOP_REASONS, audit_interruptions

MAX_NO_REPLY: int | None = 0
MAX_REPLY_LATENCY_P90_S: float | None = 2.0
MAX_REPLY_LATENCY_MAX_S: float | None = 3.0

VerdictStatus = Literal["pass", "fail", "incomplete"]

_OUTCOME_KINDS = frozenset({"reply", "no_reply", "silence", "step_failed"})
_STARTED_KINDS = frozenset({"start", "said", "reply", "silence"})


@dataclass(frozen=True)
class SoakManifest:
    scenario_hash: str
    expected_step_tags: tuple[str, ...]
    required_logs: tuple[str, ...]
    min_valid_interrupt_samples: int
    require_started: bool = True
    require_completed: bool = True
    min_duration_s: float | None = None


@dataclass(frozen=True)
class SoakVerdict:
    passed: bool
    status: VerdictStatus
    failures: tuple[str, ...]
    no_reply: int
    reply_latency_p90_s: float | None
    reply_latency_max_s: float | None
    max_no_reply: int | None
    max_reply_latency_p90_s: float | None
    max_reply_latency_max_s: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "status": self.status,
            "failures": list(self.failures),
            "no_reply": self.no_reply,
            "reply_latency_p90_s": self.reply_latency_p90_s,
            "reply_latency_max_s": self.reply_latency_max_s,
            "gates": {
                "max_no_reply": self.max_no_reply,
                "max_reply_latency_p90_s": self.max_reply_latency_p90_s,
                "max_reply_latency_max_s": self.max_reply_latency_max_s,
            },
        }


def _verdict(
    status: VerdictStatus,
    failures: tuple[str, ...],
    *,
    no_reply: int = 0,
    reply_latency_p90_s: float | None = None,
    reply_latency_max_s: float | None = None,
    max_no_reply: int | None = MAX_NO_REPLY,
    max_reply_latency_p90_s: float | None = MAX_REPLY_LATENCY_P90_S,
    max_reply_latency_max_s: float | None = MAX_REPLY_LATENCY_MAX_S,
) -> SoakVerdict:
    return SoakVerdict(
        status == "pass",
        status,
        failures,
        no_reply,
        reply_latency_p90_s,
        reply_latency_max_s,
        max_no_reply,
        max_reply_latency_p90_s,
        max_reply_latency_max_s,
    )


def manifest_dict(
    *,
    scenario_hash: str,
    expected_step_tags: list[str] | tuple[str, ...],
    required_logs: list[str] | tuple[str, ...] = (),
    min_valid_interrupt_samples: int = 0,
    require_started: bool = True,
    require_completed: bool = True,
    min_duration_s: float | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "scenario_hash": scenario_hash,
        "expected_step_tags": list(expected_step_tags),
        "required_logs": list(required_logs),
        "min_valid_interrupt_samples": min_valid_interrupt_samples,
        "require_started": require_started,
        "require_completed": require_completed,
    }
    if min_duration_s is not None:
        payload["min_duration_s"] = min_duration_s
    return payload


def write_manifest(run: Path, **fields: object) -> Path:
    path = run / "manifest.json"
    path.write_text(json.dumps(manifest_dict(**fields), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _log_inside_run(run: Path, name: str) -> Path | None:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return None
    path = run / relative
    try:
        path.resolve().relative_to(run.resolve())
    except (OSError, ValueError):
        return None
    return path


def load_manifest(run: Path) -> SoakManifest | tuple[str, ...]:
    path = run / "manifest.json"
    if not path.is_file():
        return ("incomplete: manifest missing",)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return (f"incomplete: manifest invalid: {exc}",)
    if not isinstance(raw, dict):
        return ("incomplete: manifest must be an object",)

    scenario_hash = raw.get("scenario_hash")
    if not isinstance(scenario_hash, str) or not scenario_hash.strip():
        return ("incomplete: manifest.scenario_hash required",)

    tags_raw = raw.get("expected_step_tags")
    if not isinstance(tags_raw, list) or not tags_raw or not all(isinstance(t, str) and t for t in tags_raw):
        return ("incomplete: manifest.expected_step_tags must be a non-empty string list",)

    logs_raw = raw.get("required_logs", [])
    if not isinstance(logs_raw, list) or not all(isinstance(name, str) and name for name in logs_raw):
        return ("incomplete: manifest.required_logs must be a string list",)

    samples = raw.get("min_valid_interrupt_samples", 0)
    if not isinstance(samples, int) or isinstance(samples, bool) or samples < 0:
        return ("incomplete: manifest.min_valid_interrupt_samples must be an int >= 0",)

    require_started = raw.get("require_started", True)
    require_completed = raw.get("require_completed", True)
    if not isinstance(require_started, bool) or not isinstance(require_completed, bool):
        return ("incomplete: manifest.require_started/require_completed must be bool",)

    min_duration_s = raw.get("min_duration_s")
    parsed_duration = None
    if min_duration_s is not None:
        parsed_duration = _as_float(min_duration_s)
        if parsed_duration is None or parsed_duration < 0:
            return ("incomplete: manifest.min_duration_s must be a number >= 0",)

    return SoakManifest(
        scenario_hash.strip(),
        tuple(tags_raw),
        tuple(logs_raw),
        samples,
        require_started,
        require_completed,
        parsed_duration,
    )


def check_completeness(
    manifest: SoakManifest,
    rows: list[dict[str, object]],
    run: Path,
) -> tuple[str, ...]:
    reasons: list[str] = []

    outcome_tags = {row.get("tag") for row in rows if row.get("kind") in _OUTCOME_KINDS}
    for tag in manifest.expected_step_tags:
        if tag not in outcome_tags:
            reasons.append(f"incomplete: missing_step: {tag}")

    for name in manifest.required_logs:
        path = _log_inside_run(run, name)
        if path is None or not path.is_file() or not _text(path).strip():
            reasons.append(f"incomplete: missing_log: {name}")

    bridge = _text(run / "bridge.log")
    serial = _text(run / "serial.log")
    audited = audit_interruptions(rows, bridge, serial)
    valid_interrupts = sum(
        1
        for assessment in audited.values()
        if assessment.get("stop_reason") not in UNVERIFIABLE_STOP_REASONS
    )
    if valid_interrupts < manifest.min_valid_interrupt_samples:
        reasons.append(
            f"incomplete: valid_interrupt_samples: {valid_interrupts} < {manifest.min_valid_interrupt_samples}"
        )

    if manifest.require_started and not any(row.get("kind") in _STARTED_KINDS for row in rows):
        reasons.append("incomplete: run_not_started")

    if manifest.require_completed:
        completed = any(row.get("kind") == "scenario_done" for row in rows) or any(
            row.get("kind") == "post_scenario_observation" for row in rows
        )
        if not completed:
            reasons.append("incomplete: run_not_completed")

    if manifest.min_duration_s is not None:
        stamps = [stamp for row in rows if (stamp := _as_float(row.get("ts"))) is not None]
        if len(stamps) < 2:
            reasons.append("incomplete: duration_unmeasurable")
        else:
            duration = max(stamps) - min(stamps)
            if duration < manifest.min_duration_s:
                reasons.append(f"incomplete: duration_s: {duration} < {manifest.min_duration_s}")

    return tuple(reasons)


def _rows(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"timeline.jsonl:{line_no}: invalid json") from exc
        if not isinstance(row, dict):
            raise ValueError(f"timeline.jsonl:{line_no}: row must be an object")
        rows.append(row)
    return rows


def _text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def judge(
    run: Path,
    *,
    max_no_reply: int | None = MAX_NO_REPLY,
    max_reply_latency_p90_s: float | None = MAX_REPLY_LATENCY_P90_S,
    max_reply_latency_max_s: float | None = MAX_REPLY_LATENCY_MAX_S,
) -> SoakVerdict:
    """Read one run directory and return pass, fail, or incomplete."""
    try:
        rows = _rows(run / "timeline.jsonl")
    except ValueError as exc:
        return _verdict(
            "fail",
            (str(exc),),
            max_no_reply=max_no_reply,
            max_reply_latency_p90_s=max_reply_latency_p90_s,
            max_reply_latency_max_s=max_reply_latency_max_s,
        )

    loaded = load_manifest(run)
    if isinstance(loaded, tuple):
        return _verdict(
            "incomplete",
            loaded,
            max_no_reply=max_no_reply,
            max_reply_latency_p90_s=max_reply_latency_p90_s,
            max_reply_latency_max_s=max_reply_latency_max_s,
        )

    incomplete = check_completeness(loaded, rows, run)
    if incomplete:
        return _verdict(
            "incomplete",
            incomplete,
            max_no_reply=max_no_reply,
            max_reply_latency_p90_s=max_reply_latency_p90_s,
            max_reply_latency_max_s=max_reply_latency_max_s,
        )

    failures: list[str] = []
    for row in rows:
        kind = row.get("kind")
        if kind == "abort":
            failures.append(f"abort: {row.get('reason')}")
        elif kind == "step_failed":
            failures.append(f"step_failed: {row.get('tag')} {row.get('reason')}")
        elif kind == "silence" and row.get("went_idle") is not True:
            failures.append(f"silence_not_idle: {row.get('tag')}")

    audited = audit_interruptions(rows, _text(run / "bridge.log"), _text(run / "serial.log"))
    interrupt_kinds = {
        row.get("tag"): row.get("interrupt_kind", "stop") for row in rows if row.get("kind") == "reply"
    }
    for tag, assessment in audited.items():
        if interrupt_kinds.get(tag) in {"backchannel", "crisis"}:
            if assessment.get("stopped") is True:
                failures.append(f"backchannel_stopped: {tag}")
            continue
        if (
            assessment.get("stopped") is not True
            and assessment.get("stop_reason") not in UNVERIFIABLE_STOP_REASONS
        ):
            failures.append(f"stop_not_confirmed: {tag} {assessment.get('stop_verdict')}")

    observations = [row for row in rows if row.get("kind") == "post_scenario_observation"]
    if not observations:
        failures.append("tail_idle: missing")
    elif observations[-1].get("verdict") != "verified_idle":
        failures.append(f"tail_idle: {observations[-1].get('verdict')}")

    no_reply = sum(1 for row in rows if row.get("kind") == "no_reply")
    if max_no_reply is not None and no_reply > max_no_reply:
        failures.append(f"no_reply: {no_reply} > {max_no_reply}")

    latencies = [
        float(row["latency_s"])
        for row in rows
        if row.get("kind") == "reply" and isinstance(row.get("latency_s"), int | float)
    ]
    p90 = pct(latencies, 0.90) if latencies else None
    slowest = max(latencies) if latencies else None
    for name, value, limit in (
        ("reply_latency_p90_s", p90, max_reply_latency_p90_s),
        ("reply_latency_max_s", slowest, max_reply_latency_max_s),
    ):
        if limit is not None and (value is None or value > limit):
            shown = "missing" if value is None else value
            failures.append(f"{name}: {shown} > {limit}")

    status: VerdictStatus = "pass" if not failures else "fail"
    return _verdict(
        status,
        tuple(failures),
        no_reply=no_reply,
        reply_latency_p90_s=p90,
        reply_latency_max_s=slowest,
        max_no_reply=max_no_reply,
        max_reply_latency_p90_s=max_reply_latency_p90_s,
        max_reply_latency_max_s=max_reply_latency_max_s,
    )


def _optional_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("max-no-reply must be >= 0")
    return number


def _optional_latency(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("latency gate must be >= 0")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--max-no-reply", type=_optional_int, default=MAX_NO_REPLY)
    parser.add_argument(
        "--max-reply-latency-p90", type=_optional_latency, default=MAX_REPLY_LATENCY_P90_S
    )
    parser.add_argument(
        "--max-reply-latency-max", type=_optional_latency, default=MAX_REPLY_LATENCY_MAX_S
    )
    args = parser.parse_args(argv)
    timeline = args.run / "timeline.jsonl"
    if not timeline.is_file():
        print(f"error: {timeline} is missing", file=sys.stderr)
        return 2
    result = judge(
        args.run,
        max_no_reply=args.max_no_reply,
        max_reply_latency_p90_s=args.max_reply_latency_p90,
        max_reply_latency_max_s=args.max_reply_latency_max,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
