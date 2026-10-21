#!/usr/bin/env python3
"""Print a few offline memory and open-socket samples for one process.

P2-04 still needs a predefined longsoak window (memory, connections, latency).
This script is only a stub: at most three samples, at most 0.2s apart. It does
not start a bridge, open a device socket, measure latency, or talk to FunASR.

Sampling goes through ``scripts.local_process_probe``: /proc on Linux, and on
macOS a libproc RSS read with the socket count reported as unmeasured rather
than zero, because Darwin has no verified lsof-free ESTABLISHED count.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from scripts import local_process_probe

_MAX_SAMPLES = 3
_MAX_INTERVAL_S = 0.2


def sample_process(pid: int) -> dict[str, int | None]:
    """Read resident memory and the count of socket fds for ``pid``.

    ``open_sockets`` is None when the platform cannot count them; a zero means
    "measured, none".
    """

    rss = local_process_probe.rss_bytes(pid)
    if rss is None:
        raise RuntimeError(f"cannot read resident memory of process {pid}")
    try:
        sockets: int | None = local_process_probe.established_tcp_count(pid)
    except local_process_probe.UnreadableProcess:
        sockets = None
    return {"pid": pid, "rss_bytes": rss, "open_sockets": sockets}


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
