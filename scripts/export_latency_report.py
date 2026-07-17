#!/usr/bin/env python3
"""Export latency stage report from a LatencyTrace snapshot JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from services.agent.src.observability.tracing import LatencyTrace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="JSON file with marks dict")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.input and args.input.exists():
        marks = json.loads(args.input.read_text())
        tr = LatencyTrace(marks=marks.get("marks", marks))
    else:
        tr = LatencyTrace()
        print("no input marks; empty report", file=sys.stderr)

    report = tr.derived()
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
