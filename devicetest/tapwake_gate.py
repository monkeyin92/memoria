"""Wait gate: "session" before a run needs idle, "turn" between utterances accepts a listening window."""
import re
import time
from collections.abc import Callable
from pathlib import Path

CLOSED_RE = re.compile(r"listening[ _]closed")

# The device stays in listening after a reply (#453 continuous conversation), so a turn gate must not
# force idle. A session gate still does: resume_listening from idle at run start caused not_idle cascades.
DEFAULT_TIMEOUT_S = {"session": 180.0, "turn": 30.0}


def closed_since(bridge: Path, offset: int) -> bool:
    if not bridge.exists():
        return False
    with bridge.open("rb") as f:
        f.seek(offset)
        return CLOSED_RE.search(f.read().decode("utf-8", errors="replace")) is not None


def bridge_size(bridge: Path) -> int:
    return bridge.stat().st_size if bridge.exists() else 0


def wait_gate(get_state, bridge: Path, offset: int, timeout: float | None = None,
              mode: str = "session", clock=time.monotonic, sleep=time.sleep,
              poll: float = 0.5, is_closed: Callable[[], bool] | None = None) -> str | None:
    """Return why the gate opened, or None after `timeout`.

    session: only serial state idle opens it (cold start / between runs).
    turn: idle, or a listening window opens it, so the next line can follow a reply without a forced idle.
    A listening window is `listening_closed` when the mic is shut, `listening` when open. `is_closed` reads
    the serial hold flag directly; without it the gate falls back to `listening_closed` in bridge.log after
    `offset` (which lags the serial stream).
    """
    if mode not in DEFAULT_TIMEOUT_S:
        raise ValueError(f"unknown gate mode: {mode}")
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_S[mode]
    end = clock() + timeout
    while True:
        state = get_state()
        if state == "idle":
            return "idle"
        if mode == "turn":
            if state == "listening":
                if is_closed is not None:
                    return "listening_closed" if is_closed() else "listening"
                if closed_since(bridge, offset):
                    return "listening_closed"
                return "listening"
            if is_closed is None and closed_since(bridge, offset):
                return "listening_closed"
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
