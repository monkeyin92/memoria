"""Tests for the P2-05 wake-word matrix harness (offline, no hardware).

The public seams under test are the window gate/exposure accounting, the
wake-event dedup rule, the invalid (never miss/never false-wake) outcomes and
the report rendering.  Everything runs against an unstarted ConsoleReader fed
with hand-built lines, so no serial port, speaker or device is touched.  The
dedup replay pins the real receipt console.log lines 628 (wake event with
``(state:``) and 650 (lagging duplicate without it); the file is only read.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

import pytest
from scripts import wake_word_matrix as wwm

RECEIPT_CONSOLE = (
    Path(__file__).resolve().parents[2]
    / "outputs/acceptance/run-20260916-p2-05-wake-matrix-1/console.log"
)

# Minimal in-repo fixture: the real receipt's wake event (with ``(state:``) and
# its lagging duplicate (without it), plus the preceding detector line. Only
# these three lines are needed; the full device receipt stays out of the repo.
FIXTURE_DETECTOR_TEXT = (
    "I (264007) CustomWakeWord: Custom wake word detected: "
    "command_id=1, string= mo li, prob=0.335790"
)
FIXTURE_WAKE_EVENT_TEXT = (
    "I (264017) Application: Wake word detected: 茉莉 (state: 3)"
)
FIXTURE_LAGGING_DUPLICATE_TEXT = "I (266207) Application: Wake word detected: 茉莉"

WAKE_EVENT_TEXT = FIXTURE_WAKE_EVENT_TEXT
IDLE_TEXT = "I (260817) StateMachine: State: listening -> idle"
CONNECTING_TEXT = "I (264017) StateMachine: State: idle -> connecting"
DETECTOR_ON_TEXT = (
    "I (266187) MemoriaWakeWord: configured wake word id=mo_li command=mo li display=茉莉"
)

class FakePort:
    """A readline script: bytes are returned, Exceptions raised, then b"" forever."""

    def __init__(self, script: tuple[object, ...] = ()) -> None:
        self._script = list(script)

    def readline(self) -> bytes:
        if not self._script:
            return b""
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, bytes)
        return item


class FakeClock:
    """Manual monotonic clock used as the timestamp source for console lines."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePlayer:
    """Records play calls; optionally fails like a broken audio rig."""

    def __init__(self, duration_s: float = 0.05, fail: bool = False) -> None:
        self.calls: list[float] = []
        self.duration_s = duration_s
        self.fail = fail

    def __call__(self, *args: object, **kwargs: object) -> float:
        started = time.monotonic()
        self.calls.append(started)
        if self.fail:
            raise wwm.PlayFailed("fake playback failed")
        time.sleep(self.duration_s)
        return started


def _line(monotonic: float, text: str) -> wwm.ConsoleLine:
    return wwm.ConsoleLine(
        monotonic=monotonic, iso="2026-09-16T00:00:00.000+08:00", text=text
    )


def _reader(tmp_path: Path, lines: tuple[wwm.ConsoleLine, ...] = ()) -> wwm.ConsoleReader:
    reader = wwm.ConsoleReader(FakePort(), tmp_path / "console.log")
    for line in lines:
        reader.lines.append(line)
    return reader


def _stimulus() -> wwm.Stimulus:
    return wwm.Stimulus(
        label="wake",
        voice="Tingting",
        text="茉莉",
        path="/tmp/wake.wav",
        sha256="x",
        duration_s=1.0,
        rms_dbfs=-20.0,
        peak_dbfs=-6.0,
    )


