"""统计 bridge.log 中 TTS 首包到发送的分段延迟。"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

EVENT_NAMES = (
    "first_phrase_ready",
    "doubao_session_started",
    "doubao_first_pcm",
    "first_frame_sent",
    "last_user_audio",
)
SEGMENTS = (
    ("first_phrase_ready", "doubao_session_started"),
    ("doubao_session_started", "doubao_first_pcm"),
    ("doubao_first_pcm", "first_frame_sent"),
    ("first_phrase_ready", "first_frame_sent"),
)
TIMESTAMP_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\b"
)
EVENT_RE = re.compile(
    r"(?:agent_audio_trace name=(?P<trace>"
    r"first_phrase_ready|doubao_session_started|doubao_first_pcm|last_user_audio"
    r")\b|event=(?P<frame>first_frame_sent)\b)"
)


@dataclass(frozen=True)
class Event:
    name: str
    timestamp: datetime
    line_number: int


@dataclass(frozen=True)
class Pairing:
    values: tuple[float, ...]
    ambiguous: int
    missing_end: int
    orphan_end: int
    out_of_order: int

    @property
    def discarded_rounds(self) -> int:
        return self.missing_end + self.out_of_order


def parse_lines(lines: Iterable[str]) -> tuple[tuple[Event, ...], int]:
    """从日志行提取目标事件，并返回无法解析的目标事件行数。"""
    events: list[Event] = []
    invalid_timestamp_events = 0
    for line_number, line in enumerate(lines, 1):
        event_match = EVENT_RE.search(line)
        if event_match is None:
            continue
        timestamp_match = TIMESTAMP_RE.match(line)
        if timestamp_match is None:
            invalid_timestamp_events += 1
            continue
        try:
            timestamp = datetime.fromisoformat(
                timestamp_match.group("timestamp").replace("Z", "+00:00")
            )
        except ValueError:
            invalid_timestamp_events += 1
            continue
        name = event_match.group("trace") or event_match.group("frame")
        events.append(Event(name=name, timestamp=timestamp, line_number=line_number))
    return tuple(events), invalid_timestamp_events


def pair_events(
    events: Iterable[Event], start_name: str, end_name: str
) -> Pairing:
    """按日志顺序配对起止事件并隔离无法判定归属的重叠组。

    ``ambiguous`` 统计在同一重叠组中、随后被两个 end 消费的 start 事件数。
    两个或更多 start 后，若在下一个 start 前出现两个 end，则整组 start
    和 end 都不产生样本、不计入 ``orphan_end``，重叠组也不计入
    ``discarded_rounds``。只有一个 end 的序列继续按缺少前一个 end 的旧
    语义解析。
    """
    values: list[float] = []
    pending: Event | None = None
    overlap_starts: list[Event] = []
    overlap_end: Event | None = None
    ambiguous = 0
    missing_end = 0
    orphan_end = 0
    out_of_order = 0

    for event in events:
        if event.name == start_name:
            if overlap_starts:
                if overlap_end is None:
                    overlap_starts.append(event)
                    continue
                missing_end += len(overlap_starts) - 1
                latest_start = overlap_starts[-1]
                if overlap_end.timestamp < latest_start.timestamp:
                    out_of_order += 1
                else:
                    values.append(
                        (overlap_end.timestamp - latest_start.timestamp).total_seconds()
                    )
                overlap_starts = []
                overlap_end = None
                pending = event
                continue
            if pending is not None:
                overlap_starts = [pending, event]
                pending = None
                continue
            pending = event
            continue
        if event.name != end_name:
            continue
        if overlap_starts:
            if overlap_end is None:
                overlap_end = event
            else:
                ambiguous += len(overlap_starts)
                overlap_starts = []
                overlap_end = None
            continue
        if pending is None:
            orphan_end += 1
            continue
        if event.timestamp < pending.timestamp:
            out_of_order += 1
            pending = None
            continue
        values.append((event.timestamp - pending.timestamp).total_seconds())
        pending = None

    if overlap_starts:
        if overlap_end is None:
            missing_end += len(overlap_starts)
        else:
            missing_end += len(overlap_starts) - 1
            latest_start = overlap_starts[-1]
            if overlap_end.timestamp < latest_start.timestamp:
                out_of_order += 1
            else:
                values.append(
                    (overlap_end.timestamp - latest_start.timestamp).total_seconds()
                )
    elif pending is not None:
        missing_end += 1

    return Pairing(
        values=tuple(values),
        ambiguous=ambiguous,
        missing_end=missing_end,
        orphan_end=orphan_end,
        out_of_order=out_of_order,
    )


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def summarize(values: Iterable[float]) -> dict[str, float | int | None]:
    materialized = tuple(values)
    return {
        "samples": len(materialized),
        "p50": percentile(materialized, 0.50),
        "p90": percentile(materialized, 0.90),
        "max": max(materialized) if materialized else None,
        "mean": math.fsum(materialized) / len(materialized) if materialized else None,
    }


def _segment_report(pairing: Pairing) -> dict[str, object]:
    return {
        **summarize(pairing.values),
        "ambiguous": pairing.ambiguous,
        "missing_end": pairing.missing_end,
        "orphan_end": pairing.orphan_end,
        "out_of_order": pairing.out_of_order,
        "discarded_rounds": pairing.discarded_rounds,
    }


def analyze_lines(
    lines: Iterable[str], run_name: str = "<synthetic>"
) -> dict[str, object]:
    events, invalid_timestamp_events = parse_lines(lines)
    segments: dict[str, dict[str, object]] = {}
    segment_values: dict[str, tuple[float, ...]] = {}
    for start_name, end_name in SEGMENTS:
        pairing = pair_events(events, start_name, end_name)
        key = f"{start_name}->{end_name}"
        segments[key] = _segment_report(pairing)
        segment_values[key] = pairing.values
    event_counts = {
        event_name: sum(event.name == event_name for event in events)
        for event_name in EVENT_NAMES
    }
    return {
        "run": run_name,
        "event_counts": event_counts,
        "invalid_timestamp_events": invalid_timestamp_events,
        "segments": segments,
        "_segment_values": segment_values,
    }


def analyze_log(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        return analyze_lines(handle, path.parent.name)


def _resolve_log(path: Path) -> Path:
    return path / "bridge.log" if path.is_dir() else path


def analyze_paths(paths: Iterable[Path]) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []
    for requested_path in paths:
        log_path = _resolve_log(requested_path)
        if not log_path.is_file():
            skipped.append(
                {
                    "path": str(requested_path),
                    "reason": "missing bridge.log"
                    if requested_path.is_dir()
                    else "missing log file",
                }
            )
            continue
        reports.append(analyze_log(log_path))

    overall_segments: dict[str, dict[str, object]] = {}
    for start_name, end_name in SEGMENTS:
        key = f"{start_name}->{end_name}"
        values: list[float] = []
        ambiguous = 0
        missing_end = 0
        orphan_end = 0
        out_of_order = 0
        for report in reports:
            segment = report["segments"][key]
            values.extend(
                value
                for value in report["_segment_values"][key]
            )
            ambiguous += int(segment["ambiguous"])
            missing_end += int(segment["missing_end"])
            orphan_end += int(segment["orphan_end"])
            out_of_order += int(segment["out_of_order"])
        overall_segments[key] = {
            **summarize(values),
            "ambiguous": ambiguous,
            "missing_end": missing_end,
            "orphan_end": orphan_end,
            "out_of_order": out_of_order,
            "discarded_rounds": missing_end + out_of_order,
        }

    return {
        "runs": reports,
        "overall": {
            "segments": overall_segments,
            "run_count": len(reports),
        },
        "skipped": skipped,
    }

def format_seconds(value: object) -> str:
    if value is None:
        return "—"
    return f"{float(value):.3f}s"


def format_segment_table(segments: dict[str, dict[str, object]]) -> list[str]:
    lines = [
        "segment                                      samples       p50       p90       max      mean",
        "-------------------------------------------  -------  --------  --------  --------  --------",
    ]
    for key, segment in segments.items():
        lines.append(
            f"{key.replace('->', '→'):<43} "
            f"{int(segment['samples']):>7}  "
            f"{format_seconds(segment['p50']):>8}  "
            f"{format_seconds(segment['p90']):>8}  "
            f"{format_seconds(segment['max']):>8}  "
            f"{format_seconds(segment['mean']):>8}"
        )
    return lines


def format_report(report: dict[str, object]) -> str:
    lines = ["First-packet latency report", ""]
    for run in report["runs"]:
        lines.extend(
            [
                f"Run: {run['run']}",
                "  events: "
                + ", ".join(
                    f"{name}={count}"
                    for name, count in run["event_counts"].items()
                ),
                f"  invalid timestamp events: {run['invalid_timestamp_events']}",
            ]
        )
        lines.extend(f"  {line}" for line in format_segment_table(run["segments"]))
        lines.append("  diagnostics:")
        for key, segment in run["segments"].items():
            lines.append(
                f"    {key.replace('->', '→')}: "
                f"discarded_rounds={segment['discarded_rounds']}, "
                f"ambiguous={segment['ambiguous']}, "
                f"missing_end={segment['missing_end']}, "
                f"orphan_end={segment['orphan_end']}, "
                f"out_of_order={segment['out_of_order']}"
            )
        lines.append("")

    lines.append("Overall")
    lines.extend(format_segment_table(report["overall"]["segments"]))
    lines.append(
        f"runs analyzed={report['overall']['run_count']}, "
        f"runs skipped={len(report['skipped'])}"
    )
    for skipped in report["skipped"]:
        lines.append(f"skipped: {skipped['path']} ({skipped['reason']})")
    return "\n".join(lines)


def _report_for_json(report: dict[str, object]) -> dict[str, object]:
    return {
        "runs": [
            {key: value for key, value in run.items() if key != "_segment_values"}
            for run in report["runs"]
        ],
        "overall": report["overall"],
        "skipped": report["skipped"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="统计 bridge.log 中 TTS 首包延迟分布。"
    )
    parser.add_argument("paths", nargs="+", type=Path, help="run 目录或 bridge.log 路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    report = analyze_paths(args.paths)
    if args.json:
        print(json.dumps(_report_for_json(report), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
