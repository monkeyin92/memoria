"""Turn gate and serial hold state, offline: no serial port, no robot.

Soak run-20261030-134232 spoke into a shut mic from the first `closed=true` at 13:44:35 on: the turn gate
answered `listening` whenever the StateMachine said listening, and `ensure_listening` never saw the closed
bridge.log line. The gate now takes the serial hold flag directly; these tests pin both readings, the
last-line-wins parse, and the bridge fallback.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest
from devicetest.tapwake_gate import wait_gate


def fake_clock() -> tuple:
    now = [1000.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return clock, sleep, now


def bridge_with(tmp_path: Path, text: str) -> Path:
    bridge = tmp_path / "bridge.log"
    bridge.write_text(text, encoding="utf-8")
    return bridge


def test_turn_gate_reports_listening_when_serial_mic_is_open(tmp_path: Path) -> None:
    clock, sleep, _ = fake_clock()
    bridge = bridge_with(tmp_path, "")
    got = wait_gate(lambda: "listening", bridge, 0, 5.0, mode="turn",
                    clock=clock, sleep=sleep, is_closed=lambda: False)
    assert got == "listening"


def test_turn_gate_reports_listening_closed_when_serial_mic_is_shut(tmp_path: Path) -> None:
    clock, sleep, _ = fake_clock()
    bridge = bridge_with(tmp_path, "")
    got = wait_gate(lambda: "listening", bridge, 0, 5.0, mode="turn",
                    clock=clock, sleep=sleep, is_closed=lambda: True)
    assert got == "listening_closed"


def test_turn_gate_serial_open_beats_a_stale_closed_bridge_line(tmp_path: Path) -> None:
    # Precedence guard: a leftover bridge.log line must not close a window the serial reports open.
    clock, sleep, _ = fake_clock()
    bridge = bridge_with(tmp_path, "2026-10-30T05:43:00Z INFO:listening_closed\n")
    got = wait_gate(lambda: "listening", bridge, 0, 5.0, mode="turn",
                    clock=clock, sleep=sleep, is_closed=lambda: False)
    assert got == "listening"


def test_turn_gate_falls_back_to_bridge_when_no_serial_reader(tmp_path: Path) -> None:
    clock, sleep, _ = fake_clock()
    bridge = bridge_with(tmp_path, "2026-10-30T05:43:00Z INFO:listening_closed\n")
    got = wait_gate(lambda: "listening", bridge, 0, 5.0, mode="turn", clock=clock, sleep=sleep)
    assert got == "listening_closed"


def test_turn_gate_waits_while_not_listening(tmp_path: Path) -> None:
    clock, sleep, now = fake_clock()
    bridge = bridge_with(tmp_path, "")
    got = wait_gate(lambda: "speaking", bridge, 0, 1.0, mode="turn",
                    clock=clock, sleep=sleep, poll=0.5, is_closed=lambda: True)
    assert got is None
    assert now[0] >= 1001.0


def test_idle_wins_over_a_shut_mic(tmp_path: Path) -> None:
    clock, sleep, _ = fake_clock()
    bridge = bridge_with(tmp_path, "")
    got = wait_gate(lambda: "idle", bridge, 0, 5.0, mode="turn",
                    clock=clock, sleep=sleep, is_closed=lambda: True)
    assert got == "idle"


def test_session_gate_ignores_a_listening_window(tmp_path: Path) -> None:
    clock, sleep, _ = fake_clock()
    bridge = bridge_with(tmp_path, "")
    got = wait_gate(lambda: "listening", bridge, 0, 1.0, mode="session",
                    clock=clock, sleep=sleep, poll=0.5, is_closed=lambda: False)
    assert got is None


@pytest.fixture
def watcher(monkeypatch):
    # pyserial is a device-only extra, not a dependency of these offline tests.
    monkeypatch.setitem(sys.modules, "serial", types.ModuleType("serial"))
    module = importlib.import_module("devicetest.voice_soak_tapwake")
    return module.SerialWatcher(path=Path("/dev/null"))


CLOSED_TRUE = "13:44:35.069 I (326320) MemoriaProtocol: listening_closed closed=true"
CLOSED_FALSE = "13:44:08.560 I (299680) MemoriaProtocol: listening_closed closed=false"


def test_failing_soak_window_opens_as_closed_with_a_lagging_bridge(tmp_path: Path, watcher) -> None:
    # Run-20261030-134232: serial listening + closed=true, bridge.log had not caught up.
    watcher._parse(1.0, "13:43:49.401 I (280580) StateMachine: State: speaking -> listening")
    watcher._parse(2.0, CLOSED_TRUE)
    clock, sleep, _ = fake_clock()
    bridge = bridge_with(tmp_path, "")
    got = wait_gate(lambda: watcher.state, bridge, 0, 5.0, mode="turn",
                    clock=clock, sleep=sleep, is_closed=lambda: watcher.mic_closed)
    assert got == "listening_closed"
    assert watcher.state == "listening"


def test_serial_closed_true_sets_mic_closed(watcher) -> None:
    watcher._parse(1.0, CLOSED_TRUE)
    assert watcher.mic_closed is True


def test_serial_closed_false_clears_mic_closed(watcher) -> None:
    watcher._parse(1.0, CLOSED_TRUE)
    watcher._parse(2.0, CLOSED_FALSE)
    assert watcher.mic_closed is False


def test_last_closed_line_wins_when_both_land_in_the_same_millisecond(watcher) -> None:
    # Run-20261030-134232 lines 118-119: closed=false then closed=true at 13:44:35.069.
    watcher._parse(1.0, "13:44:35.069 I (326310) MemoriaProtocol: listening_closed closed=false")
    watcher._parse(1.001, "13:44:35.069 I (326320) MemoriaProtocol: listening_closed closed=true")
    assert watcher.mic_closed is True


def test_serial_idle_transition_clears_mic_closed(watcher) -> None:
    watcher._parse(1.0, CLOSED_TRUE)
    watcher._parse(2.0, "09:54:10.000 I (80000) StateMachine: State: listening -> idle")
    assert watcher.mic_closed is False


def test_serial_resume_listening_clears_mic_closed(watcher) -> None:
    watcher._parse(1.0, CLOSED_TRUE)
    watcher._parse(2.0, "09:54:06.316 I (83390) MemoriaUsbCommand: usb wake action=resume_listening")
    assert watcher.mic_closed is False
