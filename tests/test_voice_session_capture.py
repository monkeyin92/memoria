"""Tests for the receipt-bound voice session capture tool.

No serial port is ever opened here: the preflight path validates the receipt and
writes capture.json, and the live path runs against fake serial/process/handle objects
only, including a `sys.modules` entry that makes `import serial` raise, so the
dependency refusal can be tested without any hardware.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import types
from pathlib import Path

import pytest
from scripts import voice_session_capture as capture

RELEASE_HEAD = "fa54d7de027cccaee35e1721762a5d0bb060d60c"
APP_SHA = "f58f48a4b21df96df74750ed10638c2ca906f267f2228cf37add3e37fd4a9101"
ELF_SHA = "16d981b31f45d0dbdaadc6d2ab8f19ab217c1f09ce96535ea0eea54c0cffa578"
IDENTITY_SHA = "b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846"


def _receipt(**overrides: object) -> dict[str, object]:
    receipt: dict[str, object] = {
        "started_at": "2026-09-14T19:06:59+08:00",
        "release_head": RELEASE_HEAD,
        "candidate_app_sha256": APP_SHA,
        "candidate_elf_sha256": ELF_SHA,
        "identity_sha256": IDENTITY_SHA,
        "write_offset": "0x20000",
        "verified_at": "2026-09-14T19:08:51.640084+08:00",
        "app_full_readback_byte_match": True,
        "identity_byte_match": True,
    }
    receipt.update(overrides)
    return receipt


def _receipt_file(root: Path, name: str = "postflash.json", **overrides: object) -> Path:
    path = root / name
    path.write_text(json.dumps(_receipt(**overrides)), encoding="utf-8")
    return path


def test_preflight_binds_the_receipt_and_keeps_source_revision_separate(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    revision = {"git_head": "0" * 40, "git_branch": "codex/test", "dirty": True}
    monkeypatch.setattr(capture, "source_revision", lambda: dict(revision))
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "session-1"

    code = capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--preflight-only"])
    printed = capsys.readouterr().out
    assert code == 0
    assert "WARNING: opening the serial port asserts DTR/RTS" in printed
    assert "PREFLIGHT_ONLY serial_opened=False" in printed
    assert RELEASE_HEAD in printed

    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["capture_mode"] == "preflight_only"
    assert metadata["serial_opened"] is False
    assert metadata["source_revision"] == revision
    assert metadata["firmware_receipt"]["release_head"] == RELEASE_HEAD
    assert metadata["firmware_receipt"]["candidate_app_sha256"] == APP_SHA
    assert metadata["firmware_receipt"]["identity_sha256"] == IDENTITY_SHA
    assert (
        metadata["firmware_receipt"]["sha256"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    )
    assert metadata["firmware_receipt"]["path"] == str(receipt.resolve())
    assert metadata["firmware_receipt"]["raw"] == _receipt()
    assert metadata["firmware_receipt"]["read_from_board_this_run"] is False
    assert "candidate_app_sha256" not in metadata["source_revision"]
    assert metadata["warnings"]


def test_source_revision_reports_the_working_tree() -> None:
    revision = capture.source_revision()
    assert set(revision) == {"git_head", "git_branch", "dirty"}
    assert isinstance(revision["dirty"], bool)


def test_receipt_is_required(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        capture.main(["--out", str(tmp_path / "session-1"), "--preflight-only"])
    assert error.value.code == 2
    assert not (tmp_path / "session-1").exists()


def test_missing_receipt_path_fails_without_creating_out(tmp_path: Path, capsys) -> None:
    out = tmp_path / "session-1"
    code = capture.main(
        [
            "--out",
            str(out),
            "--firmware-receipt",
            str(tmp_path / "nope.json"),
            "--preflight-only",
        ]
    )
    assert code == 2
    assert "does not exist" in capsys.readouterr().err
    assert not out.exists()


def test_unreadable_receipt_fails_closed(tmp_path: Path, capsys) -> None:
    array = tmp_path / "postflash.json"
    array.write_text("[1, 2]", encoding="utf-8")
    out = tmp_path / "session-1"
    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(array), "--preflight-only"]) == 2
    )
    assert "must be a JSON object" in capsys.readouterr().err

    garbage = tmp_path / "broken.json"
    garbage.write_text("{not json", encoding="utf-8")
    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(garbage), "--preflight-only"])
        == 2
    )
    assert "not valid JSON" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"release_head": "not-a-digest"}, "release_head"),
        ({"candidate_app_sha256": "f" * 63}, "candidate_app_sha256"),
        ({"candidate_elf_sha256": APP_SHA.upper()}, "candidate_elf_sha256"),
        ({"identity_sha256": None}, "identity_sha256"),
        ({"app_full_readback_byte_match": False}, "app_full_readback_byte_match"),
        ({"identity_byte_match": None}, "identity_byte_match"),
        ({"verified_at": ""}, "verified_at"),
    ],
)
def test_invalid_receipts_fail_closed(tmp_path: Path, capsys, overrides, expected: str) -> None:
    receipt = _receipt_file(tmp_path, **overrides)
    out = tmp_path / "session-1"
    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--preflight-only"])
        == 2
    )
    assert expected in capsys.readouterr().err
    assert not out.exists()


def test_receipt_beside_the_working_directory_is_never_guessed(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _receipt_file(decoy, name="postflash.json", release_head="a" * 40)
    _receipt_file(decoy, name="preflash.json", release_head="b" * 40)
    chosen = _receipt_file(tmp_path, name="chosen.json")
    out = tmp_path / "session-1"
    monkeypatch.chdir(decoy)

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(chosen), "--preflight-only"])
        == 0
    )
    capsys.readouterr()
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["firmware_receipt"]["release_head"] == RELEASE_HEAD
    assert metadata["firmware_receipt"]["path"] == str(chosen.resolve())
    assert "a" * 40 not in json.dumps(metadata)


def test_existing_out_directory_is_never_overwritten(tmp_path: Path, capsys) -> None:
    out = tmp_path / "session-1"
    out.mkdir()
    sentinel = out / "capture.json"
    sentinel.write_text("sentinel", encoding="utf-8")
    receipt = _receipt_file(tmp_path)
    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--preflight-only"])
        == 2
    )
    assert "already exists" in capsys.readouterr().err
    assert sentinel.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.parametrize("duration", [0, -1, 901])
def test_duration_bounds_are_enforced(tmp_path: Path, capsys, duration: int) -> None:
    out = tmp_path / f"session-{duration}"
    receipt = _receipt_file(tmp_path)
    assert (
        capture.main(
            [
                "--out",
                str(out),
                "--firmware-receipt",
                str(receipt),
                "--duration",
                str(duration),
                "--preflight-only",
            ]
        )
        == 2
    )
    assert "--duration must be between 1 and 900 seconds" in capsys.readouterr().err
    assert not out.exists()


def test_missing_pyserial_is_refused_and_recorded_without_opening_a_port(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    # Deterministic whatever the environment ships: a None entry in sys.modules makes
    # `import serial` raise ImportError, and no real port can be involved.
    monkeypatch.setitem(sys.modules, "serial", None)
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    _no_git(monkeypatch)

    assert capture.main(["--out", str(out), "--firmware-receipt", str(receipt)]) == 2
    captured = capsys.readouterr()
    assert "needs pyserial" in captured.err
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["capture_mode"] == "live"
    assert metadata["serial_opened"] is False
    assert not (out / "serial.log").exists()
    # The refusal goes through the same finalization as any other ending: the record
    # says the capture never started instead of leaving an in_progress file.
    assert metadata["capture_status"] == "completed"
    assert metadata["completed_at_local"]
    assert metadata["exit_reason"] == "capture_not_started"
    assert metadata["cleanup_errors"] == []
    assert metadata["log_stream_health"] == "degraded"
    assert (
        "the live capture never started: pyserial is not importable"
        in metadata["log_stream_health_reasons"]
    )
    assert "CAPTURE_HEALTH degraded reasons=[" in captured.out


def test_missing_esptool_for_boot_reset_is_refused_and_recorded(tmp_path, capsys, monkeypatch):
    # pyserial is importable here (the fake module) while esptool is not, so --boot-reset
    # must be refused before anything is opened, and recorded the same way.
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    seen = _fake_serial(monkeypatch)
    monkeypatch.setitem(sys.modules, "esptool.reset", None)
    _no_git(monkeypatch)

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--boot-reset"]) == 2
    )
    captured = capsys.readouterr()
    assert "needs esptool" in captured.err
    assert seen["ports"] == []
    assert not (out / "serial.log").exists()
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["serial_opened"] is False
    assert metadata["capture_status"] == "completed"
    assert metadata["exit_reason"] == "capture_not_started"
    assert metadata["log_stream_health"] == "degraded"
    assert (
        "the live capture never started: --boot-reset needs esptool"
        in metadata["log_stream_health_reasons"]
    )


def _no_git(monkeypatch) -> None:
    monkeypatch.setattr(
        capture, "source_revision", lambda: {"git_head": "", "git_branch": "", "dirty": False}
    )


def _fast_clock(monkeypatch, step: float = 0.001) -> None:
    state = {"now": 0.0}

    def monotonic() -> float:
        state["now"] += step
        return state["now"]

    monkeypatch.setattr(capture.time, "monotonic", monotonic)


def _fake_serial(
    monkeypatch,
    chunks: list[bytes] | None = None,
    open_error: str = "",
    *,
    signal_number: int | None = None,
    close_error: str = "",
    readline_hook=None,
    close_hook=None,
) -> dict[str, list]:
    module = types.ModuleType("serial")
    seen: dict[str, list] = {"ports": []}

    class Port:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.port: str | None = None
            self.dtr = None
            self.rts = None
            self.is_open = False
            self.closed = False
            self.reads = 0
            self.close_attempts = 0
            seen["ports"].append(self)

        def open(self) -> None:
            if open_error:
                raise OSError(16, open_error)
            self.is_open = True

        def readline(self) -> bytes:
            index = self.reads
            self.reads += 1
            if readline_hook is not None:
                readline_hook(self, index)
            if signal_number is not None and index == 0:
                # A real signal to this process: the capture's own handler is already
                # installed at this point, so it must absorb it.
                os.kill(os.getpid(), signal_number)
            return chunks.pop(0) if chunks else b""

        def close(self) -> None:
            self.close_attempts += 1
            if close_error:
                raise OSError(5, close_error)
            self.closed = True
            if close_hook is not None:
                close_hook(self)

    module.Serial = Port
    monkeypatch.setitem(sys.modules, "serial", module)
    return seen


class _RunningProcess:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self._code: int | None = None

    @property
    def returncode(self) -> int | None:
        return self._code

    def poll(self) -> int | None:
        return self._code

    def terminate(self) -> None:
        self._code = -15

    def kill(self) -> None:
        self._code = -9

    def wait(self, timeout: float | None = None) -> int | None:
        return self._code


class _FailingProcess(_RunningProcess):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._code = 3


def test_metadata_write_failure_preserves_the_previous_complete_json(tmp_path, monkeypatch):
    capture.write_metadata(tmp_path, {"capture_status": "in_progress"})
    before = (tmp_path / "capture.json").read_bytes()
    real_write = Path.write_text

    def partial_write(path, text, *args, **kwargs):
        real_write(path, text[:9], *args, **kwargs)
        raise OSError(28, "disk full")

    monkeypatch.setattr(Path, "write_text", partial_write)
    with pytest.raises(OSError, match="disk full"):
        capture.write_metadata(tmp_path, {"capture_status": "completed"})
    assert (tmp_path / "capture.json").read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["capture.json"]


def test_metadata_replace_failure_preserves_record_and_removes_temporary(tmp_path, monkeypatch):
    capture.write_metadata(tmp_path, {"capture_status": "in_progress"})
    before = (tmp_path / "capture.json").read_bytes()

    def refused_replace(_source, _destination):
        raise OSError(13, "replace refused")

    monkeypatch.setattr(Path, "replace", refused_replace)
    with pytest.raises(OSError, match="replace refused"):
        capture.write_metadata(tmp_path, {"capture_status": "completed"})
    assert (tmp_path / "capture.json").read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["capture.json"]


@pytest.mark.parametrize("unknown_code", [None, False, True, "0", 0.0])
def test_unknown_exit_code_never_claims_success_and_does_not_skip_other_streams(unknown_code):
    class UnknownExit(_RunningProcess):
        def __init__(self):
            super().__init__()
            self.wait_timeouts = []
            self.killed = False

        def terminate(self):
            self._code = unknown_code

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            return self._code

    unknown = UnknownExit()
    processes = [("bridge", unknown), ("agent", _RunningProcess()), ("edge", _RunningProcess())]
    state = capture.Capture(
        processes=processes,
        streams={label: {"status": "running", "exit_code": None} for label, _ in processes},
    )
    capture._stop_log_processes(state)
    assert unknown.wait_timeouts == [capture.TERMINATE_WAIT_S, capture.KILL_WAIT_S]
    assert unknown.killed
    assert state.streams["bridge"]["status"] == "cleanup_failed"
    assert state.streams["bridge"]["exit_code"] is None
    assert state.streams["bridge"]["forced_kill"] is True
    assert any(
        "wait after SIGKILL for log stream bridge:" in error for error in state.cleanup_errors
    )
    for label in ("agent", "edge"):
        assert state.streams[label]["status"] == "stopped_by_capture"
        assert state.streams[label]["exit_code"] == -15
        assert state.streams[label]["forced_kill"] is False


def test_unreadable_returncode_does_not_skip_cleanup_of_the_next_stream():
    class UnreadableExit(_RunningProcess):
        @property
        def returncode(self):
            raise OSError(5, "returncode unavailable")

        def wait(self, timeout=None):
            return None

    processes = [("bridge", UnreadableExit()), ("agent", _RunningProcess())]
    state = capture.Capture(
        processes=processes,
        streams={label: {"status": "running", "exit_code": None} for label, _ in processes},
    )
    capture._stop_log_processes(state)
    assert state.streams["bridge"]["status"] == "cleanup_failed"
    assert state.streams["agent"]["status"] == "stopped_by_capture"
    assert any("returncode unavailable" in error for error in state.cleanup_errors)


@pytest.mark.parametrize("code_source", ["wait", "returncode"])
def test_confirmed_zero_exit_code_is_not_mistaken_for_failed_reap(code_source):
    class CleanExit(_RunningProcess):
        def wait(self, timeout=None):
            self._code = 0
            return 0 if code_source == "wait" else None

    state = capture.Capture(
        processes=[("bridge", CleanExit())],
        streams={"bridge": {"status": "running", "exit_code": None}},
    )
    capture._stop_log_processes(state)
    assert state.streams["bridge"]["status"] == "stopped_by_capture"
    assert state.streams["bridge"]["exit_code"] == 0
    assert state.streams["bridge"]["forced_kill"] is False
    assert not state.cleanup_errors


def test_poll_failure_during_cleanup_still_reaps_all_streams_and_writes_metadata(
    tmp_path, monkeypatch
):
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    draining = {"now": False}
    processes = []

    class PollFailure(_RunningProcess):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.waited = False
            processes.append(self)

        def poll(self):
            if draining["now"]:
                raise OSError(5, "poll unavailable")
            return super().poll()

        def wait(self, timeout=None):
            self.waited = True
            return super().wait(timeout=timeout)

    _fake_serial(monkeypatch, close_hook=lambda _port: draining.update(now=True))
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    monkeypatch.setattr(capture.subprocess, "Popen", PollFailure)
    assert (
        capture.main(
            [
                "--out",
                str(out),
                "--firmware-receipt",
                str(receipt),
                "--duration",
                "1",
                "--server-logs",
            ]
        )
        == 1
    )
    assert len(processes) == 3
    assert all(process.waited and process.returncode == -15 for process in processes)
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["capture_status"] == "completed"
    assert metadata["completed_at_local"]
    assert metadata["log_stream_health"] == "degraded"
    for label, _container in capture.CONTAINERS:
        assert any(f"polling log stream {label}:" in error for error in metadata["cleanup_errors"])


def test_receipt_digest_and_payload_come_from_a_single_read(tmp_path, capsys, monkeypatch) -> None:
    receipt = _receipt_file(tmp_path)
    raw = receipt.read_bytes()
    calls = {"n": 0}
    real_read_bytes = Path.read_bytes

    def counting(self: Path) -> bytes:
        if self.resolve() == receipt.resolve():
            calls["n"] += 1
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    _no_git(monkeypatch)
    out = tmp_path / "session-1"

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--preflight-only"])
        == 0
    )
    capsys.readouterr()
    metadata = json.loads((out / "capture.json").read_text())
    assert calls["n"] == 1
    assert metadata["firmware_receipt"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert metadata["firmware_receipt"]["size_bytes"] == len(raw)
    assert metadata["firmware_receipt"]["raw"] == _receipt()


def test_out_directory_oserror_exits_two_without_a_traceback(tmp_path, capsys, monkeypatch) -> None:
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "session-1"

    def boom(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", boom)
    _no_git(monkeypatch)

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--preflight-only"])
        == 2
    )
    err = capsys.readouterr().err
    assert "could not create --out" in err
    assert "Permission denied" in err


def test_live_capture_streams_serial_without_hardware(tmp_path, capsys, monkeypatch) -> None:
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    seen = _fake_serial(
        monkeypatch,
        chunks=[b"StateMachine: State: activating -> idle\r\n", b"SystemInfo: boot\r\n"],
    )
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--duration", "1"])
        == 0
    )
    printed = capsys.readouterr().out
    assert "SERIAL_OPEN pid=" in printed
    assert "SystemInfo" not in printed
    assert "CAPTURE_HEALTH healthy" in printed
    assert seen["ports"] and seen["ports"][0].closed
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["serial_opened"] is True
    assert metadata["log_stream_health"] == "not_requested"
    assert metadata["serial_error"] is None
    serial = (out / "serial.log").read_text()
    assert "activating -> idle" in serial


def test_live_server_log_streams_are_healthy_and_recorded(tmp_path, capsys, monkeypatch) -> None:
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    _fake_serial(monkeypatch)
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    monkeypatch.setattr(capture.subprocess, "Popen", _RunningProcess)

    assert (
        capture.main(
            [
                "--out",
                str(out),
                "--firmware-receipt",
                str(receipt),
                "--duration",
                "1",
                "--server-logs",
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "CAPTURE_HEALTH healthy" in printed
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["log_stream_health"] == "healthy"
    assert sorted(metadata["log_streams"]) == ["agent", "bridge", "edge"]
    assert metadata["log_streams"]["bridge"]["container"] == "memoria-voice-core-media-bridge-1"
    assert metadata["log_streams"]["bridge"]["status"] == "stopped_by_capture"
    for label in ("bridge", "agent", "edge"):
        assert (out / f"{label}.log").exists()


def test_a_failing_server_log_stream_marks_health_degraded_and_exits_nonzero(
    tmp_path, capsys, monkeypatch
) -> None:
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    _fake_serial(monkeypatch)
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    monkeypatch.setattr(capture.subprocess, "Popen", _FailingProcess)

    assert (
        capture.main(
            [
                "--out",
                str(out),
                "--firmware-receipt",
                str(receipt),
                "--duration",
                "1",
                "--server-logs",
            ]
        )
        == 1
    )
    printed = capsys.readouterr().out
    assert "LOG_STREAM_EXIT label=bridge exit=3" in printed
    assert "CAPTURE_HEALTH degraded reasons=[" in printed
    assert "log stream bridge failed (exit=3)" in printed
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["log_stream_health"] == "degraded"
    assert metadata["log_streams"]["bridge"]["status"] == "failed"
    assert metadata["log_streams"]["bridge"]["exit_code"] == 3


def test_serial_open_error_goes_to_stderr_and_degrades_health(tmp_path, capsys, monkeypatch):
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    _fake_serial(monkeypatch, open_error="Resource busy")
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--duration", "1"])
        == 1
    )
    captured = capsys.readouterr()
    assert "SERIAL_OPEN_ERROR error=OSError: [Errno 16] Resource busy" in captured.err
    assert "CAPTURE_HEALTH degraded reasons=[" in captured.out
    assert "serial OSError: [Errno 16] Resource busy" in captured.out
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["serial_opened"] is False
    assert metadata["serial_error"] == "OSError: [Errno 16] Resource busy"
    assert metadata["log_stream_health"] == "degraded"


def test_log_stream_start_failure_is_recorded_and_degrades_health(tmp_path, capsys, monkeypatch):
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    _fake_serial(monkeypatch)
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)

    def refuses(*_args: object, **_kwargs: object) -> None:
        raise OSError(2, "No such file or directory: ssh")

    monkeypatch.setattr(capture.subprocess, "Popen", refuses)

    assert (
        capture.main(
            [
                "--out",
                str(out),
                "--firmware-receipt",
                str(receipt),
                "--duration",
                "1",
                "--server-logs",
            ]
        )
        == 1
    )
    printed = capsys.readouterr().out
    assert "LOG_STREAM_START_FAILED label=bridge" in printed
    assert "CAPTURE_HEALTH degraded reasons=[" in printed
    assert (
        "log stream bridge start_failed (exit=None) error=FileNotFoundError: [Errno 2] "
        "No such file or directory: ssh" in printed
    )
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["log_stream_health"] == "degraded"
    assert metadata["log_streams"]["bridge"]["status"] == "start_failed"


class _StubbornProcess(_RunningProcess):
    """Ignores SIGTERM, so the capture has to escalate to SIGKILL."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._waits = 0

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int | None:
        self._waits += 1
        if self._waits == 1:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)
        self._code = -9
        return self._code


