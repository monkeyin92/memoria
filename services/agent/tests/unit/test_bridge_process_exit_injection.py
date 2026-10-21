"""P2-04 process-exit injection for the media bridge.

Proven, offline, with no device and no Edge Go path:

* SIGTERM reaches the real ``scripts.run_media_bridge.run`` coroutine (the
  body of ``main``). The process exits 0, the gRPC and metrics listen sockets
  are gone, a successor process can bind those same ports, and ``run`` returns
  with no pending asyncio tasks.
* SIGKILL is reaped here. The kernel closes the listen sockets and a successor
  can bind them. Asyncio drain is not observable after SIGKILL and is not claimed.

Not proven: device terminate-no-reconnect, Edge Go connection or lease or
runtime reclaim (``services/media_edge/device_ws_fault_injection_test.go``),
in-process ``runtime_shutdown.close`` (``test_agent_production_wiring.py``),
a longsoak memory or connection or latency window, FunASR, production, or
firmware. The provider-neutral child loads no session factory, so this harness
has no Voice Core runtime or media lease to reclaim.

``scripts/offline_process_trend_sample.py`` prints a few samples only. It is
not that longsoak window. Linux reads them from /proc; macOS reads RSS and
liveness from libproc and proves a closed listener with a refused connect, and
reports the socket count as unmeasured. Both paths assert the same reclaim
facts, so a local Mac run exercises this harness instead of skipping it.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import pytest
from scripts import local_process_probe

_CHILD = Path(__file__).with_name("bridge_process_exit_child.py")
_REPO = Path(__file__).resolve().parents[4]
_TREND = _REPO / "scripts" / "offline_process_trend_sample.py"
_RECLAIM_MARKER = "MEMORIA_PROCESS_RECLAIM "
_READY_TIMEOUT_S = 45.0
_EXIT_TIMEOUT_S = 10.0
_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LD_LIBRARY_PATH", "TMPDIR", "TZ")


@dataclass(frozen=True, slots=True)
class ProcessExitFault:
    """One injected exit and the reclaim facts the harness can see."""

    name: str
    signum: int
    exit_code: int
    listening_sockets_closed: bool
    process_reaped: bool
    successor_can_bind: bool
    # None means the child cannot report tasks (it was killed first).
    leftover_asyncio_tasks: tuple[str, ...] | None


PROCESS_EXIT_FAULTS: tuple[ProcessExitFault, ...] = (
    ProcessExitFault(
        name="sigterm",
        signum=signal.SIGTERM,
        exit_code=0,
        listening_sockets_closed=True,
        process_reaped=True,
        successor_can_bind=True,
        leftover_asyncio_tasks=(),
    ),
    ProcessExitFault(
        name="sigkill",
        signum=signal.SIGKILL,
        exit_code=-signal.SIGKILL,
        listening_sockets_closed=True,
        process_reaped=True,
        successor_can_bind=True,
        leftover_asyncio_tasks=None,
    ),
)


@dataclass(frozen=True, slots=True)
class _Observation:
    exit_code: int
    listening_sockets_closed: bool
    process_reaped: bool
    successor_can_bind: bool
    leftover_asyncio_tasks: tuple[str, ...] | None


@dataclass
class _Log:
    _lines: list[str]
    _lock: threading.Lock

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)

    def text(self) -> str:
        with self._lock:
            return "".join(self._lines)


@dataclass
class _RunningBridge:
    process: subprocess.Popen[str]
    log: _Log
    reader: threading.Thread

    @property
    def pid(self) -> int:
        assert self.process.pid is not None
        return self.process.pid


def _sigterm_caught(mask: int) -> bool:
    return bool(mask & (1 << (signal.SIGTERM - 1)))


def _listen_ports_from(text: str) -> set[int]:
    found: set[int] = set()
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":
            continue
        _host, separator, port_hex = fields[1].rpartition(":")
        if separator:
            found.add(int(port_hex, 16))
    return found


def _connect_probe(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _ports_listening(ports: tuple[int, ...]) -> bool:
    if local_process_probe.procfs_available():
        listening: set[int] = set()
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError:
                continue
            listening.update(_listen_ports_from(text))
        return all(port in listening for port in ports)
    # Darwin: a refused connect is the end-to-end proof the listener is gone.
    return all(_connect_probe(port) for port in ports)


def _wait_until_unreachable(ports: tuple[int, ...]) -> bool:
    deadline = time.monotonic() + 2.0
    while True:
        if not any(_connect_probe(port) for port in ports):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _sigterm_observed(mask: int) -> bool:
    return not local_process_probe.procfs_available() or _sigterm_caught(mask)


def _caught_mask(pid: int) -> int | None:
    return local_process_probe.caught_signals(pid)


def _free_ports(count: int) -> tuple[int, ...]:
    holders: list[socket.socket] = []
    try:
        for _ in range(count):
            holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            holder.bind(("127.0.0.1", 0))
            holders.append(holder)
        return tuple(int(holder.getsockname()[1]) for holder in holders)
    finally:
        for holder in holders:
            holder.close()


def _child_environ(*, grpc_port: int, metrics_port: int) -> dict[str, str]:
    """Development-only env. Parent production keys are not copied."""

    env = {name: os.environ[name] for name in _PASSTHROUGH if name in os.environ}
    env.update(
        {
            "PYTHONPATH": str(_REPO),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ENVIRONMENT": "development",
            "OFFLINE_MOCK": "true",
            "LOG_LEVEL": "INFO",
            "MEDIA_BRIDGE_GRPC_ENABLED": "true",
            "MEDIA_BRIDGE_GRPC_ADDR": f"127.0.0.1:{grpc_port}",
            "MEDIA_BRIDGE_MTLS": "false",
            "MEDIA_BRIDGE_SESSION_FACTORY": "",
            "MEDIA_BRIDGE_PROVIDER_FACTORY": "",
            "MEDIA_BRIDGE_RUNTIME_FACTORY": "",
            "PROMETHEUS_PORT": str(metrics_port),
        }
    )
    return env


def _drain(stream: TextIO, log: _Log) -> None:
    for line in stream:
        log.append(line)


def _kill_group(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_reclaim(text: str) -> tuple[str, ...] | None:
    found: tuple[str, ...] | None = None
    for line in text.splitlines():
        if _RECLAIM_MARKER not in line:
            continue
        payload = json.loads(line.split(_RECLAIM_MARKER, 1)[1])
        tasks = payload["leftover_tasks"]
        if not isinstance(tasks, list) or not all(isinstance(item, str) for item in tasks):
            raise AssertionError(f"bad reclaim line: {line}")
        found = tuple(tasks)
    return found


def _tail(text: str) -> str:
    return text[-4000:]


@contextmanager
def _bridge(tmp_path: Path, grpc_port: int, metrics_port: int) -> Iterator[_RunningBridge]:
    log = _Log(_lines=[], _lock=threading.Lock())
    process = subprocess.Popen(
        [sys.executable, "-u", str(_CHILD)],
        cwd=tmp_path,
        env=_child_environ(grpc_port=grpc_port, metrics_port=metrics_port),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )
    assert process.stdout is not None
    reader = threading.Thread(target=_drain, args=(process.stdout, log), daemon=True)
    reader.start()
    running = _RunningBridge(process=process, log=log, reader=reader)
    try:
        yield running
    finally:
        if process.poll() is None:
            _kill_group(process.pid)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_group(process.pid)
                process.wait(timeout=5)
        reader.join(timeout=2)


def _wait_until_serving(bridge: _RunningBridge, grpc_port: int, metrics_port: int) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    ports = (grpc_port, metrics_port)
    while time.monotonic() < deadline:
        if bridge.process.poll() is not None:
            raise AssertionError(
                "bridge exited before it was serving "
                f"code={bridge.process.returncode}\n{_tail(bridge.log.text())}"
            )
        text = bridge.log.text()
        mask = _caught_mask(bridge.pid)
        if (
            "provider-neutral" in text
            and "media bridge listening" in text
            and "mtls=False" in text
            and _ports_listening(ports)
            and mask is not None
            and _sigterm_observed(mask)
        ):
            return
        time.sleep(0.05)
    raise AssertionError(
        f"bridge was not serving mask={_caught_mask(bridge.pid)}\n{_tail(bridge.log.text())}"
    )


def _wait_until_quiet(ports: tuple[int, ...]) -> bool:
    if local_process_probe.procfs_available():
        deadline = time.monotonic() + 2.0
        while True:
            if not any(_ports_listening((port,)) for port in ports):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
    return _wait_until_unreachable(ports)


def _stop_and_collect(bridge: _RunningBridge, signum: int, ports: tuple[int, ...]) -> _Observation:
    bridge.process.send_signal(signum)
    try:
        exit_code = bridge.process.wait(timeout=_EXIT_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        _kill_group(bridge.pid)
        bridge.process.wait(timeout=5)
        raise AssertionError(
            f"signal {signum} did not exit within {_EXIT_TIMEOUT_S}s\n{_tail(bridge.log.text())}"
        ) from exc
    bridge.reader.join(timeout=2)
    return _Observation(
        exit_code=exit_code,
        listening_sockets_closed=_wait_until_quiet(ports),
        process_reaped=not _group_alive(bridge.pid),
        successor_can_bind=False,
        leftover_asyncio_tasks=_parse_reclaim(bridge.log.text()),
    )


def _restart(tmp_path: Path, grpc_port: int, metrics_port: int) -> None:
    """Start a successor on the same ports and stop it with SIGTERM."""

    ports = (grpc_port, metrics_port)
    with _bridge(tmp_path, grpc_port, metrics_port) as bridge:
        _wait_until_serving(bridge, grpc_port, metrics_port)
        observed = _stop_and_collect(bridge, signal.SIGTERM, ports)
        log = bridge.log.text()
    if observed != _Observation(
        exit_code=0,
        listening_sockets_closed=True,
        process_reaped=True,
        successor_can_bind=False,
        leftover_asyncio_tasks=(),
    ):
        raise AssertionError(f"successor did not reclaim {observed}\n{_tail(log)}")


def _observe(fault: ProcessExitFault, tmp_path: Path) -> _Observation:
    grpc_port, metrics_port = _free_ports(2)
    ports = (grpc_port, metrics_port)
    with _bridge(tmp_path, grpc_port, metrics_port) as bridge:
        _wait_until_serving(bridge, grpc_port, metrics_port)
        observed = _stop_and_collect(bridge, fault.signum, ports)
        log = bridge.log.text()
    successor = False
    if fault.successor_can_bind:
        _restart(tmp_path, grpc_port, metrics_port)
        successor = True
    result = _Observation(
        exit_code=observed.exit_code,
        listening_sockets_closed=observed.listening_sockets_closed,
        process_reaped=observed.process_reaped,
        successor_can_bind=successor,
        leftover_asyncio_tasks=observed.leftover_asyncio_tasks,
    )
    expected = _Observation(
        exit_code=fault.exit_code,
        listening_sockets_closed=fault.listening_sockets_closed,
        process_reaped=fault.process_reaped,
        successor_can_bind=fault.successor_can_bind,
        leftover_asyncio_tasks=fault.leftover_asyncio_tasks,
    )
    assert result == expected, f"{fault.name}\nobserved={result}\nexpected={expected}\n{_tail(log)}"
    return result


def test_fault_table_lists_term_then_kill() -> None:
    assert [fault.name for fault in PROCESS_EXIT_FAULTS] == ["sigterm", "sigkill"]
    assert [fault.signum for fault in PROCESS_EXIT_FAULTS] == [signal.SIGTERM, signal.SIGKILL]
    assert PROCESS_EXIT_FAULTS[0].leftover_asyncio_tasks == ()
    assert PROCESS_EXIT_FAULTS[1].leftover_asyncio_tasks is None


def test_sigterm_bit_is_signal_15() -> None:
    assert _sigterm_caught(1 << (signal.SIGTERM - 1))
    assert _sigterm_caught(0x4000 | 0x1)
    assert not _sigterm_caught(0)


def test_listen_port_parser_reads_hex_listen_rows() -> None:
    text = (
        "  sl  local_address rem_address   st\n"
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000\n"
        "   1: 0100007F:1F91 00000000:0000 01 00000000:00000000\n"
    )
    assert _listen_ports_from(text) == {8080}


def test_child_env_is_offline_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("MEDIA_BRIDGE_SESSION_FACTORY", "services.agent.src.agent:build")
    monkeypatch.setenv("MEDIA_BRIDGE_MTLS", "true")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")
    env = _child_environ(grpc_port=9, metrics_port=10)
    assert env["ENVIRONMENT"] == "development"
    assert env["OFFLINE_MOCK"] == "true"
    assert env["MEDIA_BRIDGE_GRPC_ENABLED"] == "true"
    assert env["MEDIA_BRIDGE_MTLS"] == "false"
    assert env["MEDIA_BRIDGE_GRPC_ADDR"] == "127.0.0.1:9"
    assert env["PROMETHEUS_PORT"] == "10"
    assert env["MEDIA_BRIDGE_SESSION_FACTORY"] == ""
    assert env["MEDIA_BRIDGE_PROVIDER_FACTORY"] == ""
    assert env["MEDIA_BRIDGE_RUNTIME_FACTORY"] == ""
    assert "DASHSCOPE_API_KEY" not in env
    assert "secret" not in env.values()


@pytest.mark.parametrize("fault", PROCESS_EXIT_FAULTS, ids=lambda fault: fault.name)
def test_bridge_process_exit_reclaims(fault: ProcessExitFault, tmp_path: Path) -> None:
    _observe(fault, tmp_path)


def test_offline_trend_sample_prints_two_rows() -> None:
    completed = subprocess.run(
        [sys.executable, str(_TREND), "--samples", "2", "--interval-s", "0"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [row["index"] for row in rows] == [0, 1]
    for row in rows:
        assert row["rss_bytes"] > 0
        # None means the platform, or a sandbox hiding /proc/<pid>/fd, could not
        # count sockets; a number is a real measurement and may be zero.
        assert row["open_sockets"] is None or row["open_sockets"] >= 0
        assert isinstance(row["sampled_at"], float)


def test_offline_trend_sample_refuses_a_soak() -> None:
    completed = subprocess.run(
        [sys.executable, str(_TREND), "--samples", "4"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "samples must be between 1 and 3" in completed.stderr
