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
# The profile line that follows each status line (memoria_mascot_status.h, BenchProfileLine): microseconds
# summed over the sampled renders, in the order the robot prints them.
PROFILE_FIELDS = (
    "renders",
    "sampled",
    "total_us",
    "actor_us",
    "rect_us",
    "touch_us",
    "rows",
    "copy_in_us",
    "shadow_us",
    "sprite_us",
    "ring_us",
    "copy_out_us",
)
PROFILE_TOTALS = PROFILE_FIELDS  # every one of them only grows
# The disp line (memoria_mascot_status.h, BenchLvglLine): where taskLVGL's refreshes spend their wall-clock time.
LVGL_FIELDS = (
    "refreshes",
    "refresh_us",
    "flushes",
    "flush_us",
    "flush_px",
    "waits",
    "wait_us",
)
_STAGES = ("copy_in", "shadow", "sprite", "ring", "copy_out")
_NAMES = ("phase", "mood", "frame")
# Counters that only ever grow: a smaller value in the later line means the robot restarted in between.
_TOTALS = ("frames", "drawn", "render_us", "busy_us", "px", "composed_px")
_U32 = 1 << 32

# The robot's answers to a request it does not take (memoria_esp_vocat.cc): a product image has no `status`.
IGNORED_COMMAND = "usb command ignored (not a command)"
_STATUS_IGNORED = re.compile(r"usb status ignored reason=\w+")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LINE = re.compile(r"MemoriaBench: status (?P<fields>up_ms=.*)")
_PROFILE_LINE = re.compile(r"MemoriaBench: profile (?P<fields>renders=.*)")
_TASKS_LINE = re.compile(r"MemoriaBench: tasks (?P<fields>count=.*)")
_LVGL_LINE = re.compile(r"MemoriaBench: disp (?P<fields>refreshes=.*)")
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


def parse_profile(line: str) -> Status | None:
    """The sums of a profile line as int (`at` the logger's time stamp when there is one), or None when `line` is
    not a complete profile line: a line cut by other output is not guessed at."""
    plain = _ANSI.sub("", line).rstrip()
    match = _PROFILE_LINE.search(plain)
    if match is None:
        return None
    fields = {key: value for key, value in _FIELD.findall(match["fields"])}
    if tuple(fields) != PROFILE_FIELDS or not all(value.isdigit() for value in fields.values()):
        return None
    profile: Status = {key: int(fields[key]) for key in PROFILE_FIELDS}
    stamp = _STAMP.match(plain)
    profile["at"] = stamp[0] if stamp else ""
    return profile


def parse_lvgl(line: str) -> Status | None:
    """The sums of a disp line as int (`at` the logger's time stamp when there is one), or None when `line` is not
    a complete disp line: a line cut by other output is not guessed at."""
    plain = _ANSI.sub("", line).rstrip()
    match = _LVGL_LINE.search(plain)
    if match is None:
        return None
    fields = {key: value for key, value in _FIELD.findall(match["fields"])}
    if tuple(fields) != LVGL_FIELDS or not all(value.isdigit() for value in fields.values()):
        return None
    lvgl: Status = {key: int(fields[key]) for key in LVGL_FIELDS}
    stamp = _STAMP.match(plain)
    lvgl["at"] = stamp[0] if stamp else ""
    return lvgl


def lvgl_rates(first: Status, second: Status) -> dict[str, float | int]:
    """What one refresh costs between two disp lines, `first` the earlier one, in milliseconds of wall clock.
    The flush callbacks and the waits for the previous flush are taken out of the refresh; what is left is
    rendering. The three spans are measured separately on the robot, so the remainder can come out slightly
    off (a wait is counted in the refresh too); it is not clamped."""
    delta = {key: int(second[key]) - int(first[key]) for key in LVGL_FIELDS}
    if any(value < 0 for value in delta.values()):
        raise StatusError("a counter went backwards: the robot restarted between the two lines")
    refreshes = delta["refreshes"]
    if refreshes == 0:
        raise StatusError("no refresh happened between the two lines")
    refresh_ms = delta["refresh_us"] / refreshes / 1000
    flush_ms = delta["flush_us"] / refreshes / 1000
    wait_ms = delta["wait_us"] / refreshes / 1000
    return {
        "refreshes": refreshes,
        "refresh_ms": refresh_ms,
        "flush_ms": flush_ms,
        "wait_ms": wait_ms,
        "render_ms": refresh_ms - flush_ms - wait_ms,
        "flushes_per_refresh": delta["flushes"] / refreshes,
        "px_per_refresh": delta["flush_px"] / refreshes,
    }


