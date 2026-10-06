"""The USB command path of the computer-driven tests (offline: no serial port, no robot).

Firmware build 20 reads one command, `wake`, from the robot's USB port; a bench image (TODOLIST M-2) also reads
the read-only `snap` and `status`. The port may be opened by exactly one process (every open pulses DTR/RTS and
resets the board), so `voice_soak_serial_logger.py` holds it and forwards the commands from a unix socket.
These tests drive the real socket server and client with a fake port, and pin the rule the user set on
2026-10-02: the wake word is never used to wake the robot in a test.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import socket
import tempfile
import threading
import zlib
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
        "SNAP",
        "snap now",
        "snapshot",
        "STATUS",
        "status now",
        "face=happy",
        ">face=happy",
        "wake;snap",
        "snap\nreboot",
        "status\nwake",
    ],
)
def test_nothing_but_the_three_words_can_be_typed_into_the_board(served, request_text: str) -> None:
    server, port, lines = served
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3.0)
        conn.connect(str(server.path))
        conn.sendall(request_text.encode("latin-1") + b"\n")
        reply = conn.recv(256).decode()
    first_line = request_text.split("\n", 1)[0].strip()
    if first_line in {
        "wake",
        "snap",
        "status",
    }:  # "wake\nreboot": the first line is the request, the rest is ignored
        assert reply == "ok\n" and port.written == [first_line.encode() + b"\n"]
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


def test_the_wire_table_holds_exactly_the_words_the_robot_knows() -> None:
    # `wake` is in every image; `snap` and `status` only read, and only a bench image answers them.
    assert logger.WIRE_COMMANDS == {"wake": b"wake\n", "snap": b"snap\n", "status": b"status\n"}
    assert set(command.COMMANDS) == {"wake", "snap", "status", "ping"}
    assert set(command.BENCH_COMMANDS) == {"snap", "status"}


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


def test_snap_and_status_reach_the_port_as_one_line_each_and_are_logged(served) -> None:
    server, port, lines = served
    assert command.send_command("snap", socket_path=server.path) == "ok"
    assert command.send_command("status", socket_path=server.path) == "ok"
    assert port.written == [b"snap\n", b"status\n"]
    assert lines == ["LOGGER: command snap sent", "LOGGER: command status sent"]


# --- a bench robot, as far as the log can tell -----------------------------------------------------------


def _snap_lines(width: int, height: int, snap_id: str = "0001e240") -> tuple[bytes, list[str]]:
    picture = bytes((index * 7 + 3) & 0xFF for index in range(width * height * 2))
    total = -(-len(picture) // 384)
    lines = []
    for seq in range(1, total + 1):
        part = picture[(seq - 1) * 384 : seq * 384]
        lines.append(
            f"SNAP {snap_id} {width}x{height} {seq}/{total} {zlib.crc32(part):08x} "
            f"{base64.b64encode(part).decode()}"
        )
    lines.append(
        f"SNAP {snap_id} end {total} {len(picture)} {zlib.crc32(picture):08x} fmt=rgb565le"
    )
    return picture, lines


class FakeRobot(FakePort):
    """What the logger would record after `snap` and `status`: the robot's answer, or its refusal."""

    def __init__(self, log: Path, *, bench: bool = True) -> None:
        super().__init__()
        self.log = log
        self.bench = bench
        self.requests = 0
        self.picture = b""

    def send(self, payload: bytes) -> None:
        super().send(payload)
        self.requests += 1
        if payload not in (b"snap\n", b"status\n"):
            return
        if not self.bench:
            answer = ["I (9) MemoriaUsb: usb command ignored (not a command)"]
        elif payload == b"snap\n":
            self.picture, answer = _snap_lines(100, 10)
        else:
            up_ms = 1000 + 60000 * (self.requests - 1)
            answer = [
                f"I (9) MemoriaBench: status up_ms={up_ms} phase=idle mood=neutral frame=default "
                f"screen_off=0 sleeping=0 captioned=0 frames={self.requests * 1500} "
                f"drawn={self.requests * 1470} render_us={self.requests * 18_000_000} render_max_us=33000 "
                f"busy_us={self.requests * 30_000_000} px={self.requests * 147_000_000} "
                f"composed_px={self.requests * 73_500_000} extra_ms=0 heap_free=140000 "
                "psram_free=4000000 anim_stack_free=2100"
            ]
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write("".join(f"10:00:00.000 {line}\n" for line in answer))


@pytest.fixture
def bench_rig(socket_dir):
    log = socket_dir / "robot.log"
    log.write_text("10:00:00.000 I (1) boot: an earlier session\n", encoding="utf-8")
    robot = FakeRobot(log)
    lines: list[str] = []
    server = logger.CommandServer(socket_dir / "robot.sock", robot.send, lines.append)
    server.start()
    try:
        yield server, robot, log, lines
    finally:
        server.stop()


