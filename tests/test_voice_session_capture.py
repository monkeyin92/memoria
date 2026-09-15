"""Tests for the receipt-bound voice session capture tool.

No serial port is ever opened here: the preflight path validates the receipt and
writes capture.json, and the live path is skipped when pyserial is importable
because it would touch real hardware (the project .venv does not ship pyserial).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
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


def test_live_capture_without_pyserial_never_opens_a_port(tmp_path: Path, capsys) -> None:
    if importlib.util.find_spec("serial") is not None:
        pytest.skip("pyserial is installed; the live path would touch real hardware")
    receipt = _receipt_file(tmp_path)
    out = tmp_path / "live"

    assert capture.main(["--out", str(out), "--firmware-receipt", str(receipt)]) == 2
    assert "needs pyserial" in capsys.readouterr().err
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["capture_mode"] == "live"
    assert metadata["serial_opened"] is False
    assert not (out / "serial.log").exists()


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
    monkeypatch, chunks: list[bytes] | None = None, open_error: str = ""
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
            seen["ports"].append(self)

        def open(self) -> None:
            if open_error:
                raise OSError(16, open_error)
            self.is_open = True

        def readline(self) -> bytes:
            return chunks.pop(0) if chunks else b""

        def close(self) -> None:
            self.closed = True

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
    assert "log stream bridge ignored SIGTERM and needed SIGKILL" in printed
    metadata = json.loads((out / "capture.json").read_text())
    assert metadata["log_stream_health"] == "degraded"
    assert metadata["log_streams"]["bridge"]["status"] == "stopped_by_capture"
    assert metadata["log_streams"]["bridge"]["forced_kill"] is True
