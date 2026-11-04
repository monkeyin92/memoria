"""Offline long-session + dual interrupt report. No device or network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from scripts import voice_soak_session_report as report

BASE = datetime(2026, 10, 31, 8, 0, tzinfo=UTC).timestamp()
DELIVERY = "session-a/epoch-1/turn-2/generation-3/tool-0"


def _delivery(seconds: float, event: str, terminal: str = "", reason: str = "") -> str:
    at = datetime.fromtimestamp(BASE + seconds, UTC).isoformat().replace("+00:00", "Z")
    return (
        f"{at} INFO:media reply delivery session=session-a delivery_id={DELIVERY} "
        f"event={event} terminal={terminal} terminal_reason={reason} "
        "first_frame_sent=True provider_completed=True playback_ended=True actual_heard=True\n"
    )


def _serial(seconds: float, message: str) -> str:
    clock = datetime.fromtimestamp(BASE + seconds, timezone(timedelta(hours=8)))
    return f"{clock:%H:%M:%S.%f} I (100) {message}\n"


def _confirmed_stop_logs() -> tuple[str, str]:
    at = datetime.fromtimestamp(BASE + 5.01, UTC).isoformat().replace("+00:00", "Z")
    bridge = (
        _delivery(-0.1, "first_frame_sent")
        + _delivery(5, "preempted", "preempted", "superseded")
        + (
            f"{at} INFO:media spoken stop interrupted reply session=session-a "
            "turn=2 generation=3 replacement_generation=4 flush=True\n"
        )
    )
    serial = _serial(
        0, "MemoriaProtocol: First playable downlink frame generation=3 seq=0"
    ) + _serial(
        5.1,
        "AudioService: media playback supply summary generation=3 close=generation_switch "
        "output_frames=250 first_output=yes",
    )
    return bridge, serial


def _write(
    run: Path,
    rows: list[dict[str, object]],
    bridge: str = "",
    serial: str = "",
    *,
    meta: str = "",
) -> None:
    run.mkdir()
    (run / "timeline.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    if bridge:
        (run / "bridge.log").write_text(bridge, encoding="utf-8")
    if serial:
        (run / "serial.log").write_text(serial, encoding="utf-8")
    if meta:
        (run / "meta.txt").write_text(meta, encoding="utf-8")


def _turn_pair(
    index: int,
    *,
    offset_s: float,
    latency_s: float,
    reply_text: str,
    interrupted: bool = False,
) -> list[dict[str, object]]:
    tag = f"t{index:03d}"
    # Interrupted fixture aligns reply_started_ts with confirmed-stop logs at BASE.
    if interrupted:
        said_ts = BASE - 1
        reply_started = BASE
    else:
        said_ts = BASE + offset_s
        reply_started = said_ts + latency_s
    rows: list[dict[str, object]] = [
        {
            "kind": "said",
            "tag": tag,
            "text": f"问{index}",
            "ts": said_ts,
            "start": "00:00:00.000",
            "end": "00:00:01.000",
        },
        {
            "kind": "reply",
            "tag": tag,
            "latency_s": latency_s,
            "reply_text": reply_text,
            "reply_started_ts": reply_started,
            "ts": reply_started,
        },
    ]
    if interrupted:
        rows[1].update(
            interrupted=True,
            interrupt_start_ts=BASE + 3,
            interrupt_end_ts=BASE + 4,
            interrupt_deadline_ts=BASE + 12,
            device_returned_ts=BASE + 5.1,
            serial_utc_offset_s=8 * 3600,
        )
    return rows


def _long_run_rows(
    *,
    first_latencies: list[float],
    second_latencies: list[float],
    with_interrupt: bool = True,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    # Spread across >15 minutes: first half near t=0, second near t=900+.
    for i, latency in enumerate(first_latencies, 1):
        rows.extend(
            _turn_pair(
                i,
                offset_s=60 + i * 30,
                latency_s=latency,
                reply_text=f"答前{i}",
                interrupted=with_interrupt and i == 1,
            )
        )
    start_second = len(first_latencies)
    for j, latency in enumerate(second_latencies, 1):
        rows.extend(
            _turn_pair(
                start_second + j,
                offset_s=900 + j * 30,
                latency_s=latency,
                reply_text=f"答后{j}",
            )
        )
    rows.append({"kind": "post_scenario_observation", "verdict": "verified_idle", "ts": BASE + 1200})
    return rows


def test_normal_long_session_pass_with_dual_interrupt_origins(tmp_path: Path) -> None:
    bridge, serial = _confirmed_stop_logs()
    run = tmp_path / "run-normal"
    rows = _long_run_rows(
        first_latencies=[2.0, 2.0, 2.1, 2.0],
        second_latencies=[2.1, 2.0, 2.2, 2.1],
    )
    _write(run, rows, bridge, serial, meta="version=test-agent\nvolume=30\n")
    result = report.report_run(run)
    assert result["status"] == "pass"
    assert result["passed"] is True
    assert result["duration_s"] >= 900
    assert result["long_session"]["half_p50_delta_s"] <= 0.3
    assert result["long_session"]["thresholds"]["reply_rate"] == "待产品确认"
    assert result["interrupt"]["confirmed_stops"] == 1
    sample = result["interrupt"]["samples"][0]
    assert sample["stop_delay_from_interrupt_start_s"] == 2.1
    assert sample["stop_delay_from_speech_end_s"] == 1.1
    assert result["source_sha256"]["timeline.jsonl"]
    assert result["run_id"] == "run-normal"
    assert report.main([str(run)]) == 0


def test_fail_when_second_half_p50_rises_over_0_3s(tmp_path: Path) -> None:
    bridge, serial = _confirmed_stop_logs()
    run = tmp_path / "run-fail"
    rows = _long_run_rows(
        first_latencies=[2.0, 2.0, 2.0, 2.0],
        second_latencies=[2.5, 2.5, 2.6, 2.5],
    )
    _write(run, rows, bridge, serial)
    result = report.report_run(run)
    assert result["status"] == "fail"
    assert result["passed"] is False
    assert result["long_session"]["half_p50_delta_s"] > 0.3
    assert any("half_p50_delta_s" in f for f in result["failures"])
    assert report.main([str(run)]) == 1


def test_incomplete_when_duration_under_15_min(tmp_path: Path) -> None:
    run = tmp_path / "run-short"
    rows = []
    for i in range(1, 5):
        rows.extend(_turn_pair(i, offset_s=i * 10, latency_s=2.0, reply_text=f"短{i}"))
    rows.append({"kind": "post_scenario_observation", "verdict": "verified_idle", "ts": BASE + 50})
    _write(run, rows)
    result = report.report_run(run)
    assert result["status"] == "incomplete"
    assert result["passed"] is False
    assert any("duration_s" in f for f in result["failures"])


def test_incomplete_when_interrupt_logs_missing(tmp_path: Path) -> None:
    run = tmp_path / "run-missing-logs"
    rows = _long_run_rows(
        first_latencies=[2.0, 2.0, 2.0, 2.0],
        second_latencies=[2.0, 2.0, 2.0, 2.0],
        with_interrupt=True,
    )
    _write(run, rows)  # no bridge/serial
    result = report.report_run(run)
    assert result["status"] == "incomplete"
    assert "incomplete: missing_log: bridge.log" in result["failures"]
    assert "incomplete: missing_log: serial.log" in result["failures"]
