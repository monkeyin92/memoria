"""Wait gate before a USB wake: device must reach idle (listening_closed is log-only)."""
import re
import time
from pathlib import Path

CLOSED_RE = re.compile(r"listening[ _]closed")


def closed_since(bridge: Path, offset: int) -> bool:
    if not bridge.exists():
        return False
    with bridge.open("rb") as f:
        f.seek(offset)
        return CLOSED_RE.search(f.read().decode("utf-8", errors="replace")) is not None


def bridge_size(bridge: Path) -> int:
    return bridge.stat().st_size if bridge.exists() else 0


def wait_gate(get_state, bridge: Path, offset: int, timeout: float = 180.0,
              clock=time.monotonic, sleep=time.sleep, poll: float = 0.5) -> str | None:
    """Return "idle" when serial state is idle, or None after `timeout`.

    `listening_closed` in bridge.log is recorded by the caller for diagnostics only;
    it no longer releases the gate (resume_listening left the device in listening and
    caused not_idle / no_greeting cascades).
    """
    end = clock() + timeout
    while True:
        if get_state() == "idle":
            return "idle"
        if clock() >= end:
            return None
        sleep(poll)


class TimeoutStreak:
    """Counts consecutive gate timeouts; `hit` is True at the limit."""

    def __init__(self, limit: int = 3):
        self.limit, self.count = limit, 0

    def update(self, timed_out: bool) -> bool:
        self.count = self.count + 1 if timed_out else 0
        return self.count >= self.limit
