"""Offline interruption scoring; no speaker, serial port, SSH or archive reads."""

from __future__ import annotations

import importlib
import json
import sys
import threading
import types
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from scripts import voice_soak_analyze as analyze
from scripts.voice_soak_evidence import assess_interruption, audit_interruptions

BASE = datetime(2026, 10, 10, 0, 5, tzinfo=UTC).timestamp()
DELIVERY = "session-a/epoch-1/turn-2/generation-3/tool-0"


def delivery_line(seconds: float, event: str, terminal: str = "", reason: str = "") -> str:
    at = datetime.fromtimestamp(BASE + seconds, UTC).isoformat().replace("+00:00", "Z")
    return (
        f"{at} INFO:media reply delivery session=session-a delivery_id={DELIVERY} "
        f"event={event} terminal={terminal} terminal_reason={reason} "
        "first_frame_sent=True provider_completed=True playback_ended=True actual_heard=True\n"
    )


def test_report_never_counts_a_naturally_completed_reply_as_a_stop(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    rows = [
        {
            "kind": "said",
            "tag": "t001",
            "text": "question",
            "start": "08:04:58.000",
            "ts": BASE - 1,
        },
        {
            "kind": "reply",
            "tag": "t001",
            "latency_s": 1.0,
            "reply_s": 7.0,
            "interrupted": True,
            "stopped": True,
            "stop_delay_s": 2.0,
            "end_state": "listening",
            "ts": BASE + 7,
        },
    ]
    (tmp_path / "timeline.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    (tmp_path / "serial.log").write_text(
        "08:05:00.000 I (10) MemoriaProtocol: First playable downlink frame generation=3 seq=0\n"
    )
    (tmp_path / "bridge.log").write_text(
        delivery_line(-0.1, "first_frame_sent")
        + delivery_line(7, "playback_ended", "playback_ended", "playback_completed")
    )
    monkeypatch.setattr(analyze, "archive_rows", lambda *_args: [])
    monkeypatch.setattr(analyze.sys, "argv", ["analyze", str(tmp_path), "start", "end"])

    assert analyze.main() == 0

    report = capsys.readouterr().out
    assert "确认停止 0 次" in report
    assert "打断成功" not in report


def serial_line(seconds: float, message: str) -> str:
    clock = datetime.fromtimestamp(BASE + seconds, timezone(timedelta(hours=8)))
    return f"{clock:%H:%M:%S.%f} I (100) {message}\n"


def stop_line(seconds: float = 5.01, session: str = "session-a", generation: int = 3) -> str:
    at = datetime.fromtimestamp(BASE + seconds, UTC).isoformat().replace("+00:00", "Z")
    return (
        f"{at} INFO:media spoken stop interrupted reply session={session} "
        f"turn=2 generation={generation} replacement_generation=4 flush=True\n"
    )


@pytest.fixture
def evidence():
    attempt = {
        "reply_started_ts": BASE,
        "interrupt_start_ts": BASE + 3,
        "interrupt_end_ts": BASE + 4,
        "interrupt_deadline_ts": BASE + 12,
        "device_returned_ts": BASE + 5.1,
    }
    bridge = (
        delivery_line(-0.1, "first_frame_sent")
        + delivery_line(5, "preempted", "preempted", "superseded")
        + stop_line()
    )
    serial = serial_line(
        0, "MemoriaProtocol: First playable downlink frame generation=3 seq=0"
    ) + serial_line(
        5.1,
        "AudioService: media playback supply summary generation=3 close=generation_switch output_frames=250 first_output=yes",
    )
    return bridge, serial, attempt


def test_stop_requires_a_matched_command_terminal_and_device_closure(evidence) -> None:
    bridge, serial, attempt = evidence
    result = assess_interruption(bridge, serial, attempt)
    assert result["stopped"] is True
    assert result["stop_verdict"] == "stopped"
    assert result["reply_delivery_id"] == DELIVERY
    assert result["stop_delay_s"] == 2.1
    assert result["stop_delay_origin"] == "interrupt_start"


@pytest.mark.parametrize("reason", ["superseded", "reply_task_cancelled", "output_task_cancelled"])
def test_generic_cancellation_is_not_a_spoken_stop(evidence, reason: str) -> None:
    bridge, serial, attempt = evidence
    bridge = bridge.replace(stop_line(), "").replace("superseded", reason)
    result = assess_interruption(bridge, serial, attempt)
    assert result["stopped"] is None
    assert result["stop_reason"] == "missing_matching_spoken_stop"
    assert result["stop_delay_s"] is None


@pytest.mark.parametrize(
    "terminal,reason",
    [
        ("playback_ended", "playback_completed"),
        ("error", "reply_task_exception"),
        ("transport_rejected", "playback_rejected"),
        ("no_audio", "no_audio"),
    ],
)
def test_completion_or_failure_never_counts_as_a_stop(evidence, terminal: str, reason: str) -> None:
    bridge, serial, attempt = evidence
    bridge = bridge.replace(
        delivery_line(5, "preempted", "preempted", "superseded"),
        delivery_line(5, terminal, terminal, reason),
    )
    result = assess_interruption(bridge, serial, attempt)
    assert result["stopped"] is False
    assert result["stop_delay_s"] is None


@pytest.mark.parametrize("session,generation", [("other-session", 3), ("session-a", 4)])
def test_a_stop_for_another_reply_is_not_borrowed(evidence, session: str, generation: int) -> None:
    bridge, serial, attempt = evidence
    bridge = bridge.replace(stop_line(), stop_line(session=session, generation=generation))
    assert assess_interruption(bridge, serial, attempt)["stopped"] is None


@pytest.mark.parametrize(
    "suffix", ["epoch-2/turn-2/generation-3/tool-0", "epoch-1/turn-2/generation-3/tool-1"]
)
def test_a_terminal_from_another_full_fence_is_not_borrowed(evidence, suffix: str) -> None:
    bridge, serial, attempt = evidence
    terminal = delivery_line(5, "preempted", "preempted", "superseded")
    bridge = bridge.replace(terminal, terminal.replace(DELIVERY, "session-a/" + suffix))
    assert (
        assess_interruption(bridge, serial, attempt)["stop_reason"] == "missing_delivery_terminal"
    )


def test_multiple_active_sessions_with_the_same_generation_are_ambiguous(evidence) -> None:
    bridge, serial, attempt = evidence
    bridge += delivery_line(0, "first_frame_sent").replace("session-a", "session-b")
    assert (
        assess_interruption(bridge, serial, attempt)["stop_reason"]
        == "missing_or_ambiguous_delivery"
    )


@pytest.mark.parametrize("part", ["bridge", "serial", "terminal", "device_closure"])
def test_missing_receipts_are_unverified_not_success(evidence, part: str) -> None:
    bridge, serial, attempt = evidence
    if part == "bridge":
        bridge = "Permission denied\n"
    elif part == "serial":
        serial = ""
    elif part == "terminal":
        bridge = bridge.replace(delivery_line(5, "preempted", "preempted", "superseded"), "")
    else:
        serial = serial.splitlines(keepends=True)[0]
    result = assess_interruption(bridge, serial, attempt)
    assert result["stopped"] is None
    assert result["stop_delay_s"] is None


@pytest.mark.parametrize("returned", [None, BASE + 2, BASE + 12.1])
def test_device_timeout_or_out_of_window_return_is_not_success(evidence, returned) -> None:
    bridge, serial, attempt = evidence
    attempt["device_returned_ts"] = returned
    assert assess_interruption(bridge, serial, attempt)["stopped"] is False


@pytest.mark.parametrize("at", [2.5, 12.5])
def test_stop_before_stimulus_or_after_deadline_is_not_success(evidence, at: float) -> None:
    bridge, serial, attempt = evidence
    bridge = bridge.replace(stop_line(), stop_line(at))
    assert assess_interruption(bridge, serial, attempt)["stopped"] is None


@pytest.mark.parametrize("replacement", ["generation=4", "output_frames=0", "first_output=no"])
def test_device_must_close_the_same_generation_after_really_playing(
    evidence, replacement: str
) -> None:
    bridge, serial, attempt = evidence
    original = {
        "generation=4": "generation=3",
        "output_frames=0": "output_frames=250",
        "first_output=no": "first_output=yes",
    }[replacement]
    first, last = serial.splitlines(keepends=True)
    serial = first + last.replace(original, replacement)
    assert (
        assess_interruption(bridge, serial, attempt)["stop_reason"]
        == "missing_device_playout_receipt"
    )


def test_legacy_success_flags_and_delays_are_never_trusted(evidence) -> None:
    bridge, serial, _ = evidence
    rows = [
        {"kind": "said", "tag": "t1", "ts": BASE - 1},
        {
            "kind": "reply",
            "tag": "t1",
            "latency_s": 1,
            "interrupted": True,
            "stopped": True,
            "stop_delay_s": 1,
        },
    ]
    result = audit_interruptions(rows, bridge, serial)["t1"]
    assert result["stopped"] is None
    assert result["stop_reason"] == "missing_interrupt_window"
    assert rows[1]["stopped"] is True  # Historical evidence is not rewritten.


def test_current_assessment_can_be_repaired_when_delayed_logs_arrive(evidence) -> None:
    bridge, serial, attempt = evidence
    row = {"kind": "reply", "tag": "t1", "interrupted": True, **attempt}
    row.update(assess_interruption("", serial, attempt))
    assert row["stopped"] is None
    assert audit_interruptions([row], bridge, serial)["t1"]["stopped"] is True


def test_an_unrelated_later_stop_cannot_explain_an_earlier_cancellation(evidence) -> None:
    bridge, serial, attempt = evidence
    bridge = bridge.replace(stop_line(), stop_line(10))
    assert assess_interruption(bridge, serial, attempt)["stop_reason"] == "stop_and_terminal_timing_mismatch"


def test_conflicting_terminal_receipts_are_unverified(evidence) -> None:
    bridge, serial, attempt = evidence
    bridge += delivery_line(6, "playback_ended", "playback_ended", "playback_completed")
    assert assess_interruption(bridge, serial, attempt)["stop_reason"] == "conflicting_delivery_terminals"


def test_negative_stop_delay_from_old_metrics_is_not_reused(evidence) -> None:
    bridge, serial, attempt = evidence
    attempt["interrupt_end_ts"] = BASE + 6  # The robot stops before the whole clip ends.
    attempt["stop_delay_s"] = -0.9
    assert assess_interruption(bridge, serial, attempt)["stop_delay_s"] == 2.1


def test_reply_start_and_device_closure_can_cross_midnight(evidence) -> None:
    bridge, _serial, attempt = evidence
    shift = (23 * 3600 + 59 * 60 + 58) - (8 * 3600 + 5 * 60)
    for key in attempt:
        attempt[key] += shift
    for seconds in (-0.1, 5, 5.01):
        old = datetime.fromtimestamp(BASE + seconds, UTC).isoformat().replace("+00:00", "Z")
        new = datetime.fromtimestamp(BASE + seconds + shift, UTC).isoformat().replace("+00:00", "Z")
        bridge = bridge.replace(old, new)
    serial = serial_line(shift, "First playable downlink frame generation=3 seq=0") + serial_line(
        shift + 5.1, "media playback supply summary generation=3 output_frames=250 first_output=yes"
    )
    assert assess_interruption(bridge, serial, attempt)["stopped"] is True


@pytest.mark.parametrize("natural_completion", [False, True])
def test_driver_records_the_attempt_window_and_uses_evidence_not_just_state(
    evidence, tmp_path: Path, monkeypatch, natural_completion: bool
) -> None:
    # pyserial is a device-only extra, not a dependency of these offline tests.
    monkeypatch.setitem(sys.modules, "serial", types.ModuleType("serial"))
    driver = importlib.import_module("scripts.voice_soak")
    bridge, serial, _ = evidence
    if natural_completion:
        bridge = delivery_line(-0.1, "first_frame_sent") + delivery_line(
            5, "playback_ended", "playback_ended", "playback_completed"
        )

    class FakeWatcher:
        state = "listening"
        state_since = BASE - 10
        booted_at = None
        stop = threading.Event()

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def wait_state(self, states, _timeout, since=None):
            if states == {"speaking"}:
                self.state = "speaking"
                return BASE
            self.state = "listening"
            return BASE - 10 if since is None else BASE + 5.1

    def logs(out, _remote):
        (out / "bridge.log").write_text(bridge)
        (out / "serial.log").write_text(serial)
        return []

    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps([{"say": "question", "interrupt": {"say": "stop", "after": 0}}]))
    out = tmp_path / "out"
    volumes = []
    monkeypatch.setattr(driver, "SerialWatcher", FakeWatcher)
    monkeypatch.setattr(driver, "follow_logs", logs)
    monkeypatch.setattr(driver, "now", lambda: BASE - 10)
    monkeypatch.setattr(driver.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(driver, "set_volume", lambda level: volumes.append(level) or 19)
    monkeypatch.setattr(
        driver,
        "speak",
        lambda _text, _rate, _out, tag, *_args: (
            (BASE + 3, BASE + 4, 1) if tag.endswith("-stop") else (BASE - 2, BASE - 1, 1)
        ),
    )
    monkeypatch.setattr(driver.time, "timezone", -8 * 3600)
    monkeypatch.setattr(driver.time, "altzone", -8 * 3600)
    monkeypatch.setattr(
        driver.sys,
        "argv",
        ["voice_soak", "--scenario", str(scenario), "--out", str(out), "--volume", "30"],
    )

    assert driver.main() == 0

    rows = [json.loads(line) for line in (out / "timeline.jsonl").read_text().splitlines()]
    reply = next(row for row in rows if row["kind"] == "reply")
    assert reply["stopped"] is not natural_completion
    assert reply["reply_started_ts"] == BASE
    assert reply["interrupt_start_ts"] == BASE + 3
    assert reply["interrupt_end_ts"] == BASE + 4
    assert reply["interrupt_deadline_ts"] == BASE + 12
    assert reply["device_returned_ts"] == BASE + 5.1
    assert volumes == [30, 19]
