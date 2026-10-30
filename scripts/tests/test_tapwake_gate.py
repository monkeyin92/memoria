"""Turn gate and serial hold state, offline: no serial port, no robot.

Soak run-20261030-134232 spoke into a closed mic because the turn gate answered `listening` while the
serial said `listening_closed closed=true`, and `ensure_listening` never saw a closed bridge.log line.
The gate now takes the serial hold flag directly; these tests pin both readings and the bridge fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from devicetest.tapwake_gate import wait_gate
from devicetest.voice_soak_tapwake import SerialWatcher


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
    # The failing soak: bridge.log carries an old listening_closed line, the serial says the mic is open.
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
def watcher() -> SerialWatcher:
    return SerialWatcher(path=Path("/dev/null"))


def test_serial_closed_true_sets_mic_closed(watcher: SerialWatcher) -> None:
    watcher._parse(1.0, "09:54:02.438 I (79630) MemoriaProtocol: listening_closed closed=true")
    assert watcher.mic_closed is True


def test_serial_closed_false_clears_mic_closed(watcher: SerialWatcher) -> None:
    watcher._parse(1.0, "09:54:02.438 I (79630) MemoriaProtocol: listening_closed closed=true")
    watcher._parse(2.0, "09:54:06.316 I (83420) MemoriaProtocol: listening_closed closed=false")
    assert watcher.mic_closed is False


def test_serial_idle_transition_clears_mic_closed(watcher: SerialWatcher) -> None:
    watcher._parse(1.0, "09:54:02.438 I (79630) MemoriaProtocol: listening_closed closed=true")
    watcher._parse(2.0, "09:54:10.000 I (80000) StateMachine: State: listening -> idle")
    assert watcher.mic_closed is False


def test_serial_resume_listening_clears_mic_closed(watcher: SerialWatcher) -> None:
    watcher._parse(1.0, "09:54:02.438 I (79630) MemoriaProtocol: listening_closed closed=true")
    watcher._parse(2.0, "09:54:06.316 I (83390) MemoriaUsbCommand: usb wake action=resume_listening")
    assert watcher.mic_closed is False
