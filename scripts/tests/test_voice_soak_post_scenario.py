from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

BASE = 1_794_000_000.0
DRIVERS = (
    "scripts.voice_soak",
    "devicetest.voice_soak_tapwake",
)


class FakeClock:
    def __init__(self, transitions: list[tuple[float, str]] | None = None) -> None:
        self.wall = BASE
        self.monotonic = 0.0
        self.transitions = transitions or []
        self.watcher = None
        self.applied: set[float] = set()

    def advance(self, seconds: float) -> None:
        target = self.monotonic + seconds
        for at, line in self.transitions:
            if self.monotonic < at <= target and at not in self.applied:
                self.monotonic = at
                self.wall = BASE + at
                self.watcher._parse(self.wall, line)
                self.applied.add(at)
        self.monotonic = target
        self.wall = BASE + target


class FakeCondition:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def wait(self, timeout: float) -> None:
        self.clock.advance(timeout)

    def notify_all(self) -> None:
        pass


@pytest.fixture(params=DRIVERS, ids=("service-driver", "device-driver"))
def driver(request, monkeypatch):
    monkeypatch.setitem(sys.modules, "serial", types.ModuleType("serial"))
    return importlib.import_module(request.param)


def make_watcher(driver, monkeypatch, clock: FakeClock, state: str, state_since: float, source: str):
    monkeypatch.setattr(driver, "now", lambda: clock.wall)
    monkeypatch.setattr(driver.time, "monotonic", lambda: clock.monotonic)
    watcher = driver.SerialWatcher(Path("serial.log"))
    watcher.state = state
    watcher.state_since = state_since
    watcher.state_source = source
    watcher.cond = FakeCondition(clock)
    clock.watcher = watcher
    return watcher


def test_window_starts_after_speaking_settles_and_catches_late_idle(
    driver, monkeypatch
) -> None:
    clock = FakeClock(
        [
            (10.0, "StateMachine: State: speaking -> listening"),
            (190.0, "StateMachine: State: listening -> idle"),
        ]
    )
    watcher = make_watcher(driver, monkeypatch, clock, "speaking", BASE, "serial_transition")

    result = watcher.observe_post_scenario(180.0)

    assert result["settled_s"] == 10.0
    assert result["waited_s"] == 180.0
    assert result["final_state"] == "idle"
    assert result["final_state_source"] == "serial_transition"
    assert result["idle_observed"] is True
    assert result["verdict"] == "verified_idle"
    assert clock.monotonic == 190.0


def test_idle_transition_before_window_is_not_counted(driver, monkeypatch) -> None:
    clock = FakeClock()
    watcher = make_watcher(
        driver, monkeypatch, clock, "idle", BASE - 1, "serial_transition"
    )

    result = watcher.observe_post_scenario(5.0)

    assert result["idle_observed"] is False
    assert result["final_state"] == "idle"
    assert result["final_state_source"] == "serial_transition"
    assert result["verdict"] == "evidence_insufficient"


def test_screen_off_idle_note_does_not_create_idle_state(driver, monkeypatch) -> None:
    clock = FakeClock()
    watcher = make_watcher(
        driver, monkeypatch, clock, "listening", BASE - 1, "serial_transition"
    )

    watcher._parse(BASE, "MemoriaMascot: screen off (idle)")
    result = watcher.observe_post_scenario(5.0)

    assert watcher.state == "listening"
    assert watcher.state_source == "serial_transition"
    assert result["idle_observed"] is False
    assert result["verdict"] == "timeout"


def test_post_scenario_window_returns_timeout_without_idle(driver, monkeypatch) -> None:
    clock = FakeClock()
    watcher = make_watcher(
        driver, monkeypatch, clock, "listening", BASE - 1, "serial_transition"
    )

    result = watcher.observe_post_scenario(5.0)

    assert result["idle_observed"] is False
    assert result["final_state"] == "listening"
    assert result["verdict"] == "timeout"
    assert result["timed_out"] is True
    assert result["waited_s"] == 5.0
