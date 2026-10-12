#!/usr/bin/env python3
"""Print a few offline memory and open-socket samples for one process.

P2-04 still needs a predefined longsoak window (memory, connections, latency).
This script is only a stub: at most three samples, at most 0.2s apart. It does
not start a bridge, open a device socket, measure latency, or talk to FunASR.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_MAX_SAMPLES = 3
_MAX_INTERVAL_S = 0.2


def sample_process(pid: int) -> dict[str, int]:
    """Read VmRSS and the count of socket fds for ``pid`` from /proc."""

    status_path = Path(f"/proc/{pid}/status")
    try:
        status = status_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read {status_path}") from exc
    rss_kb: int | None = None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            rss_kb = int(line.split()[1])
            break
    if rss_kb is None:
        raise RuntimeError(f"{status_path} has no VmRSS")
    sockets = 0
    for entry in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:"):
            sockets += 1
    return {"pid": pid, "rss_bytes": rss_kb * 1024, "open_sockets": sockets}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pid", type=int, default=os.getpid())
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--interval-s", type=float, default=0.0)
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= _MAX_SAMPLES:
        print(f"samples must be between 1 and {_MAX_SAMPLES}", file=sys.stderr)
        return 2
    if not 0 <= args.interval_s <= _MAX_INTERVAL_S:
        print(f"interval-s must be between 0 and {_MAX_INTERVAL_S}", file=sys.stderr)
        return 2
    for index in range(args.samples):
        if index:
            time.sleep(args.interval_s)
        row = sample_process(args.pid)
        print(
            json.dumps({**row, "index": index, "sampled_at": time.time()}),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
