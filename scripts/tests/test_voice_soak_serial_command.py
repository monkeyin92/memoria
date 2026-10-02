"""The USB wake command path of the computer-driven tests (offline: no serial port, no robot).

Firmware build 20 reads one command, `wake`, from the robot's USB port. The port may be opened by exactly one
process (every open pulses DTR/RTS and resets the board), so `voice_soak_serial_logger.py` holds it and
forwards the command from a unix socket. These tests drive the real socket server and client with a fake
port, and pin the rule the user set on 2026-10-02: the wake word is never used to wake the robot in a test.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest
from scripts import voice_soak_serial_command as command
from scripts import voice_soak_serial_logger as logger

SCRIPTS = Path(__file__).resolve().parents[1]


@pytest.fixture
def socket_dir():
    # A unix socket path is limited to ~104 bytes on macOS; pytest's tmp_path can be longer.
    directory = Path(tempfile.mkdtemp(prefix="msc", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class FakePort:
    def __init__(self, fail: Exception | None = None) -> None:
        self.written: list[bytes] = []
        self.fail = fail

    def send(self, payload: bytes) -> None:
        if self.fail is not None:
            raise self.fail
        self.written.append(payload)


@pytest.fixture
def served(socket_dir):
    port = FakePort()
    lines: list[str] = []
    server = logger.CommandServer(socket_dir / "robot.sock", port.send, lines.append)
    server.start()
    try:
        yield server, port, lines
    finally:
        server.stop()


def test_wake_reaches_the_port_as_exactly_one_line_and_is_logged(served) -> None:
    server, port, lines = served
    assert command.send_command("wake", socket_path=server.path) == "ok"
    assert port.written == [b"wake\n"]
    assert lines == ["LOGGER: command wake sent"]


def test_ping_proves_the_logger_is_alive_without_touching_the_port(served) -> None:
    server, port, lines = served
    assert command.send_command("ping", socket_path=server.path) == "ok"
    assert port.written == [] and lines == []


@pytest.mark.parametrize(
    "request_text",
    [
        "reboot",
        "WAKE",
        "wake now",
        "wake\nreboot",
        "wak",
        "",
        "stop",
        "erase",
        "\x00wake",
        "wake " + "x" * 200,
    ],
)
def test_nothing_but_wake_can_be_typed_into_the_board(served, request_text: str) -> None:
    server, port, lines = served
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3.0)
        conn.connect(str(server.path))
        conn.sendall(request_text.encode("latin-1") + b"\n")
        reply = conn.recv(256).decode()
    first_line = request_text.split("\n", 1)[0].strip()
    if first_line == "wake":  # "wake\nreboot": the first line is the request, the rest is ignored
        assert reply == "ok\n" and port.written == [b"wake\n"]
    else:
        assert reply.startswith("error: unknown command")
        assert port.written == []


def test_the_client_refuses_a_command_the_logger_does_not_know(socket_dir) -> None:
    with pytest.raises(command.SerialCommandError, match="unknown command"):
        command.send_command("reboot", socket_path=socket_dir / "x.sock")


def test_a_failed_port_write_is_reported_and_not_logged_as_sent(socket_dir) -> None:
    port = FakePort(fail=OSError("device disconnected"))
    lines: list[str] = []
    server = logger.CommandServer(socket_dir / "robot.sock", port.send, lines.append)
    server.start()
    try:
        with pytest.raises(
            command.SerialCommandError, match="serial write failed: device disconnected"
        ):
            command.send_command("wake", socket_path=server.path)
    finally:
        server.stop()
    assert lines == []


def test_no_logger_gives_an_actionable_error(socket_dir) -> None:
    with pytest.raises(
        command.SerialCommandError, match="start scripts/voice_soak_serial_logger.py"
    ):
        command.send_command("wake", socket_path=socket_dir / "missing.sock")


def test_the_socket_is_owner_only_and_removed_when_the_logger_stops(socket_dir) -> None:
    server = logger.CommandServer(socket_dir / "robot.sock", FakePort().send, lambda _line: None)
    server.start()
    assert (os.stat(server.path).st_mode & 0o777) == 0o600
    server.stop()
    assert not server.path.exists()


def test_a_second_logger_for_the_same_socket_is_refused_and_a_stale_socket_is_replaced(
    socket_dir,
) -> None:
    path = socket_dir / "robot.sock"
    first = logger.CommandServer(path, FakePort().send, lambda _line: None)
    first.start()
    try:
        with pytest.raises(SystemExit, match="already serves"):
            logger.CommandServer(path, FakePort().send, lambda _line: None).start()
    finally:
        first.stop()
    # A logger that died without cleaning up leaves a socket file nobody listens on.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    assert path.exists()
    revived = logger.CommandServer(path, FakePort().send, lambda _line: None)
    revived.start()
    try:
        assert command.send_command("ping", socket_path=path) == "ok"
    finally:
        revived.stop()


def test_a_path_that_is_not_a_socket_is_never_deleted(socket_dir) -> None:
    precious = socket_dir / "notes.txt"
    precious.write_text("keep me")
    with pytest.raises(SystemExit, match="not a socket"):
        logger.CommandServer(precious, FakePort().send, lambda _line: None).start()
    assert precious.read_text() == "keep me"


def test_concurrent_wakes_each_become_one_complete_line(served) -> None:
    server, port, _lines = served
    results: list[str] = []

    def one() -> None:
        results.append(command.send_command("wake", socket_path=server.path))

    threads = [threading.Thread(target=one) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert results == ["ok"] * 8
    assert port.written == [b"wake\n"] * 8


def test_default_socket_path_is_short_and_follows_the_port() -> None:
    path = command.default_socket_path("/dev/cu.usbmodem2101")
    assert path == Path("/tmp/memoria-serial-usbmodem2101.sock")
    assert len(str(path)) < 100
    assert command.default_socket_path("/dev/tty.usbserial-A1 b") == Path(
        "/tmp/memoria-serial-usbserial-A1_b.sock"
    )


def test_the_only_wire_command_is_the_one_the_robot_knows() -> None:
    assert logger.WIRE_COMMANDS == {"wake": b"wake\n"}
    assert set(command.COMMANDS) == {"wake", "ping"}


def test_the_soak_driver_wakes_over_usb_and_never_plays_the_wake_word() -> None:
    source = (SCRIPTS / "voice_soak.py").read_text(encoding="utf-8")
    assert "--wake-clips" not in source.split("def main()", 1)[1]
    assert "wake_clips" not in source and "wake_voice" not in source
    assert "茉莉" not in source.split("def ensure_listening", 1)[1].split("turn = 0", 1)[0]
    ensure = source.split("def ensure_listening", 1)[1].split("turn = 0", 1)[0]
    assert "watcher.wake()" in ensure
    # A failed or refused wake ends the step: no fallback to the voice.
    assert re.search(
        r"except SerialCommandError as exc:\s+record\(kind=\"wake_failed\".*\n\s+return False",
        ensure,
    )
    assert "say(" not in ensure and "play_clip(" not in ensure


def test_the_soak_watchers_send_wake_through_the_logger_or_their_own_port() -> None:
    source = (SCRIPTS / "voice_soak.py").read_text(encoding="utf-8")
    tail = source.split("class LogTailWatcher", 1)[1].split("def clip_seconds", 1)[0]
    assert 'send_command("wake", socket_path=self.wake_socket, port=self.port)' in tail
    own = source.split("class SerialWatcher", 1)[1].split("MIN_CLIP_SECONDS", 1)[0]
    assert 'self.ser.write(b"wake\\n")' in own
    assert "usb wake (accepted|ignored)" in source


def test_the_logger_only_writes_what_the_wire_table_allows() -> None:
    source = (SCRIPTS / "voice_soak_serial_logger.py").read_text(encoding="utf-8")
    # ser.write is reached only through send(), which only dispatch() calls with a WIRE_COMMANDS payload.
    assert source.count("ser.write(") == 1
    assert source.count("self._send(") == 1
    assert "payload = WIRE_COMMANDS.get(verb)" in source
