"""Summarize one completed software window without inferring production stability."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any


def summarize(directory: Path) -> dict[str, Any]:
    result_path = directory / "result.json"
    if not result_path.is_file():
        return {"window_completed": False, "reason": "end receipt missing"}
    result = json.loads(result_path.read_text())
    samples = [json.loads(line) for line in (directory / "process.jsonl").read_text().splitlines()]
    cycles = [
        json.loads(match.group(1))
        for match in re.finditer(
            r"software_soak_window_test.go:\d+: (\{[^\n]+\})", (directory / "edge.log").read_text()
        )
    ]
    if not samples or not cycles:
        return {"window_completed": False, "reason": "resource samples or completed cycles missing"}
    measured = all(
        all(row.get(key) is not None for key in ("rss_bytes", "fd_count", "established_tcp"))
        for row in samples
    )
    rss = [row["rss_bytes"] for row in samples if row["rss_bytes"] is not None]
    latencies = sorted(row["cycle_latency_ms"] for row in cycles)
    return {
        "window_completed": result.get("window_completed") is True
        and result.get("exit_code") == 0
        and result.get("elapsed_s", 0) >= 900,
        "process_exit_code": result.get("exit_code"),
        "elapsed_s": result.get("elapsed_s"),
        "resource_sample_count": len(samples),
        "completed_connection_cycles": len(cycles),
        "resources_measured": measured,
        "rss_first_bytes": rss[0] if rss else None,
        "rss_last_bytes": rss[-1] if rss else None,
        "rss_min_bytes": min(rss) if rss else None,
        "rss_max_bytes": max(rss) if rss else None,
        "fd_min": min(row["fd_count"] for row in samples) if measured else None,
        "fd_max": max(row["fd_count"] for row in samples) if measured else None,
        "established_tcp_max_between_cycles": max(row["established_tcp"] for row in samples)
        if measured
        else None,
        "connection_cycle_latency_p50_ms": statistics.median(latencies),
        "connection_cycle_latency_p95_ms": latencies[math.ceil(0.95 * len(latencies)) - 1],
        "every_cycle_drained": all(
            row["connections"] == 0 and row["leases"] == 0 for row in cycles
        ),
        "final_cycle": cycles[-1],
        "production_bridge_verified": False,
        "memory_leak_verdict": "inconclusive: one instrumented window with a synthetic core",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = summarize(args.directory)
    print(json.dumps(result, indent=2))
    return (
        0
        if result["window_completed"]
        and result.get("resources_measured")
        and result.get("every_cycle_drained")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
