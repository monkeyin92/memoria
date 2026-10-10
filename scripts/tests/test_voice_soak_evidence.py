"""Offline interruption scoring; no speaker, serial port, SSH or archive reads."""

from __future__ import annotations

import importlib
import json
import sys
import threading
import time
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
@pytest.mark.parametrize(
    ("post_verdict", "expected_exit"),
    [("verified_idle", 0), ("timeout", 3), ("evidence_insufficient", 4)],
)
def test_driver_records_the_attempt_window_and_uses_evidence_not_just_state(
    evidence,
    tmp_path: Path,
    monkeypatch,
    natural_completion: bool,
    post_verdict: str,
    expected_exit: int,
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

        def observe_post_scenario(self, _timeout):
            final_state = "listening" if post_verdict == "timeout" else "idle"
            return {
                "idle_observed": post_verdict == "verified_idle",
                "final_state": final_state,
                "final_state_source": (
                    "serial_transition"
                    if post_verdict == "verified_idle"
                    else "assumed_idle"
                    if post_verdict == "evidence_insufficient"
                    else "serial_transition"
                ),
                "final_state_since": driver.stamp(BASE + 6),
                "waited_s": 0,
                "timeout_s": 0,
                "verdict": post_verdict,
                "timed_out": post_verdict == "timeout",
            }

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

    assert driver.main() == expected_exit

    rows = [json.loads(line) for line in (out / "timeline.jsonl").read_text().splitlines()]
    reply = next(row for row in rows if row["kind"] == "reply")
    assert reply["stopped"] is not natural_completion
    assert reply["reply_started_ts"] == BASE
    assert reply["interrupt_start_ts"] == BASE + 3
    assert reply["interrupt_end_ts"] == BASE + 4
    assert reply["interrupt_deadline_ts"] == BASE + 12
    assert reply["device_returned_ts"] == BASE + 5.1
    assert volumes == [30, 19]
    observation = next(row for row in rows if row["kind"] == "post_scenario_observation")
    assert observation["final_state"] == ("listening" if post_verdict == "timeout" else "idle")
    assert observation["verdict"] == post_verdict
    assert observation["timed_out"] is (post_verdict == "timeout")


def test_driver_waits_for_a_late_wake_greeting_before_playing_the_first_line(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setitem(sys.modules, "serial", types.ModuleType("serial"))
    driver = importlib.import_module("scripts.voice_soak")
    listening_at = BASE
    greeting_at = listening_at + 4.2641
    greeting_done_at = listening_at + 6
    reply_at = listening_at + 12
    reply_done_at = listening_at + 15
    watchers = []

    class DelayedGreetingWatcher(driver.SerialWatcher):
        def __init__(self, *_args, **_kwargs):
            super().__init__(tmp_path / "serial.log")
            self.state = "idle"
            self.state_since = BASE - 10

        def start(self):
            pass

        def observe_post_scenario(self, _timeout):
            return {
                "idle_observed": True,
                "final_state": "idle",
                "final_state_source": "serial_transition",
                "final_state_since": driver.stamp(reply_done_at),
                "waited_s": 0,
                "timeout_s": 0,
                "verdict": "verified_idle",
                "timed_out": False,
            }

        def wake(self):
            pass

        def wait_state(self, states, _timeout, since=None):
            if states == {"idle", "listening"}:
                return self.state_since if self.state in states else None
            if states == {"connecting", "listening"}:
                return listening_at - 0.1
            if states == {"listening", "idle"} and since == greeting_at:
                self._parse(greeting_done_at, "StateMachine: State: speaking -> listening")
                return greeting_done_at
            if states == {"listening", "idle"} and since == reply_at:
                self._parse(reply_done_at, "StateMachine: State: speaking -> listening")
                return reply_done_at
            if states == {"speaking"}:
                self._parse(reply_at, "StateMachine: State: listening -> speaking")
                return reply_at
            return None

        def wait_state_event(self, states, _timeout, since):
            if states == {"listening"}:
                self._parse(listening_at - 0.1, "StateMachine: State: idle -> connecting")
                self._parse(listening_at, "StateMachine: State: connecting -> listening")
            else:
                assert states == {"speaking"}
                assert since == listening_at
                self._parse(greeting_at, "StateMachine: State: listening -> speaking")
                self._parse(greeting_done_at, "StateMachine: State: speaking -> listening")
            return super().wait_state_event(states, 0, since)

    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps([{"tag": "hello", "say": "hello"}]))
    out = tmp_path / "out"
    spoken: list[tuple[str, str, float]] = []

    def make_watcher(*_args, **_kwargs):
        watcher = DelayedGreetingWatcher()
        watchers.append(watcher)
        return watcher

    monkeypatch.setattr(driver, "SerialWatcher", make_watcher)
    monkeypatch.setattr(driver, "follow_logs", lambda *_args: [])
    monkeypatch.setattr(driver, "now", lambda: BASE - 10)
    monkeypatch.setattr(driver.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(driver, "set_volume", lambda _level: 19)
    monkeypatch.setattr(
        driver,
        "speak",
        lambda _text, _rate, _out, tag, *_args: (
            spoken.append((tag, watchers[0].state, watchers[0].state_since))
            or (BASE + 8, BASE + 9, 1)
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
    kinds = [row["kind"] for row in rows]
    assert spoken == [("t001-hello", "listening", greeting_done_at)]
    assert kinds.index("greeting_started") < kinds.index("greeting_done") < kinds.index("said")
    greeting = next(row for row in rows if row["kind"] == "greeting_started")
    assert greeting["started"] == driver.stamp(greeting_at)


def test_post_scenario_observation_keeps_collecting_until_late_idle(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setitem(sys.modules, "serial", types.ModuleType("serial"))
    driver = importlib.import_module("scripts.voice_soak")
    monkeypatch.setattr(driver, "now", lambda: BASE)
    watcher = driver.SerialWatcher(tmp_path / "serial.log")
    watcher.state = "listening"
    watcher.state_since = BASE - 1

    def delayed_transitions():
        time.sleep(0.01)
        watcher._parse(BASE + 0.01, "StateMachine: State: listening -> speaking")
        time.sleep(0.01)
        watcher._parse(BASE + 0.02, "StateMachine: State: speaking -> idle")

    transition_thread = threading.Thread(target=delayed_transitions)
    transition_thread.start()
    result = watcher.observe_post_scenario(0.04)
    transition_thread.join()

    assert result["idle_observed"] is True
    assert result["final_state"] == "idle"
    assert result["final_state_source"] == "serial_transition"
    assert result["verdict"] == "verified_idle"
    assert result["timed_out"] is False
    assert result["waited_s"] >= 0.03


def test_post_scenario_timeout_captures_non_idle_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "serial", types.ModuleType("serial"))
    driver = importlib.import_module("scripts.voice_soak")
    watcher = driver.SerialWatcher(tmp_path / "serial.log")
    watcher.state = "listening"
    watcher.state_since = BASE - 1
    result = watcher.observe_post_scenario(0.01)

    assert result["idle_observed"] is False
    assert result["final_state"] == "listening"
    assert result["final_state_source"] == "unknown"
    assert result["verdict"] == "timeout"
    assert result["timed_out"] is True
    assert result["waited_s"] >= 0.01


def test_post_scenario_assumed_idle_is_insufficient_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setitem(sys.modules, "serial", types.ModuleType("serial"))
    driver = importlib.import_module("scripts.voice_soak")
    watcher = driver.SerialWatcher(tmp_path / "serial.log")
    watcher.state = "idle"
    watcher.state_since = BASE - 1
    watcher.state_source = "assumed_idle"

    result = watcher.observe_post_scenario(0.01)

    assert result["idle_observed"] is False
    assert result["final_state"] == "idle"
    assert result["final_state_source"] == "assumed_idle"
    assert result["verdict"] == "evidence_insufficient"
    assert result["timed_out"] is False


def test_preexisting_serial_idle_is_not_observed_in_the_post_scenario_window(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setitem(sys.modules, "serial", types.ModuleType("serial"))
    driver = importlib.import_module("scripts.voice_soak")
    monkeypatch.setattr(driver, "now", lambda: BASE)
    watcher = driver.SerialWatcher(tmp_path / "serial.log")
    watcher.state = "idle"
    watcher.state_since = BASE - 1
    watcher.state_source = "serial_transition"

    result = watcher.observe_post_scenario(0.01)

    assert result["idle_observed"] is False
    assert result["final_state"] == "idle"
    assert result["final_state_source"] == "serial_transition"
    assert result["verdict"] == "evidence_insufficient"


def test_report_marks_legacy_missing_post_scenario_observation_unverified(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    (tmp_path / "timeline.jsonl").write_text(
        json.dumps({"kind": "scenario_done", "turns": 4, "ts": BASE})
    )
    (tmp_path / "bridge.log").write_text("")
    monkeypatch.setattr(analyze, "archive_rows", lambda *_args: [])
    monkeypatch.setattr(analyze.sys, "argv", ["analyze", str(tmp_path), "start", "end"])

    assert analyze.main() == 0

    report = capsys.readouterr().out
    assert "尾窗 idle：未验证" in report
    assert "逐句归档结果未验证" in report


def test_archive_query_is_limited_to_the_device_session(monkeypatch) -> None:
    captured = {}

    def run(_command, *, input, **_kwargs):
        captured["sql"] = input
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    import subprocess

    monkeypatch.setattr(analyze.subprocess, "run", run)
    analyze.archive_rows("2026-10-10 19:48:25", "2026-10-10 19:50:30", "c5ba6569-54ff-4783-8a78-28628fe85052")

    assert "session_id = 'c5ba6569-54ff-4783-8a78-28628fe85052'" in captured["sql"]
