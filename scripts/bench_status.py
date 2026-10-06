#!/usr/bin/env python3
"""Read the `status` line of a bench build (TODOLIST M-2) from the serial log, and what two of them say.

A bench image (`firmware/esp32/scripts/build.sh --bench`) answers the USB command `status` with one log line
(memoria_mascot_status.h):

    I (123456) MemoriaBench: status up_ms=123456 phase=idle mood=neutral frame=default_blink screen_off=0 ...

What the mascot is doing right now, and totals since boot of what its frames have cost. A total says little;
two lines taken a while apart give the rates in between, which is what a bench run compares:

    python scripts/bench_status.py outputs/serial/robot.log            # the last line, one field per row
    python scripts/bench_status.py outputs/serial/robot.log --rates    # the last two lines and what lies between

`voice_soak_serial_command.py status --log ... [--after SECONDS]` sends the request and runs this in one go.
Standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

# The fields of one line in the order the robot prints them; a test compares this with the C++ format string.
FIELDS = (
    "up_ms",
    "phase",
    "mood",
    "frame",
    "screen_off",
    "sleeping",
    "captioned",
    "frames",
    "drawn",
    "render_us",
    "render_max_us",
    "busy_us",
    "px",
    "composed_px",
    "extra_ms",
    "heap_free",
    "psram_free",
    "anim_stack_free",
)
_NAMES = ("phase", "mood", "frame")
# Counters that only ever grow: a smaller value in the later line means the robot restarted in between.
_TOTALS = ("frames", "drawn", "render_us", "busy_us", "px", "composed_px")
_U32 = 1 << 32

# The robot's answers to a request it does not take (memoria_esp_vocat.cc): a product image has no `status`.
IGNORED_COMMAND = "usb command ignored (not a command)"
_STATUS_IGNORED = re.compile(r"usb status ignored reason=\w+")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LINE = re.compile(r"MemoriaBench: status (?P<fields>up_ms=.*)")
_STAMP = re.compile(r"^\d\d:\d\d:\d\d\.\d{3}")
_FIELD = re.compile(r"(\w+)=(\S+)")


class StatusError(RuntimeError):
    """No usable status line, or two lines that cannot be compared."""


Status = dict[str, int | str]


def parse_status(line: str) -> Status | None:
    """The fields of a status line (numbers as int, `phase`/`mood`/`frame` as text, `at` the logger's time
    stamp when there is one), or None when `line` is not a complete status line: a line cut by other output
    is not guessed at."""
    plain = _ANSI.sub("", line).rstrip()
    match = _LINE.search(plain)
    if match is None:
        return None
    fields = {key: value for key, value in _FIELD.findall(match["fields"])}
    if tuple(fields) != FIELDS:
        return None
    status: Status = {}
    for key in FIELDS:
        value = fields[key]
        if key in _NAMES:
            status[key] = value
        elif value.isdigit():
            status[key] = int(value)
        else:
            return None
    stamp = _STAMP.match(plain)
    status["at"] = stamp[0] if stamp else ""
    return status


def collect(text: str) -> list[Status]:
    """Every complete status line in `text`, in order."""
    return [status for line in text.splitlines() if (status := parse_status(line)) is not None]


def read_text(path: Path, offset: int = 0) -> str:
    """The log from byte `offset` on; a log that does not exist yet reads as empty."""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def wait_for_status(
    path: Path,
    offset: int,
    timeout: float,
    *,
    poll: float = 0.1,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Status:
    """Wait for the status line a `status` request sent at log position `offset` produces. The robot
    answers within one animation frame, so the timeout is for a robot that is not there or not a bench
    image: its refusal ends the wait at once."""
    deadline = clock() + timeout
    while True:
        text = read_text(path, offset)
        found = collect(text)
        if found:
            return found[0]
        refused = _STATUS_IGNORED.search(text)
        if refused is not None:
            raise StatusError(f"the robot refused `status`: {refused[0]}")
        if IGNORED_COMMAND in text:
            raise StatusError(
                "the robot ignored `status` (usb command ignored): it is not running a bench image; "
                "build and flash one with firmware/esp32/scripts/build.sh --bench and flash.sh --bench"
            )
        if clock() >= deadline:
            raise StatusError(f"no status line in {timeout:.0f} s")
        sleep(poll)


def rates(first: Status, second: Status) -> dict[str, float | int | None]:
    """What the mascot's frames cost between two status lines, `first` the earlier one."""
    elapsed_ms = (int(second["up_ms"]) - int(first["up_ms"])) % _U32
    if elapsed_ms == 0:
        raise StatusError("the two lines carry the same up_ms: there is no time between them")
    delta = {key: int(second[key]) - int(first[key]) for key in _TOTALS}
    # The 32-bit ones (frames, drawn) take five years to wrap at 25 fps, so a smaller number is a restart.
    if any(value < 0 for value in delta.values()):
        raise StatusError("a counter went backwards: the robot restarted between the two lines")
    seconds = elapsed_ms / 1000
    drawn = delta["drawn"]
    return {
        "seconds": seconds,
        "frames_per_s": delta["frames"] / seconds,
        "drawn_per_s": drawn / seconds,
        "render_ms_per_drawn": delta["render_us"] / drawn / 1000 if drawn else None,
        "busy_pct": delta["busy_us"] / (elapsed_ms * 1000) * 100,
        "px_per_drawn": delta["px"] // drawn if drawn else None,
        "composed_px_per_drawn": delta["composed_px"] // drawn if drawn else None,
        # Not rates: the later line's own reading (the longest render is since boot, the stack low-water mark too).
        "render_max_ms_since_boot": int(second["render_max_us"]) / 1000,
        "extra_ms": int(second["extra_ms"]),
        "heap_free": int(second["heap_free"]),
        "psram_free": int(second["psram_free"]),
        "anim_stack_free": int(second["anim_stack_free"]),
    }