def test_window_requires_idle_and_detector(tmp_path: Path) -> None:
    now = time.monotonic()
    idle_old = _line(now - 5.0, IDLE_TEXT)
    detector_on = _line(now - 4.0, DETECTOR_ON_TEXT)

    invalid: list[str] = []
    busy = _reader(tmp_path, (idle_old, detector_on, _line(now - 0.05, CONNECTING_TEXT)))
    window = wwm._run_window(
        busy,
        label="tv",
        gain=0.3,
        wall_seconds=0.3,
        idle_timeout_s=0.2,
        invalid=invalid,
        play_fn=FakePlayer(),
    )
    assert window.invalid_reason == "invalid_never_idle"
    assert window.wakes == [] and window.effective_exposure_s == 0.0
    assert any("tv" in reason for reason in invalid)

    invalid.clear()
    no_detector = _reader(tmp_path, (idle_old,))
    window = wwm._run_window(
        no_detector,
        label="tv",
        gain=0.3,
        wall_seconds=0.3,
        idle_timeout_s=0.5,
        invalid=invalid,
        play_fn=FakePlayer(),
    )
    assert window.invalid_reason == "invalid_detector_off"
    assert window.wakes == []

    invalid.clear()
    ready = _reader(tmp_path, (idle_old, detector_on))
    window = wwm._run_window(
        ready,
        label="tv",
        gain=0.3,
        wall_seconds=0.4,
        idle_timeout_s=1.0,
        invalid=invalid,
        play_fn=FakePlayer(),
        poll_s=0.05,
    )
    assert window.invalid_reason is None
    assert window.effective_exposure_s > 0
    assert window.detector_on_evidence == DETECTOR_ON_TEXT
    assert invalid == []


def test_window_pauses_while_away_from_idle(tmp_path: Path) -> None:
    now = time.monotonic()
    reader = _reader(tmp_path, (_line(now - 5.0, IDLE_TEXT), _line(now - 4.0, DETECTOR_ON_TEXT)))
    invalid: list[str] = []

    def play_once() -> float:
        reader.lines.append(_line(time.monotonic(), CONNECTING_TEXT))
        return time.monotonic()

    window = wwm._run_window(
        reader,
        label="tv",
        gain=0.3,
        wall_seconds=0.8,
        idle_timeout_s=1.0,
        invalid=invalid,
        play_fn=play_once,
        poll_s=0.05,
    )
    assert window.invalid_reason is None
    wall = window.finished_monotonic - window.started_monotonic
    assert window.effective_exposure_s < wall
    assert window.paused
    assert all(set(entry) == {"from", "to", "reason"} for entry in window.paused)
    assert any("connecting" in entry["reason"] for entry in window.paused)


