#!/usr/bin/env python3
"""Summarize the bridge's commit and classifier timing lines (TODOLIST N-14 4).

Round 11 could not say what the ~2.7 s between a sentence's ASR final and its turn commit consists of, nor whether
the two cloud classifier calls (live lookup, conversation close) are on that path.  The bridge now logs, never the
text, one ``media turn commit timing`` line per commit, one ``classifier call`` line per cloud call and one
``classifier verdict wait`` line whenever a turn path had to wait for a verdict (a cached verdict logs nothing).
Logs from before TODOLIST N-14 7 also hold the waits of the background evaluation that starts at the ASR final;
there, and always for "did the commit wait", read ``close_ms`` of the commit lines instead::

    ssh <host> "docker logs -t --since 2h memoria-voice-core-media-bridge-1" > bridge.log 2>&1
    python scripts/voice_commit_timing.py bridge.log [--session 0d3185b8]

The commit table splits a commit into ``since_endpoint_ms`` (speech end -> commit start: VAD tail, grace and ASR
wait), ``final_to_start_ms`` (last ASR final -> commit start), the awaits inside the commit (``speaker``, ``close``
= the conversation-close verdict, ``prepare`` = reply preparation: runtime profile, plan, memory) and
``final_to_done_ms`` (what the child waits for).  Percentiles are nearest-rank; ``-`` means a step was not reached.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

COMMIT = "media turn commit timing "
CALL = "classifier call "
WAIT = "classifier verdict wait "
COMMIT_FIELDS = (
    "since_endpoint_ms",
    "final_to_start_ms",
    "lock_ms",
    "speaker_ms",
    "close_ms",
    "prepare_ms",
    "commit_ms",
    "final_to_done_ms",
)
# A verdict the commit waited this long for is a real cost on the path; shorter waits are scheduling noise.
SIGNIFICANT_WAIT_MS = 50


def _fields(line: str, marker: str) -> dict[str, str] | None:
    head, found, tail = line.partition(marker)
    if not found:
        return None
    return dict(part.split("=", 1) for part in tail.split() if "=" in part)


def _number(raw: str | None) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _percentile(values: Sequence[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _row(values: Sequence[int]) -> str:
    if not values:
        return "n=0"
    return (
        f"n={len(values)} p50={_percentile(values, 0.5)} p90={_percentile(values, 0.9)} max={max(values)}"
    )


def summarize(lines: Iterable[str], *, session: str = "") -> str:
    commits: list[dict[str, str]] = []
    calls: dict[str, list[dict[str, str]]] = defaultdict(list)
    waits: dict[tuple[str, str], list[int]] = defaultdict(list)
    for line in lines:
        if (commit := _fields(line, COMMIT)) is not None:
            if session in commit.get("session", ""):
                commits.append(commit)
        elif (call := _fields(line, CALL)) is not None:
            calls[call.get("kind", "?")].append(call)
        elif (wait := _fields(line, WAIT)) is not None and (waited := _number(wait.get("waited_ms"))) is not None:
            waits[(wait.get("kind", "?"), wait.get("source", "?"))].append(waited)

    out = [f"== turn commits n={len(commits)}  results: " + ", ".join(
        f"{name}={count}" for name, count in Counter(c.get("result", "?") for c in commits).most_common()
    )]
    for name in COMMIT_FIELDS:
        values = [v for c in commits if (v := _number(c.get(name))) is not None]
        out.append(f"  {name:<18} {_row(values)}")
    out.append("== classifier calls (cloud round trips, duration_ms)")
    for kind, items in sorted(calls.items()):
        outcomes = Counter(i.get("outcome", "?") for i in items)
        durations = [v for i in items if (v := _number(i.get("duration_ms"))) is not None]
        out.append(f"  {kind:<20} {_row(durations)}  outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
    out.append("== waits for a verdict (waited_ms; a cached verdict logs nothing; older logs include background waits)")
    for (kind, source), values in sorted(waits.items()):
        slow = sum(1 for v in values if v >= SIGNIFICANT_WAIT_MS)
        out.append(f"  {kind:<20} {source:<7} {_row(values)}  waited>={SIGNIFICANT_WAIT_MS}ms: {slow}")
    if not waits:
        out.append("  (none)")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", type=Path, help="bridge log (docker logs -t output)")
    parser.add_argument("--session", default="", help="only commits whose session id contains this text")
    args = parser.parse_args(argv)
    text = args.log.read_text(encoding="utf-8", errors="replace")
    print(summarize(text.splitlines(), session=args.session))
    return 0


if __name__ == "__main__":
    sys.exit(main())