def format_status(status: Status) -> str:
    return "\n".join(f"{key:>16}  {status[key]}" for key in FIELDS)


def format_rates(first: Status, second: Status) -> str:
    r = rates(first, second)

    def per_drawn(value: float | int | None, unit: str) -> str:
        return "no frame was drawn" if value is None else f"{value:,.1f} {unit}"

    return "\n".join(
        [
            f"between up_ms {first['up_ms']} and {second['up_ms']} ({r['seconds']:.1f} s), "
            f"phase {first['phase']} -> {second['phase']}:",
            f"  loop      {r['frames_per_s']:.1f} /s   drawn {r['drawn_per_s']:.1f} /s",
            f"  render    {per_drawn(r['render_ms_per_drawn'], 'ms per drawn frame')}"
            f"   (longest since boot {r['render_max_ms_since_boot']:.1f} ms)",
            f"  busy      {r['busy_pct']:.1f} % of the animation task's time",
            f"  pixels    {per_drawn(r['px_per_drawn'], 'per drawn frame')}"
            f", {per_drawn(r['composed_px_per_drawn'], 'composed')}",
            f"  pacer     +{r['extra_ms']} ms   heap {r['heap_free']:,}   psram {r['psram_free']:,}"
            f"   animation stack free {r['anim_stack_free']:,}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "log", type=Path, help="serial log with the status lines (the logger's output)"
    )
    parser.add_argument(
        "--rates", action="store_true", help="the last two lines and what lies between them"
    )
    args = parser.parse_args(argv)
    lines = collect(read_text(args.log))
    need = 2 if args.rates else 1
    if len(lines) < need:
        print(
            f"error: {len(lines)} status line(s) in {args.log}, need {need}"
            " (send `status` with scripts/voice_soak_serial_command.py)",
            file=sys.stderr,
        )
        return 1
    try:
        print(format_rates(lines[-2], lines[-1]) if args.rates else format_status(lines[-1]))
    except StatusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