def test_window_starts_exposure_only_after_settle_gate_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-05: settle waiting is entry cost, never TV exposure.

    Fake clock: a busy->idle transition at t=2000.5 needs a 1.5 s settle wait,
    so the gate passes at t=2002.0 and the 1.0 s exposure window covers only
    post-gate idle. Pre-fix, ``window_start`` is the pre-gate call time and
    the pre-gate idle/settle slice inflates exposure. Post-fix the pre-gate
    slices earn nothing: playbacks only run post-gate and exposure is ~1.0 s.
    """
    clock = FakeClock(start=2000.0)
    monkeypatch.setattr(wwm.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(wwm.time, "sleep", clock.sleep)
    reader = _reader(
        tmp_path,
        (
            _line(1999.0, CONNECTING_TEXT),
            _line(2000.5, IDLE_TEXT),
            _line(2000.5, DETECTOR_ON_TEXT),
        ),
    )
    player_calls: list[float] = []

    def play_once() -> float:
        player_calls.append(clock.monotonic())
        clock.sleep(0.05)
        return clock.monotonic()

    invalid: list[str] = []
    window = wwm._run_window(
        reader,
        label="tv",
        gain=0.3,
        wall_seconds=1.0,
        idle_timeout_s=5.0,
        invalid=invalid,
        play_fn=play_once,
        poll_s=0.05,
    )
    assert window.invalid_reason is None
    assert player_calls, "gate passed: playback ran post-gate"
    assert all(call >= 2002.0 - 1e-6 for call in player_calls)
    # Pre-gate slices (1999-2002, busy then settling) are entry cost: post-fix
    # exposure is ~1.0 s; pre-fix it counted ~2.5 s (all fake-idle or settled).
    assert window.effective_exposure_s <= 1.1
    assert window.effective_exposure_s >= 0.5


def test_window_excludes_settle_period_wake_from_numerator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-05: a wake during settle is entry cost, never a counted false wake.

    Fake clock: busy->idle at t=2000.5, gate passes at t=2002.0; a wake event
    at t=2001.0 (inside the settle wait) must not appear in ``wakes``. The
    receipt also carries ``exposure_started_monotonic`` so the report wall
    excludes the gate wait. Fails while ``_wake_lines`` starts at the
    pre-gate call time.
    """
    clock = FakeClock(start=2000.0)
    monkeypatch.setattr(wwm.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(wwm.time, "sleep", clock.sleep)
    reader = _reader(
        tmp_path,
        (
            _line(1999.0, CONNECTING_TEXT),
            _line(2000.5, IDLE_TEXT),
            _line(2000.5, DETECTOR_ON_TEXT),
            _line(2001.0, FIXTURE_WAKE_EVENT_TEXT),
        ),
    )
    player_calls: list[float] = []

    def play_once() -> float:
        player_calls.append(clock.monotonic())
        clock.sleep(0.05)
        return clock.monotonic()

    invalid: list[str] = []
    window = wwm._run_window(
        reader,
        label="tv",
        gain=0.3,
        wall_seconds=1.0,
        idle_timeout_s=5.0,
        invalid=invalid,
        play_fn=play_once,
        poll_s=0.05,
    )
    assert window.invalid_reason is None
    assert window.wakes == []
    assert window.exposure_started_monotonic == pytest.approx(2002.0)
    assert window.started_monotonic == pytest.approx(2000.0)
    report = wwm._build_report({"windows": [asdict(window)], "trials": []})
    line = next(
        entry for entry in report.splitlines() if entry.startswith("- tv：")
    )
    assert "0 次" in line
    assert "有效曝光 1.1s/1.0s" in line


def test_window_exposure_excludes_pre_gate_idle_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-05: pre-gate idle history is not stimulus exposure either.

    The gate may pass instantly on a long-settled idle line; exposure must
    still start at gate passage, so hours of pre-call idle history cannot
    inflate one short window. A fake clock pins the gate delay and the
    exposure slice apart.
    """
    clock = FakeClock(start=2000.0)
    monkeypatch.setattr(wwm.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(wwm.time, "sleep", clock.sleep)
    reader = _reader(
        tmp_path,
        (
            _line(1000.0, IDLE_TEXT),
            _line(1000.0, DETECTOR_ON_TEXT),
            _line(2000.0, CONNECTING_TEXT),
            _line(2000.5, IDLE_TEXT),
        ),
    )
    player_calls: list[float] = []

    def play_once() -> float:
        player_calls.append(clock.monotonic())
        clock.sleep(0.05)
        return clock.monotonic()

    invalid: list[str] = []
    window = wwm._run_window(
        reader,
        label="tv",
        gain=0.3,
        wall_seconds=1.0,
        idle_timeout_s=5.0,
        invalid=invalid,
        play_fn=play_once,
        poll_s=0.05,
    )
    assert window.invalid_reason is None
    assert player_calls, "gate passed: playback ran inside the exposure window"
    # Pre-gate busy slice (2000.0-2000.5) and the settle wait are entry cost:
    # post-fix exposure is ~1.0 s, pre-fix it would be ~2.5 s.
    assert window.effective_exposure_s <= 1.1
    assert window.effective_exposure_s >= 0.5
    assert all(entry["from"] >= 2002.0 - 1e-6 for entry in window.paused)


def test_offline_replay_dedups_lagging_duplicate(tmp_path: Path) -> None:
    if RECEIPT_CONSOLE.exists():
        raw = RECEIPT_CONSOLE.read_text(encoding="utf-8").splitlines()
        event_text = raw[627].split("] ", 1)[1]
        duplicate_text = raw[649].split("] ", 1)[1]
        detector_text = raw[626].split("] ", 1)[1]
        assert event_text == FIXTURE_WAKE_EVENT_TEXT
        assert duplicate_text == FIXTURE_LAGGING_DUPLICATE_TEXT
        assert detector_text == FIXTURE_DETECTOR_TEXT
    else:
        event_text = FIXTURE_WAKE_EVENT_TEXT
        duplicate_text = FIXTURE_LAGGING_DUPLICATE_TEXT
        detector_text = FIXTURE_DETECTOR_TEXT
    assert wwm._is_wake_event(event_text)
    assert not wwm._is_wake_event(duplicate_text)

    clock = FakeClock(start=1000.0)
    reader = _reader(
        tmp_path,
        (
            _line(clock.now, event_text),
            _line(clock.now + 2.2, duplicate_text),
        ),
    )
    replayed = wwm._wake_lines(reader, since=clock.now - 1.0, until=clock.now + 10.0)
    assert len(replayed) == 1  # the pair is one wake, not two
    duplicate_only = wwm._wake_lines(reader, since=clock.now + 1.0, until=clock.now + 10.0)
    assert duplicate_only == []  # the lagging duplicate alone counts for 0
    wide = sum(1 for line in reader.snapshot() if wwm.WAKE_MARKER in line.text)
    assert wide == 2  # documents why the wide marker must not be used for trials
    prob_reader = _reader(
        tmp_path,
        (
            _line(clock.now, detector_text),
            _line(clock.now + 0.1, event_text),
            _line(clock.now + 5.0, detector_text),
        ),
    )
    prob = wwm._detector_prob_before(
        prob_reader, since=clock.now - 1.0, until=clock.now + 0.1
    )
    assert prob == pytest.approx(0.33579)  # started..wake only; the later line is out


def test_window_boundaries_are_half_open(tmp_path: Path) -> None:
    clock = FakeClock(start=500.0)
    reader = _reader(
        tmp_path,
        (
            _line(clock.now, WAKE_EVENT_TEXT),
            _line(clock.now + 10.0, WAKE_EVENT_TEXT),
        ),
    )
    left = wwm._wake_lines(reader, since=clock.now, until=clock.now + 10.0)
    right = wwm._wake_lines(reader, since=clock.now + 10.0, until=clock.now + 20.0)
    assert [entry["monotonic"] for entry in left] == [clock.now]
    assert [entry["monotonic"] for entry in right] == [clock.now + 10.0]


def test_serial_gap_and_play_failure_are_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.monotonic()
    seed = (_line(now - 5.0, IDLE_TEXT), _line(now - 4.0, DETECTOR_ON_TEXT))

    trials: list[wwm.Trial] = []
    monkeypatch.setattr(wwm, "_play", FakePlayer(fail=True))
    failed = wwm._run_trial(
        _reader(tmp_path, seed),
        _stimulus(),
        phase="recall_near",
        gain=1.0,
        wake_timeout_s=0.2,
        idle_timeout_s=1.0,
        trials=trials,
    )
    assert failed is not None and failed.outcome == "invalid_play_failed"
    assert failed.notes and trials == [failed]

    reader = _reader(tmp_path, seed)
    real_snapshot = reader.snapshot
    calls = {"n": 0}

    def flaky_snapshot(since: float | None = None) -> list[wwm.ConsoleLine]:
        calls["n"] += 1
        if calls["n"] == 2:
            reader.read_errors += 1
        return real_snapshot(since)

    monkeypatch.setattr(reader, "snapshot", flaky_snapshot)
    monkeypatch.setattr(wwm, "_play", FakePlayer())
    gapped_trials: list[wwm.Trial] = []
    gapped = wwm._run_trial(
        reader,
        _stimulus(),
        phase="recall_near",
        gain=1.0,
        wake_timeout_s=0.2,
        idle_timeout_s=1.0,
        trials=gapped_trials,
    )
    assert gapped is not None and gapped.outcome == "invalid_serial_gap"
    assert "miss" not in (failed.outcome, gapped.outcome)


def test_restore_reports_unrestored_and_marks_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    before = "output muted:false, output volume:50"
    still_muted = "output muted:true, output volume:50"

    class _Failed:
        returncode = 1

    class _Ok:
        returncode = 0

    monkeypatch.setattr(wwm.subprocess, "run", lambda *args, **kwargs: _Failed())
    assert wwm._restore_output_settings(before) is False

    monkeypatch.setattr(wwm.subprocess, "run", lambda *args, **kwargs: _Ok())
    assert wwm._restore_output_settings(before) is True
    assert wwm._settings_match(before, still_muted) is False

    monkeypatch.setattr(wwm, "_output_settings", lambda: still_muted)
    invalid: list[str] = []
    restored, after = wwm._finalize_output_restore(before, invalid)
    assert restored is False and after == still_muted
    assert len(invalid) == 1 and "run marked invalid" in invalid[0]

    monkeypatch.setattr(wwm, "_output_settings", lambda: before)
    clean: list[str] = []
    assert wwm._finalize_output_restore(before, clean) == (True, before)
    assert clean == []


def test_human_report_uses_speech_onset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.monotonic()
    reader = _reader(tmp_path, (_line(now - 5.0, IDLE_TEXT), _line(now - 4.0, DETECTOR_ON_TEXT)))
    reader.lines.append(_line(time.monotonic() + 0.5, WAKE_EVENT_TEXT))

    def fake_input(prompt: str = "") -> str:
        time.sleep(0.2)
        return ""

    monkeypatch.setattr("builtins.input", fake_input)
    trials: list[wwm.Trial] = []
    trial = wwm._run_trial(
        reader,
        _stimulus(),
        phase="recall_near",
        gain=1.0,
        wake_timeout_s=5.0,
        idle_timeout_s=1.0,
        trials=trials,
        human_speaker=True,
        human_confirm=True,
    )
    assert trial is not None and trial.outcome == "wake"
    assert trial.latency_speech_onset is not None and trial.latency_prompt is not None
    assert trial.latency_speech_onset == pytest.approx(0.3, abs=0.2)
    assert trial.latency_prompt == pytest.approx(0.5, abs=0.2)
    assert trial.latency_speech_onset < trial.latency_prompt

    receipt = {
        "created_at": "2026-09-17T00:00:00+08:00",
        "stimulus": {"human_speaker": True, "files": {"wake": {"voice": "operator"}}},
        "protocol": {"human_confirm": True},
        "input_range": {
            "system_output_settings_before_run": "b",
            "system_output_settings_during_run": "d",
            "system_output_restored_after_run": True,
            "gain_near": 1.0,
            "gain_far": 0.3,
            "ambient_floor_dbfs_at_mic": -60.0,
            "ambient_peak_dbfs_at_mic": -40.0,
            "levels_at_mic_dbfs": {},
        },
        "counts": {
            "recall_near": {"trials": 1, "wakes": 1, "misses": 0, "invalid": 0},
            "recall_far": {"trials": 0, "wakes": 0, "misses": 0, "invalid": 0},
        },
        "windows": [],
        "trials": [asdict(trial)],
        "invalid_reasons": [],
    }
    report = wwm._build_report(receipt)
    assert "真人说话者=是" in report
    assert "开口起算" in report


def test_exposure_ignores_busy_middle_between_idle_endpoints() -> None:
    """P2-05: 1s idle, 8s connecting, 1s idle earns 2s exposure, not 10s."""
    timeline = [
        {"monotonic": 1000.0, "iso": "", "text": "", "from": "activating", "to": "idle"},
        {"monotonic": 1001.0, "iso": "", "text": "", "from": "idle", "to": "connecting"},
        {"monotonic": 1009.0, "iso": "", "text": "", "from": "connecting", "to": "idle"},
    ]
    exposure, paused = wwm._exposure_over_timeline(
        timeline, window_start=1000.0, window_end=1010.0
    )
    assert exposure == pytest.approx(2.0)
    assert sum(entry["to"] - entry["from"] for entry in paused) == pytest.approx(8.0)
    assert any("connecting" in entry["reason"] for entry in paused)


def test_exposure_splits_at_window_edges_and_handles_zero_exposure() -> None:
    """Cross-window transitions clip; an all-busy window earns zero exposure."""
    timeline = [
        {"monotonic": 1000.0, "iso": "", "text": "", "from": "activating", "to": "idle"},
        {"monotonic": 1005.0, "iso": "", "text": "", "from": "idle", "to": "speaking"},
        {"monotonic": 1015.0, "iso": "", "text": "", "from": "speaking", "to": "idle"},
    ]
    left, _ = wwm._exposure_over_timeline(timeline, window_start=1000.0, window_end=1010.0)
    right, _ = wwm._exposure_over_timeline(timeline, window_start=1010.0, window_end=1020.0)
    assert left == pytest.approx(5.0)
    assert right == pytest.approx(5.0)
    busy, paused = wwm._exposure_over_timeline(
        timeline, window_start=1006.0, window_end=1014.0
    )
    assert busy == pytest.approx(0.0)
    assert len(paused) == 1 and "speaking" in paused[0]["reason"]