def test_stream_that_ignores_sigterm_is_flagged(tmp_path, capsys, monkeypatch):
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    _fake_serial(monkeypatch)
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    monkeypatch.setattr(capture.subprocess, "Popen", _StubbornProcess)

    assert (
        capture.main(
            [
                "--out",
                str(out),
                "--firmware-receipt",
                str(receipt),
                "--duration",
                "1",
                "--server-logs",
            ]
        )
        == 1
    )
    printed = capsys.readouterr().out
    assert "log stream bridge needed SIGKILL to be reclaimed (SIGTERM timed out)" in printed
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["log_stream_health"] == "degraded"
    assert metadata["log_streams"]["bridge"]["status"] == "stopped_by_capture"
    assert metadata["log_streams"]["bridge"]["forced_kill"] is True


def test_capture_json_stays_in_progress_until_the_finalization(tmp_path, capsys, monkeypatch):
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    writes: list[dict[str, object]] = []
    real_write = capture.write_metadata

    def recording(path: Path, metadata: dict[str, object]) -> None:
        writes.append(json.loads(json.dumps(metadata)))
        real_write(path, metadata)

    monkeypatch.setattr(capture, "write_metadata", recording)
    _fake_serial(monkeypatch, chunks=[b"SystemInfo: boot\r\n"])
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--duration", "1"])
        == 0
    )
    capsys.readouterr()
    assert len(writes) == 3
    first, opened, last = writes
    # The lifecycle record starts at the first metadata write and pre-writes nothing.
    assert first["capture_status"] == "in_progress"
    for field in ("completed_at_local", "exit_reason", "stop_signal", "cleanup_errors"):
        assert field not in first
    assert opened["serial_opened"] is True
    assert opened["capture_status"] == "in_progress"
    assert "completed_at_local" not in opened
    assert last["capture_status"] == "completed"
    assert last["completed_at_local"]
    assert last["exit_reason"] == "duration_elapsed"
    assert last["stop_signal"] is None
    assert last["cleanup_errors"] == []
    assert last["log_stream_health"] == "not_requested"