def collect_profiles(text: str) -> list[Status]:
    """Every complete profile line in `text`, in order."""
    return [p for line in text.splitlines() if (p := parse_profile(line)) is not None]


def profile_rates(first: Status, second: Status) -> dict[str, object]:
    """Where a sampled render spends its time between two profile lines, `first` the earlier one. Per sampled
    render for the whole Render, per composed row for the stages; the shares are of the five row stages
    together, so they add up to 1."""
    delta = {key: int(second[key]) - int(first[key]) for key in PROFILE_TOTALS}
    if any(value < 0 for value in delta.values()):
        raise StatusError("a counter went backwards: the robot restarted between the two lines")
    sampled, rows = delta["sampled"], delta["rows"]
    if sampled == 0:
        raise StatusError("no render was sampled between the two lines")
    stage_total = sum(delta[f"{name}_us"] for name in _STAGES)
    out: dict[str, object] = {
        "sampled": sampled,
        "total_us_per_render": delta["total_us"] / sampled,
        "actor_us_per_render": delta["actor_us"] / sampled,
        "rect_us_per_render": delta["rect_us"] / sampled,
        "touch_us_per_render": delta["touch_us"] / sampled,
        "rows_per_render": rows / sampled,
    }
    for name in _STAGES:
        out[f"{name}_us_per_row"] = delta[f"{name}_us"] / rows if rows else None
    out["share_of_rect"] = {
        name: (delta[f"{name}_us"] / stage_total if stage_total else 0.0) for name in _STAGES
    }
    return out


# FreeRTOS run-time counters are 32 bits in this build (CONFIG_FREERTOS_RUN_TIME_COUNTER_TYPE_U32): they wrap
# about every 71 minutes, so a difference is taken modulo 2**32.
_COUNTER_WRAP = 1 << 32


def parse_tasks(line: str) -> dict[str, object] | None:
    """A `tasks` line as {"count", "up_us", "at", "tasks": {name: (core, priority, runtime_us)}}, or None when
    `line` is not a complete tasks line. A task name appears once; a repeated name makes the line unusable."""
    plain = _ANSI.sub("", line).rstrip()
    match = _TASKS_LINE.search(plain)
    if match is None:
        return None
    words = match["fields"].split()
    if len(words) < 2 or not words[0].startswith("count=") or not words[1].startswith("up_us="):
        return None
    count, up_us = words[0][6:], words[1][6:]
    if not (count.isdigit() and up_us.isdigit()):
        return None
    tasks: dict[str, tuple[int, int, int]] = {}
    for word in words[2:]:
        parts = word.split(":")
        if len(parts) != 4:
            return None
        name, core, priority, runtime = parts
        try:
            row = (int(core), int(priority), int(runtime))
        except ValueError:
            return None
        if not name or name in tasks or row[1] < 0 or row[2] < 0:
            return None
        tasks[name] = row
    stamp = _STAMP.match(plain)
    return {
        "count": int(count),
        "up_us": int(up_us),
        "at": stamp[0] if stamp else "",
        "tasks": tasks,
    }


def task_rates(first: dict[str, object], second: dict[str, object]) -> dict[str, object]:
    """Each task's CPU time between two tasks lines as a share of the wall time between them (1.0 = one whole
    core), and the sum per core. A task missing from either line (it started or ended in between) is left out
    and listed under `unmatched`. Only the busiest rows are printed by the robot, so a core's sum is a floor."""
    wall = int(second["up_us"]) - int(first["up_us"])  # type: ignore[call-overload]
    if wall <= 0:
        raise StatusError("the two tasks lines are not in time order")
    before, after = first["tasks"], second["tasks"]
    shares: dict[str, float] = {}
    cores: dict[str, int] = {}
    per_core: dict[int, float] = {}
    for name, (core, _priority, runtime) in after.items():  # type: ignore[attr-defined]
        if name not in before:  # type: ignore[operator]
            continue
        used = (runtime - before[name][2]) % _COUNTER_WRAP  # type: ignore[index]
        shares[name] = used / wall
        cores[name] = core
        per_core[core] = per_core.get(core, 0.0) + used / wall
    unmatched = sorted(set(before) ^ set(after))  # type: ignore[arg-type]
    return {
        "wall_us": wall,
        "share": shares,
        "core": cores,
        "per_core": per_core,
        "unmatched": unmatched,
    }


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
