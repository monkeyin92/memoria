"""How the test scripts open the robot's USB port and recover a silent one (fake pyserial, no robot).

Measured on the robot 2026-10-21 (ESP32-S3 USB-Serial/JTAG, macOS): a default pyserial open, or DTR and RTS
both set low before the open, resets the board (`rst:0x15 USB_UART_CHIP_RESET`); RTS low then DTR high before
the open does not. A handle opened before an `esptool --after hard-reset` receives nothing afterwards.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest
from scripts import voice_soak_serial_logger as logger

PORT = "/dev/cu.usbmodem2101"
RENAMED = "/dev/cu.usbmodem101"
NO_RESET_OPEN = [("rts", False, False), ("dtr", True, False), ("open",)]


class FakeSerialException(OSError):
    pass


class Bench:
    """The fake hardware: what each successive open of the port reads, and a clock reads advance."""

    def __init__(self, scripts: list[list[object]], fail_opens: int = 0) -> None:
        self.scripts = scripts
        self.fail_opens = fail_opens
        self.ports: list[Serial] = []
        self.now = 0.0
        self.sleeps: list[float] = []
        self.logs: list[str] = []
        self.module = types.ModuleType("serial")
        bench = self

        class Serial:
            def __init__(self, *args: object, **kwargs: object) -> None:
                assert not args and not kwargs, (
                    "Serial() must be built empty, or it opens with the reset lines"
                )
                object.__setattr__(self, "ops", [])
                object.__setattr__(self, "is_open", False)
                object.__setattr__(self, "written", [])

            def __setattr__(self, name: str, value: object) -> None:
                if name in ("dtr", "rts"):
                    self.ops.append((name, value, self.is_open))
                object.__setattr__(self, name, value)

            def open(self) -> None:
                if bench.fail_opens:
                    bench.fail_opens -= 1
                    raise FakeSerialException("could not open port")
                self.ops.append(("open",))
                self.is_open = True
                self.script = bench.scripts.pop(0) if bench.scripts else []
                bench.ports.append(self)

            def read(self, size: int) -> bytes:
                if self.script:
                    item = self.script.pop(0)
                    if isinstance(item, Exception):
                        raise item
                    return item
                bench.now += 5.0
                return b""

            def write(self, payload: bytes) -> None:
                self.written.append(payload)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                self.is_open = False

        self.module.Serial = Serial
        self.module.SerialException = FakeSerialException

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def keeper(self, list_ports: list[list[str]]) -> logger.ReopeningPort:
        listings = iter(list_ports)
        return logger.ReopeningPort(
            self.module,
            PORT,
            self.logs.append,
            list_ports=lambda: next(listings),
            clock=lambda: self.now,
            sleep=self.sleep,
        )


def read_until(port: logger.ReopeningPort, wanted: bytes, limit: int = 200) -> bytes:
    got = b""
    for _ in range(limit):
        got += port.read()
        if wanted in got:
            return got
    raise AssertionError(f"never read {wanted!r}, got {got!r}")


def test_open_sets_rts_low_then_dtr_high_before_the_open_and_never_touches_them_again():
    bench = Bench([[b"boot ok\n"]])
    port = bench.keeper([])
    assert read_until(port, b"boot ok") == b"boot ok\n"
    port.write(b"wake\n")
    (opened,) = bench.ports
    assert opened.ops == NO_RESET_OPEN
    assert (opened.port, opened.baudrate) == (PORT, 115200)
    assert opened.written == [b"wake\n"]


def test_thirty_silent_seconds_reopen_the_renamed_port_without_a_reset():
    bench = Bench([[], [b"I (5) back\n"]])
    port = bench.keeper([[RENAMED]])
    assert read_until(port, b"back") == b"I (5) back\n"
    assert len(bench.ports) == 2
    assert bench.ports[0].is_open is False
    assert all(p.ops == NO_RESET_OPEN for p in bench.ports)
    assert bench.ports[1].port == RENAMED
    assert bench.sleeps == [logger.REOPEN_PAUSE_S]
    assert bench.now >= logger.SILENCE_S
    assert f"LOGGER: no serial data for 30 s on {PORT}" in bench.logs
    assert f"LOGGER: reopen 1/3: port is now {RENAMED} (was {PORT})" in bench.logs
    port.write(b"wake\n")
    assert bench.ports[1].written == [b"wake\n"]


def test_the_original_name_wins_while_it_is_still_listed():
    bench = Bench([[], [b"x\n"]])
    port = bench.keeper([[RENAMED, PORT]])
    read_until(port, b"x")
    assert bench.ports[1].port == PORT


def test_data_after_a_reopen_restores_all_three_attempts():
    bench = Bench([[], [], [b"a\n"], [], [], [b"b\n"]])
    port = bench.keeper([[PORT]] * 5)
    read_until(port, b"a")
    read_until(port, b"b")
    assert len(bench.ports) == 6


def test_three_silent_reopens_give_up():
    bench = Bench([[], [], [], []])
    port = bench.keeper([[PORT]] * 3)
    with pytest.raises(logger.PortGaveUp, match="no serial data after 3 reopens"):
        read_until(port, b"never")
    assert len(bench.ports) == 4
    assert bench.sleeps == [logger.REOPEN_PAUSE_S] * 3


def test_a_missing_port_and_a_failed_open_each_use_up_an_attempt():
    bench = Bench([[], []])
    port = bench.keeper([[], [PORT], [PORT]])
    bench.fail_opens = 1
    with pytest.raises(logger.PortGaveUp):
        read_until(port, b"never")
    assert "LOGGER: reopen 1/3: no /dev/cu.usbmodem* present" in bench.logs
    assert "LOGGER: reopen 2/3: cannot open /dev/cu.usbmodem2101: could not open port" in bench.logs
    assert "LOGGER: reopen 3/3: opened /dev/cu.usbmodem2101" in bench.logs


def test_a_read_error_reopens_at_once():
    bench = Bench(
        [[FakeSerialException("device reports readiness to read but returned no data")], [b"ok\n"]]
    )
    port = bench.keeper([[PORT]])
    read_until(port, b"ok")
    assert bench.now == logger.REOPEN_PAUSE_S
    assert len(bench.ports) == 2


def test_a_command_during_a_reopen_is_refused_not_written():
    bench = Bench([[]])
    port = bench.keeper([[]])
    server = logger.CommandServer(Path("/tmp/unused.sock"), port.write, bench.logs.append)
    while len(bench.sleeps) < 1:
        port.read()
    assert server.dispatch("wake").startswith(
        "error: serial write failed: the port is being reopened"
    )


def run_main(monkeypatch, tmp_path, bench: Bench, *extra: str) -> tuple[int, str]:
    out = tmp_path / "robot.log"
    listings = iter([[PORT]] * 3)
    monkeypatch.setitem(sys.modules, "serial", bench.module)
    monkeypatch.setattr(logger.signal, "signal", lambda *args: None)
    real = logger.ReopeningPort
    monkeypatch.setattr(
        logger,
        "ReopeningPort",
        lambda *args: real(
            *args, list_ports=lambda: next(listings), clock=lambda: bench.now, sleep=bench.sleep
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["voice_soak_serial_logger.py", str(out), "--port", PORT, "--no-command-socket", *extra],
    )
    return logger.main(), out.read_text(encoding="utf-8")


def test_main_asks_for_a_replug_with_the_last_seen_time(monkeypatch, tmp_path, capsys):
    bench = Bench([[b"I (1) up\n"], [], [], []])
    status, log = run_main(
        monkeypatch, tmp_path, bench, "--last-seen-cmd", "printf '2026-10-21 11:15:02+08\\n'"
    )
    assert status == 2
    assert " I (1) up\n" in log
    (hint,) = [line for line in log.splitlines() if logger.REPLUG_HINT in line]
    assert hint.endswith("服务器最后一次请求: 2026-10-21 11:15:02+08")
    assert "未复位" in hint
    assert logger.REPLUG_HINT in capsys.readouterr().err
    assert all(p.ops == NO_RESET_OPEN for p in bench.ports)


def test_main_runs_no_command_without_last_seen_cmd(monkeypatch, tmp_path):
    bench = Bench([[], [], [], []])

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("nothing may run without --last-seen-cmd")

    monkeypatch.setattr(logger.subprocess, "run", refuse)
    status, log = run_main(monkeypatch, tmp_path, bench)
    assert status == 2
    assert "服务器最后一次请求: 未提供 --last-seen-cmd" in log


def test_a_failing_last_seen_cmd_reports_its_exit_code_only():
    assert logger.last_seen("echo secret-token >&2; exit 3") == "--last-seen-cmd 没有结果 (exit 3)"


def test_the_soak_driver_opens_its_own_port_without_a_reset(monkeypatch, tmp_path):
    bench = Bench([[b"I (9) StateMachine: State: starting -> idle\n"]])
    monkeypatch.setitem(sys.modules, "serial", bench.module)
    monkeypatch.delitem(sys.modules, "voice_soak", raising=False)
    monkeypatch.delitem(sys.modules, "voice_soak_serial_logger", raising=False)
    soak = importlib.import_module("scripts.voice_soak")
    watcher = soak.SerialWatcher(tmp_path / "serial.log", port=PORT)
    watcher.start()
    try:
        assert watcher.wait_state({"idle"}, 5) is not None
    finally:
        watcher.stop.set()
        watcher._thread.join(timeout=5)
    (opened,) = bench.ports
    assert opened.ops == NO_RESET_OPEN
    assert opened.port == PORT