def test_preflight_writes_no_completion_or_stop_state(tmp_path, capsys, monkeypatch) -> None:
    _no_git(monkeypatch)
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "session-1"

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--preflight-only"])
        == 0
    )
    capsys.readouterr()
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["capture_status"] == "preflight_only"
    for field in ("completed_at_local", "exit_reason", "stop_signal", "cleanup_errors"):
        assert field not in metadata


STOP_SIGNALS = (signal.SIGHUP, signal.SIGQUIT, signal.SIGINT, signal.SIGTERM)


@pytest.mark.parametrize("signum", STOP_SIGNALS, ids=lambda number: number.name)
def test_every_stoppable_signal_is_handled_recorded_and_restored(
    tmp_path, capsys, monkeypatch, signum
) -> None:
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    before = {number: signal.getsignal(number) for number in STOP_SIGNALS}
    _fake_serial(
        monkeypatch,
        chunks=[b"StateMachine: State: activating -> idle\r\n"],
        signal_number=signum,
    )
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--duration", "900"])
        == 0
    )
    printed = capsys.readouterr().out
    assert f"CAPTURE_STOP_REQUESTED signal={signum.name}" in printed
    assert "CAPTURE_HEALTH healthy exit_reason=stop_signal " in printed

    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["capture_status"] == "completed"
    assert metadata["stop_signal"] == signum.name
    assert metadata["stop_signal_number"] == int(signum)
    assert metadata["stop_requested_at_local"]
    assert metadata["exit_reason"] == f"stop_signal {signum.name}"
    assert metadata["completed_at_local"]
    assert metadata["cleanup_errors"] == []
    assert metadata["log_stream_health"] == "not_requested"
    assert sorted(metadata["stop_signal_handlers"]) == ["SIGHUP", "SIGINT", "SIGQUIT", "SIGTERM"]
    # The caller's handlers are handed back: a capture never leaves its own behind.
    for number in STOP_SIGNALS:
        assert signal.getsignal(number) is before[number]


