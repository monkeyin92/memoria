#!/usr/bin/env python3
"""Pass or fail one voice_soak run from its logs. No device, no network.

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

if __package__:
    from .voice_soak_analyze import pct
    from .voice_soak_evidence import UNVERIFIABLE_STOP_REASONS, audit_interruptions
else:
    from voice_soak_analyze import pct
    from voice_soak_evidence import UNVERIFIABLE_STOP_REASONS, audit_interruptions

MAX_NO_REPLY: int | None = 0
MAX_REPLY_LATENCY_P90_S: float | None = 2.0
MAX_REPLY_LATENCY_MAX_S: float | None = 3.0


@dataclass(frozen=True)
class SoakVerdict:
    passed: bool
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
    """Read one run directory and return the pass/fail result."""
    try:
        rows = _rows(run / "timeline.jsonl")
    except ValueError as exc:
        return SoakVerdict(
            False,
            (str(exc),),
            0,
            None,
            None,
            max_no_reply,
            max_reply_latency_p90_s,
            max_reply_latency_max_s,
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
    for tag, assessment in audited.items():
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

    return SoakVerdict(
        not failures,
        tuple(failures),
        no_reply,
        p90,
        slowest,
        max_no_reply,
        max_reply_latency_p90_s,
        max_reply_latency_max_s,
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
