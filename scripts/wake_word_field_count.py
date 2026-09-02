#!/usr/bin/env python3
"""Count real device wake events from production logs for HANDOFF stage 4.

Stage 4 asks for missed-wake and false-wake counts per wake word. The operator
can only report what they said; whether the board actually woke is decided by
its local KWS and is observable server-side: every wake opens a fresh media
session, which logs one ``media PCM tap opened ... stream_epoch=N`` line with a
unique session id.

So: the operator reports attempts, this script reports actual wakes, and the
difference is the missed-wake count. Wakes with no attempt behind them are
false wakes. Neither number depends on the operator watching an LED.

This reads logs only. It never writes to production and never changes a session.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass

BRIDGE_CONTAINER = "memoria-voice-core-media-bridge-1"
EDGE_CONTAINER = "memoria-media-edge-1"

_TAP_OPENED = re.compile(
    r"media PCM tap opened session=(?P<session>[0-9a-f-]{36}) stream_epoch=(?P<epoch>\d+)"
)
_EDGE_CLOSE = re.compile(
    r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) .*"
    r"projected conversation close session=(?P<session>[0-9a-f-]{36}) "
    r"device=(?P<device>\S+) epoch=(?P<epoch>\d+) reason=(?P<reason>\S+)"
)


@dataclass(frozen=True)
class WakeEvent:
    session_id: str
    stream_epoch: int


@dataclass(frozen=True)
class CloseEvent:
    at_utc: str
    session_id: str
    device_id: str
    stream_epoch: int
    reason: str


def _docker_logs(remote: str, container: str, since: str) -> str:
    """Return container logs. Read-only; --since bounds the window."""

    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            remote,
            f"docker logs {container} --since {since} 2>&1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cannot read {container} logs on {remote}: {completed.stderr.strip()}"
        )
    return completed.stdout


def collect(remote: str, since: str) -> tuple[list[WakeEvent], list[CloseEvent]]:
    wakes: list[WakeEvent] = []
    seen: set[str] = set()
    for line in _docker_logs(remote, BRIDGE_CONTAINER, since).splitlines():
        match = _TAP_OPENED.search(line)
        if match is None or match["session"] in seen:
            continue
        seen.add(match["session"])
        wakes.append(WakeEvent(match["session"], int(match["epoch"])))

    closes: list[CloseEvent] = []
    for line in _docker_logs(remote, EDGE_CONTAINER, since).splitlines():
        match = _EDGE_CLOSE.search(line)
        if match is None:
            continue
        closes.append(
            CloseEvent(
                at_utc=match["ts"],
                session_id=match["session"],
                device_id=match["device"],
                stream_epoch=int(match["epoch"]),
                reason=match["reason"],
            )
        )
    return wakes, closes


def cmd_count(args: argparse.Namespace) -> int:
    wakes, closes = collect(args.remote, args.since)
    print(f"window: last {args.since} (UTC log timestamps)")
    print(f"wake_word_id: {args.wake_word_id}")
    print(f"attempts_reported_by_operator: {args.attempts}")
    print(f"actual_wakes_observed: {len(wakes)}")

    if args.attempts is not None:
        missed = args.attempts - len(wakes)
        if missed >= 0:
            print(f"missed_wakes: {missed}")
            print("false_wakes: 0 (no wake exceeded the reported attempts)")
        else:
            print("missed_wakes: 0")
            print(f"false_wakes: {-missed}  <-- more wakes than attempts")
        print(
            "NOTE: false wakes during a silent observation window are only "
            "trustworthy if nobody spoke the wake word in that window."
        )

    print("\nsessions opened:")
    for event in wakes:
        print(f"  epoch={event.stream_epoch} session={event.session_id}")
    if not wakes:
        print("  (none — if you did speak, the board never woke)")

    print("\nconversation closes:")
    for close in closes:
        print(
            f"  {close.at_utc} epoch={close.stream_epoch} "
            f"reason={close.reason} device={close.device_id}"
        )
    if not closes:
        print("  (none)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default="memoria-prod")
    parser.add_argument(
        "--since",
        default="20m",
        help="docker logs --since window, e.g. 20m or 2h (default: 20m)",
    )
    parser.add_argument(
        "--wake-word-id",
        default="mo_li",
        choices=["mo_li", "mei_mo_li_ya", "custom"],
        help="which catalog word this round tested",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=None,
        help="how many times the operator actually spoke the wake word",
    )
    parser.set_defaults(func=cmd_count)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