def test_a_stop_at_the_first_metadata_write_is_recorded_and_keeps_the_port_closed(
    tmp_path, capsys, monkeypatch
):
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    seen = _fake_serial(monkeypatch)
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    before = {number: signal.getsignal(number) for number in STOP_SIGNALS}
    real_write = capture.write_metadata
    calls = {"n": 0}

    def write_then_stop(path: Path, metadata: dict[str, object]) -> None:
        real_write(path, metadata)
        calls["n"] += 1
        if calls["n"] == 1:
            # The handlers must already be installed for the first live metadata write:
            # otherwise this signal kills the process and leaves no record at all.
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(capture, "write_metadata", write_then_stop)

    assert (
        capture.main(
            [
                "--out",
                str(out),
                "--firmware-receipt",
                str(receipt),
                "--duration",
                "900",
                "--server-logs",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "STOP_BEFORE_START signal=SIGTERM serial_not_opened=True" in captured.out
    for label in ("bridge", "agent", "edge"):
        assert f"LOG_STREAM_NOT_STARTED label={label} reason=stopping" in captured.out
        assert f"log stream {label} not_started (exit=None)" in captured.out
    assert "the serial port was never opened (the capture stopped before it opened)" in captured.out
    assert "CAPTURE_HEALTH degraded reasons=[" in captured.out

    assert seen["ports"] == []
    assert not (out / "serial.log").exists()
    for label in ("bridge", "agent", "edge"):
        assert not (out / f"{label}.log").exists()
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["capture_status"] == "completed"
    assert metadata["serial_opened"] is False
    assert metadata["exit_reason"] == "stop_signal SIGTERM"
    assert metadata["stop_signal"] == "SIGTERM"
    assert sorted(metadata["stop_signal_handlers"]) == ["SIGHUP", "SIGINT", "SIGQUIT", "SIGTERM"]
    assert metadata["cleanup_errors"] == []
    assert sorted(metadata["log_streams"]) == ["agent", "bridge", "edge"]
    assert metadata["log_stream_health"] == "degraded"
    for number in STOP_SIGNALS:
        assert signal.getsignal(number) is before[number]


def test_a_capture_that_cannot_handle_every_stop_signal_refuses_to_start(
    tmp_path, capsys, monkeypatch
):
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    seen = _fake_serial(monkeypatch)
    _no_git(monkeypatch)
    before = {number: signal.getsignal(number) for number in STOP_SIGNALS}
    real_install = capture._install_stop_handlers

    def install_with_a_failure(recorder):
        previous, failures = real_install(recorder)
        return previous, [*failures, "installing the SIGHUP handler failed: OSError: no pty"]

    monkeypatch.setattr(capture, "_install_stop_handlers", install_with_a_failure)

    assert capture.main(["--out", str(out), "--firmware-receipt", str(receipt)]) == 2
    err = capsys.readouterr().err
    assert "installing the SIGHUP handler failed: OSError: no pty" in err
    assert "refusing to start a capture that cannot record every stop signal" in err
    # Nothing was opened, nothing was created, and no handler was left behind.
    assert seen["ports"] == []
    assert not out.exists()
    for number in STOP_SIGNALS:
        assert signal.getsignal(number) is before[number]


class _RudeProcess(_RunningProcess):
    """Refuses SIGTERM and SIGKILL, as a wedged process group can."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.terminate_attempts = 0

    def terminate(self) -> None:
        self.terminate_attempts += 1
        raise OSError("terminate refused")

    def kill(self) -> None:
        raise OSError("kill refused")


class _RudeHandle:
    """A log handle whose close() fails; the other handles must still be closed."""

    def __init__(self) -> None:
        self.close_attempts = 0

    def write(self, _data: bytes) -> int:
        return 0

    def close(self) -> None:
        self.close_attempts += 1
        raise OSError("bad file descriptor")


def test_one_cleanup_error_neither_skips_the_others_nor_the_metadata(
    tmp_path, capsys, monkeypatch
) -> None:
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    seen = _fake_serial(monkeypatch, close_error="port wedged")
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    processes: list[_RudeProcess] = []
    handles: list[_RudeHandle] = []

    def popen(*args: object, **kwargs: object) -> _RudeProcess:
        process = _RudeProcess(*args, **kwargs)
        processes.append(process)
        return process

    def open_handle(_path: Path) -> _RudeHandle:
        handle = _RudeHandle()
        handles.append(handle)
        return handle

    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    monkeypatch.setattr(capture, "_open_log_handle", open_handle)

    assert (
        capture.main(
            [
                "--out",
                str(out),
                "--firmware-receipt",
                str(receipt),
                "--duration",
                "1",
                "--server-logs",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "warning: closing the serial port: OSError: [Errno 5] port wedged" in captured.err

    metadata = json.loads((out / "capture.json").read_text())
    cleanup_errors = metadata["cleanup_errors"]
    recorded = "\n".join(cleanup_errors)
    assert "closing the serial port: OSError: [Errno 5] port wedged" in recorded
    for label in ("bridge", "agent", "edge"):
        assert f"terminating log stream {label}: OSError: terminate refused" in recorded
    assert recorded.count("closing a log file handle: OSError: bad file descriptor") == 3
    # Every later step still ran: the port was closed, every process was signalled and
    # every handle was closed, even though the first of each raised.
    assert [port.close_attempts for port in seen["ports"]] == [1]
    assert [port.closed for port in seen["ports"]] == [False]
    assert [process.terminate_attempts for process in processes] == [1, 1, 1]
    assert [handle.close_attempts for handle in handles] == [1, 1, 1]

    assert metadata["capture_status"] == "completed"
    assert metadata["completed_at_local"]
    assert metadata["exit_reason"] == "duration_elapsed"
    assert metadata["log_stream_health"] == "degraded"
    for error in cleanup_errors:
        assert f"cleanup {error}" in metadata["log_stream_health_reasons"]


class _RefusesKillProcess(_RunningProcess):
    """Never exits and refuses both signals: every wait must stay bounded."""

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        raise OSError("kill refused")

    def wait(self, timeout: float | None = None) -> int | None:
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)


def test_a_failed_kill_and_a_timed_out_wait_after_kill_are_recorded(
    tmp_path, capsys, monkeypatch
) -> None:
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    _fake_serial(monkeypatch)
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    monkeypatch.setattr(capture.subprocess, "Popen", _RefusesKillProcess)

    assert (
        capture.main(
            [
                "--out",
                str(out),
                "--firmware-receipt",
                str(receipt),
                "--duration",
                "1",
                "--server-logs",
            ]
        )
        == 1
    )
    capsys.readouterr()
    metadata = json.loads((out / "capture.json").read_text())
    cleanup_errors = metadata["cleanup_errors"]
    recorded = "\n".join(cleanup_errors)
    for label in ("bridge", "agent", "edge"):
        assert f"SIGKILL for log stream {label}: OSError: kill refused" in recorded
        assert f"wait after SIGKILL for log stream {label}: TimeoutExpired:" in recorded
    # The bounded finalization still reached the metadata, and claims no success.
    assert metadata["capture_status"] == "completed"
    assert metadata["completed_at_local"]
    assert metadata["log_stream_health"] == "degraded"
    # SIGKILL itself failed and the reap timed out, so nothing may claim the stream stopped.
    assert metadata["log_streams"]["bridge"]["status"] == "cleanup_failed"
    assert metadata["log_streams"]["bridge"]["forced_kill"] is True
    assert "stopped_by_capture" not in json.dumps(metadata["log_streams"])


class _WaitErrorProcess(_RunningProcess):
    """Its first wait() fails for a non-timeout reason; kill+reap must still be tried."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._waits = 0
        self.killed = False

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True
        self._code = -9

    def wait(self, timeout: float | None = None) -> int | None:
        self._waits += 1
        if self._waits == 1:
            raise OSError("wait failed")
        return self._code


def test_a_non_timeout_wait_error_still_reclaims_the_stream_and_says_so(
    tmp_path, capsys, monkeypatch
) -> None:
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    _fake_serial(monkeypatch)
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    processes: list[_WaitErrorProcess] = []

    def popen(*args: object, **kwargs: object) -> _WaitErrorProcess:
        process = _WaitErrorProcess(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(capture.subprocess, "Popen", popen)

    assert (
        capture.main(
            [
                "--out",
                str(out),
                "--firmware-receipt",
                str(receipt),
                "--duration",
                "1",
                "--server-logs",
            ]
        )
        == 1
    )
    capsys.readouterr()
    metadata = json.loads((out / "capture.json").read_text())
    recorded = "\n".join(metadata["cleanup_errors"])
    for label in ("bridge", "agent", "edge"):
        assert f"waiting for log stream {label}: OSError: wait failed" in recorded
    # Every stream was still reclaimed with SIGKILL, and the record says it stopped.
    assert [process.killed for process in processes] == [True, True, True]
    assert metadata["log_streams"]["bridge"]["status"] == "stopped_by_capture"
    assert metadata["log_streams"]["bridge"]["exit_code"] == -9
    # SIGKILL was used (and reclaimed the stream), so it is recorded, with the reason it
    # was needed: SIGTERM did not time out, so this must not claim it ignored SIGTERM.
    assert metadata["log_streams"]["bridge"]["forced_kill"] is True
    assert metadata["log_streams"]["bridge"]["forced_reason"] == "its wait failed"
    assert "its wait failed" in json.dumps(metadata["log_stream_health_reasons"])
    assert "ignored SIGTERM" not in json.dumps(metadata["log_stream_health_reasons"])
    assert metadata["log_stream_health"] == "degraded"
    assert metadata["capture_status"] == "completed"


def test_a_stream_that_exited_before_the_drain_is_not_reported_as_stopped(
    tmp_path, capsys, monkeypatch
) -> None:
    # The read loop can exit between a stream's exit and its next poll, so the stream
    # reaches the drain already exited: poll() has its code, and the record must say
    # exited_early/failed instead of claiming this capture stopped it.
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    draining = {"now": False}

    class _ExitsAtShutdown(_RunningProcess):
        def poll(self) -> int | None:
            return 0 if draining["now"] else None

        def terminate(self) -> None:
            raise AssertionError("an already-exited stream must not be terminated")

    _fake_serial(
        monkeypatch,
        chunks=[b"StateMachine: State: activating -> idle\r\n"],
        close_hook=lambda _port: draining.update(now=True),
    )
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    monkeypatch.setattr(capture.subprocess, "Popen", _ExitsAtShutdown)

    assert (
        capture.main(
            [
                "--out",
                str(out),
                "--firmware-receipt",
                str(receipt),
                "--duration",
                "1",
                "--server-logs",
            ]
        )
        == 1
    )
    captured = capsys.readouterr().out
    assert "LOG_STREAM_EXIT label=bridge exit=0 observed=at_drain" in captured
    metadata = json.loads((out / "capture.json").read_text())
    for label in ("bridge", "agent", "edge"):
        assert metadata["log_streams"][label]["status"] == "exited_early"
        assert metadata["log_streams"][label]["exit_code"] == 0
    assert "stopped_by_capture" not in json.dumps(metadata["log_streams"])
    assert "log stream bridge exited_early (exit=0)" in metadata["log_stream_health_reasons"]
    assert metadata["log_stream_health"] == "degraded"


class _GoneTerminal:
    """A terminal or pipe that closed: every write raises, as BrokenPipeError does."""

    def write(self, _text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def test_a_terminal_that_goes_away_cannot_cost_the_closing_record(tmp_path, monkeypatch) -> None:
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    _fake_serial(monkeypatch, chunks=[b"SystemInfo: boot\r\n"])
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    terminal = _GoneTerminal()
    monkeypatch.setattr(capture.sys, "stdout", terminal)
    monkeypatch.setattr(capture.sys, "stderr", terminal)

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--duration", "1"])
        == 0
    )
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["capture_status"] == "completed"
    assert metadata["completed_at_local"]
    assert metadata["exit_reason"] == "duration_elapsed"
    assert metadata["cleanup_errors"] == []
    assert metadata["log_stream_health"] == "not_requested"
    assert b"SystemInfo" in (out / "serial.log").read_bytes()


def test_handlers_stay_installed_until_the_closing_record_is_written(
    tmp_path, capsys, monkeypatch
) -> None:
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    before = {number: signal.getsignal(number) for number in STOP_SIGNALS}
    _fake_serial(monkeypatch, chunks=[b"StateMachine: State: activating -> idle\r\n"])
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    real_write = capture.write_metadata
    snapshots: list[tuple[object, object]] = []

    def observing(path: Path, metadata: dict[str, object]) -> None:
        snapshots.append((metadata.get("capture_status"), signal.getsignal(signal.SIGTERM)))
        real_write(path, metadata)

    monkeypatch.setattr(capture, "write_metadata", observing)

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--duration", "1"])
        == 0
    )
    capsys.readouterr()
    assert [status for status, _ in snapshots] == ["in_progress", "in_progress", "completed"]
    # The capture's own handler is still in place while the closing record is written...
    assert snapshots[-1][1] is not before[signal.SIGTERM]
    # ...and only afterwards are the caller's handlers handed back.
    for number in STOP_SIGNALS:
        assert signal.getsignal(number) is before[number]


def test_a_stop_during_the_closing_record_leaves_a_complete_honest_record(
    tmp_path, capsys, monkeypatch
) -> None:
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"
    _fake_serial(monkeypatch, chunks=[b"StateMachine: State: activating -> idle\r\n"])
    _no_git(monkeypatch)
    _fast_clock(monkeypatch)
    real_write = capture.write_metadata
    calls = {"n": 0}

    def stop_during_the_record(path: Path, metadata: dict[str, object]) -> None:
        calls["n"] += 1
        if calls["n"] == 3:  # third write = the closing record
            os.kill(os.getpid(), signal.SIGTERM)
        real_write(path, metadata)

    monkeypatch.setattr(capture, "write_metadata", stop_during_the_record)

    assert (
        capture.main(["--out", str(out), "--firmware-receipt", str(receipt), "--duration", "1"])
        == 0
    )
    capsys.readouterr()
    metadata = json.loads((out / "capture.json").read_text())
    # The signal cannot kill the record, and the record does not hide it either.
    assert metadata["capture_status"] == "completed"
    assert metadata["completed_at_local"]
    assert metadata["stop_signal"] == "SIGTERM"
    assert metadata["exit_reason"] == "stop_signal SIGTERM"
    assert metadata["log_stream_health"] == "not_requested"
