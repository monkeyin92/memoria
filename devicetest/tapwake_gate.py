"""Wait gate: "session" before a run needs idle, "turn" between utterances accepts a listening window."""
import re
import time
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
              poll: float = 0.5) -> str | None:
    """Return why the gate opened, or None after `timeout`.

    session: only serial state idle opens it (cold start / between runs).
    turn: idle, or a listening window (state listening, or `listening_closed` in bridge.log after
    `offset`) opens it, so the next line can follow a reply without a forced idle.
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
                return "listening"
            if closed_since(bridge, offset):
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
