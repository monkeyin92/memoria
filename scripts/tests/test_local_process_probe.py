"""Portable process sampling for the P2-04 harness and the trend sample.

Linux reads /proc. macOS reads RSS and liveness from libproc and reports the
ESTABLISHED TCP count as unmeasured, never as a false zero. These tests build
the probes against a real live and a real reaped process so the Mac path is
checked here, not only in the Agent gate that reads it.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import local_process_probe as probe
from scripts import offline_process_trend_sample as trend

ROOT = Path(__file__).resolve().parents[2]


def _pid_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.fixture
def reaped_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=20)
    assert _pid_is_gone(process.pid)
    return process.pid


@pytest.fixture
def sleeper() -> int:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait(timeout=20)


@pytest.mark.skipif(not probe.procfs_available(), reason="Darwin reads no /proc at all")
def test_linux_reads_rss_and_the_caught_sigterm_bit(sleeper: int) -> None:
    assert probe.rss_bytes(sleeper) is not None and probe.rss_bytes(sleeper) > 0
    mask = probe.caught_signals(sleeper)
    assert mask is not None
    assert mask & (1 << (signal.SIGTERM - 1)) == 0


def test_rss_is_measured_for_a_live_process_on_every_platform(sleeper: int) -> None:
    measured = probe.rss_bytes(sleeper)

    assert measured is not None
    assert measured > 0


def test_liveness_matches_kill_zero(sleeper: int, reaped_pid: int) -> None:
    assert probe.process_exists(sleeper) is True
    assert probe.process_exists(reaped_pid) is False
    # Darwin has no portable signal-mask read; it must not invent a SIGTERM bit.
    assert probe.caught_signals(reaped_pid) is None
    if not probe.procfs_available():
        assert probe.caught_signals(sleeper) == 0


def test_rss_of_a_reaped_process_is_unknown(reaped_pid: int) -> None:
    assert probe.rss_bytes(reaped_pid) is None


def test_established_tcp_count_reads_this_processes_own_sockets() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        conn.connect(listener.getsockname())
        peer, _ = listener.accept()
        with peer:
            measured = probe.established_tcp_count(os.getpid())
    finally:
        conn.close()
        listener.close()

    if probe.procfs_available():
        assert measured is not None and measured >= 1
    else:
        assert measured is None


def test_established_tcp_count_on_a_reaped_process(reaped_pid: int) -> None:
    if probe.procfs_available():
        assert probe.established_tcp_count(reaped_pid) == 0
    else:
        with pytest.raises(probe.UnreadableProcess):
            probe.established_tcp_count(reaped_pid)


def test_trend_sample_reports_an_unmeasured_socket_count_instead_of_zero() -> None:
    row = trend.sample_process(os.getpid())

    assert row["rss_bytes"] is not None and row["rss_bytes"] > 0
    if probe.procfs_available():
        assert isinstance(row["open_sockets"], int) and row["open_sockets"] >= 0
    else:
        assert row["open_sockets"] is None


def test_trend_sample_exits_non_zero_when_rss_cannot_be_read(reaped_pid: int) -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "offline_process_trend_sample.py"),
         "--pid", str(reaped_pid)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "cannot read resident memory" in completed.stderr
