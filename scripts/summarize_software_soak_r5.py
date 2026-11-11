"""Summarize actual three-process receipts; reject missing faults or drain evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def summarize(directory: Path) -> dict:
    config = json.loads((directory / "config.json").read_text())
    result = json.loads((directory / "result.json").read_text())
    events = []
    for line in (directory / "edge.log").read_text().splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            value = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(value, dict) and "event" in value:
            events.append(value)
    counts = Counter(row["event"] for row in events)
    cycles = [row for row in events if row["event"] == "cycle_pass"]
    samples = [json.loads(line) for line in (directory / "process.jsonl").read_text().splitlines()]
    resources = {}
    for name in ("control", "bridge", "edge"):
        rows = [row["processes"][name] for row in samples]
        resources[name] = {
            "pids": sorted({row["pid"] for row in rows}),
            "sample_count": len(rows),
            "ranges": {
                metric: {
                    "min": min(values),
                    "max": max(values),
                    "first": values[0],
                    "last": values[-1],
                }
                for metric in ("rss_bytes", "fd_count", "established_tcp")
                if (values := [row[metric] for row in rows if row[metric] is not None])
            },
            "all_readable": all(
                all(row[key] is not None for key in ("rss_bytes", "fd_count", "established_tcp"))
                for row in rows
            ),
        }
    bridge = (directory / "bridge.log").read_text()
    faults = [
        row
        for row in events
        if row["event"]
        in {
            "fault_tcp_recovered",
            "bridge_old_terminal",
            "fault_bridge_recovered",
            "fault_slow_bounded_old_rejected",
        }
    ]
    http = [row for row in events if row["event"] == "http_request"]
    slow = next((row for row in faults if row["event"] == "fault_slow_bounded_old_rejected"), {})
    predicates = {
        "sample_coverage": len(samples) >= config["duration_s"] // 5
        and samples[-1]["elapsed_s"] - samples[0]["elapsed_s"] >= config["duration_s"] - 5,
        "http_no_unexpected_status": bool(http)
        and all(row["status"] in {200, 401, 403} for row in http),
        "bridge_authority_clients_drained": "fixture authority clients after registry drain remaining=0"
        in bridge,
        "completed_duration": result.get("window_completed") is True
        and result.get("actual_window_s", 0) >= config["duration_s"],
        "three_process_resources": bool(samples)
        and all(row["all_readable"] for row in resources.values()),
        "real_audio_and_transcripts": bool(cycles)
        and counts["actual_opus_downlink"] >= len(cycles)
        and counts["actual_transcript"] >= len(cycles),
        "all_faults": all(
            counts[key] == 1
            for key in (
                "fault_tcp_recovered",
                "bridge_old_terminal",
                "fault_bridge_recovered",
                "fault_slow_bounded_old_rejected",
            )
        ),
        "actual_sigkill": len(result.get("restarts", [])) == 1
        and result["restarts"][0]["old_exit"] == -9,
        "bounded_slow_and_old_rejected": slow.get("dropped", 0) > 0
        and slow.get("max_audio_queue", 99) <= 10
        and slow.get("new_generation", 0) > slow.get("old_generation", 0),
        "every_cycle_drained": bool(cycles)
        and all(row["connections"] == row["leases"] == 0 for row in cycles),
        "old_issued_profile_rejected": "old issued profile rejected session=session_1 old_epoch=1 current_epoch=2"
        in bridge,
        "unauthorized_401_and_bad_session_403": any(row["status"] == 401 for row in http)
        and any(row["status"] == 403 for row in http),
        "cleanup": result["cleanup"]["all_reaped"] and result["cleanup"]["remove_exit"] == 0,
    }
    latencies = sorted(row["latency_ms"] for row in cycles)
    summary = {
        "candidate": config["candidate"],
        "edge_binary_sha256": config["edge_binary_sha256"],
        "start": result.get("start"),
        "end": result.get("end"),
        "actual_window_s": result.get("actual_window_s"),
        "predicates": predicates,
        "software_window_pass": all(predicates.values()),
        "event_counts": dict(counts),
        "resources": resources,
        "faults": faults,
        "http_status_counts": dict(Counter(str(row["status"]) for row in http)),
        "http_unexpected_non_success": sum(row["status"] not in {200, 401, 403} for row in http),
        "cycle_latency_ms": {
            "count": len(latencies),
            "p50": latencies[len(latencies) // 2],
            "p95": latencies[int((len(latencies) - 1) * 0.95)],
            "max": latencies[-1],
        }
        if latencies
        else None,
        "cycle_goroutines": sorted({row["goroutines"] for row in cycles}),
        "samples_first_s": samples[0]["elapsed_s"] if samples else None,
        "samples_last_s": samples[-1]["elapsed_s"] if samples else None,
        "restarts": result.get("restarts"),
        "cleanup": result["cleanup"],
        "raw_local_only": str(directory.resolve()),
        "raw_available_in_new_clone": False,
        "limits": "one shared-machine race-instrumented local window; deterministic provider; no leak absence, capacity, model quality, deployed gateway or hardware acceptance",
        "artifacts": {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in (
                "config.json",
                "result.json",
                "edge.log",
                "bridge.log",
                "control.log",
                "process.jsonl",
            )
        },
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = summarize(args.directory)
    print(
        json.dumps(
            {
                "software_window_pass": result["software_window_pass"],
                "predicates": result["predicates"],
            },
            indent=2,
        )
    )
    return 0 if result["software_window_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