def test_snap_waits_in_the_log_for_the_picture_and_returns_it_as_a_png(bench_rig) -> None:
    server, robot, log, lines = bench_rig
    shot, png = command.take_snapshot(log, socket_path=server.path, wait=5)
    assert shot.problems() == [] and shot.raw() == robot.picture
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert lines == ["LOGGER: command snap sent"]
    # The evidence stays in the session's log, next to everything else the robot said.
    assert log.read_text(encoding="utf-8").count("SNAP 0001e240 100x10") == 6


def test_status_waits_in_the_log_for_the_line_and_returns_it_parsed(bench_rig) -> None:
    server, _robot, log, _lines = bench_rig
    got = command.read_status(log, socket_path=server.path, wait=5)
    assert got["up_ms"] == 1000 and got["phase"] == "idle" and got["drawn"] == 1470


def test_a_product_image_ignoring_the_request_is_named_at_once(socket_dir) -> None:
    log = socket_dir / "robot.log"
    robot = FakeRobot(log, bench=False)
    server = logger.CommandServer(socket_dir / "robot.sock", robot.send, lambda _line: None)
    server.start()
    try:
        for call in (command.take_snapshot, command.read_status):
            with pytest.raises(command.SerialCommandError, match="not running a bench image"):
                call(log, socket_path=server.path, wait=5)
    finally:
        server.stop()


def test_no_logger_stops_snap_before_any_waiting(socket_dir) -> None:
    with pytest.raises(
        command.SerialCommandError, match="start scripts/voice_soak_serial_logger.py"
    ):
        command.take_snapshot(socket_dir / "robot.log", socket_path=socket_dir / "missing.sock")


def test_the_command_line_writes_the_png_and_prints_where(bench_rig, capsys, tmp_path) -> None:
    server, _robot, log, _lines = bench_rig
    out = tmp_path / "shot.png"
    argv = [
        "snap",
        "--socket",
        str(server.path),
        "--log",
        str(log),
        "--out",
        str(out),
        "--wait",
        "5",
    ]
    assert command.main(argv) == 0
    assert capsys.readouterr().out.strip() == f"wrote {out} (100x10, snapshot 0001e240)"
    assert out.read_bytes().startswith(b"\x89PNG")


def test_the_command_line_names_the_png_after_the_snapshot_by_default(
    bench_rig, capsys, monkeypatch, tmp_path
) -> None:
    server, _robot, log, _lines = bench_rig
    monkeypatch.chdir(tmp_path)
    assert command.main(["snap", "--socket", str(server.path), "--log", str(log)]) == 0
    assert (tmp_path / "snap-0001e240.png").exists()
    assert "snap-0001e240.png" in capsys.readouterr().out


def test_status_after_asks_twice_and_prints_the_rates_between(bench_rig, capsys) -> None:
    server, _robot, log, _lines = bench_rig
    argv = [
        "status",
        "--socket",
        str(server.path),
        "--log",
        str(log),
        "--after",
        "0",
        "--wait",
        "5",
    ]
    assert command.main(argv) == 0
    out = capsys.readouterr().out
    assert "between up_ms 1000 and 61000 (60.0 s)" in out
    assert "drawn 24.5 /s" in out


def test_status_without_after_prints_the_fields(bench_rig, capsys) -> None:
    server, _robot, log, _lines = bench_rig
    assert command.main(["status", "--socket", str(server.path), "--log", str(log)]) == 0
    assert "phase  idle" in capsys.readouterr().out.replace("   ", "  ")


def test_snap_without_a_log_only_sends_it(bench_rig, capsys) -> None:
    server, robot, log, _lines = bench_rig
    before = log.stat().st_size
    assert command.main(["snap", "--socket", str(server.path)]) == 0
    assert capsys.readouterr().out.strip() == "ok"
    assert robot.requests == 1
    assert log.stat().st_size > before  # the picture is in the log for snap_to_png.py to read later


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["wake", "--log", "x.log"], "--log does not apply to wake"),
        (["ping", "--wait", "3"], "--wait does not apply to ping"),
        (["status", "--log", "x.log", "--out", "x.png"], "--out does not apply to status"),
        (["status", "--log", "x.log", "--partial"], "--partial does not apply to status"),
        (["snap", "--log", "x.log", "--after", "5"], "--after does not apply to snap"),
        (["snap", "--out", "x.png"], "--out needs --log FILE"),
        (["status", "--after", "5"], "--after needs --log FILE"),
        (["snap", "--partial"], "--partial needs --log FILE"),
    ],
)
def test_options_that_do_not_belong_to_the_command_are_refused(argv, message, capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        command.main(argv)
    assert stopped.value.code == 2
    assert message in capsys.readouterr().err
