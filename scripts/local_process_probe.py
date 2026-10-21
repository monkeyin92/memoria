"""Read local process facts without /proc, so the release gates run on a Mac.

Linux keeps the kernel interfaces CI already reads: ``/proc/<pid>/status`` for
RSS and the caught-signal mask, and ``/proc/<pid>/fd`` plus ``/proc/net/tcp``
for the ESTABLISHED TCP connections the pid holds.

macOS has no /proc, no lsof, and this repository has no psutil dependency.
RSS and liveness come from libproc (``proc_pidinfo``) and the POSIX ``kill``
probe. ESTABLISHED TCP counting has no lsof-free sampler here yet, so it
raises :class:`UnreadableProcess` instead of reporting a zero that would read
as a healthy measurement. A pid whose ``/proc`` entries are hidden from this
user (``hidepid``, or a CI sandbox) is unreadable for the same reason.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

_PROC = Path("/proc")
_PROC_PIDTASKINFO = 4
# Darwin struct proc_taskinfo is 96 bytes and rejects a shorter buffer at any
# EINVAL; resident_size follows virtual_size at byte 24.
_PROC_TASKINFO_SIZE = 96
_PROC_TASKINFO_RSS_OFFSET = 24
_PROC_TASKINFO_RSS_END = 32


class UnreadableProcess(RuntimeError):
    """The pid exists but this platform cannot answer, or cannot read it."""


def procfs_available() -> bool:
    return (_PROC / "self" / "status").is_file()


def _proc_status_field(pid: int, prefix: str) -> str | None:
    try:
        text = (_PROC / str(pid) / "status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split()[1]
    return None


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def caught_signals(pid: int) -> int | None:
    """Return the caught-signal mask, or None when gone or unreadable.

    Darwin reports 0 for a live pid because it has no portable signal-mask
    read; callers that need the SIGTERM bit must use their own observation.
    """

    sigcgt = _proc_status_field(pid, "SigCgt:")
    if sigcgt is not None:
        return int(sigcgt, 16)
    if sys.platform == "darwin":
        return 0 if process_exists(pid) else None
    return None


def rss_bytes(pid: int) -> int | None:
    """Return the resident set size, or None when gone or unreadable."""

    vm_rss_kb = _proc_status_field(pid, "VmRSS:")
    if vm_rss_kb is not None:
        return int(vm_rss_kb) * 1024
    if sys.platform != "darwin":
        return None
    return _darwin_rss_bytes(pid)


def _darwin_libproc() -> ctypes.CDLL:
    library_name = ctypes.util.find_library("proc")
    if library_name is None:
        raise UnreadableProcess("libproc is unavailable on this platform")
    return ctypes.CDLL(library_name, use_errno=True)


def _darwin_rss_bytes(pid: int) -> int | None:
    if not process_exists(pid):
        return None
    info = ctypes.create_string_buffer(_PROC_TASKINFO_SIZE)
    used: int = _darwin_libproc().proc_pidinfo(
        pid, _PROC_PIDTASKINFO, 0, info, _PROC_TASKINFO_SIZE
    )
    if used <= 0:
        if not process_exists(pid):
            return None
        raise UnreadableProcess(f"cannot read task info of process {pid}")
    return int.from_bytes(
        info.raw[_PROC_TASKINFO_RSS_OFFSET:_PROC_TASKINFO_RSS_END], sys.byteorder
    )


def _endpoint_port(value: str) -> int:
    return int(value.rpartition(":")[2], 16)


def _proc_tcp_inodes(text: str) -> set[int]:
    inodes: set[int] = set()
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 10 and _endpoint_port(fields[1]) and int(fields[9]) > 0:
            inodes.add(int(fields[9]))
    return inodes


def established_tcp_count(pid: int) -> int | None:
    """Return the pid's ESTABLISHED TCP connections, or None when unmeasurable."""

    if not procfs_available():
        if process_exists(pid):
            return None
        raise UnreadableProcess(f"process {pid} does not exist")
    try:
        targets = [os.readlink(entry) for entry in (_PROC / str(pid) / "fd").iterdir()]
    except OSError as exc:
        if process_exists(pid):
            raise UnreadableProcess(f"cannot list /proc/{pid}/fd: {exc.strerror}") from exc
        return 0
    table: set[int] = set()
    for name in ("tcp", "tcp6"):
        try:
            text = (_PROC / "net" / name).read_text(encoding="ascii")
        except OSError:
            continue
        table.update(_proc_tcp_inodes(text))
    return sum(
        1 for target in targets if target.startswith("socket:[") and int(target[8:-1]) in table
    )


__all__ = [
    "UnreadableProcess",
    "caught_signals",
    "established_tcp_count",
    "procfs_available",
    "process_exists",
    "rss_bytes",
]
