#!/usr/bin/env python3
"""Pack the logs a voice_soak run already wrote. No device, no network.

The archive contains timeline.jsonl and whichever of serial.log, bridge.log,
edge.log, and control.log are present. Other files in the run directory stay
out. A sha256 sidecar is written next to the archive.

    python scripts/voice_soak_pack.py outputs/acceptance/run-<stamp>-soak
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tarfile
from pathlib import Path

LOG_NAMES = ("timeline.jsonl", "serial.log", "bridge.log", "edge.log", "control.log")


def pack_run(run: Path, dest: Path | None = None) -> Path:
    run = run.resolve()
    if not (run / "timeline.jsonl").is_file():
        raise FileNotFoundError(f"{run} has no timeline.jsonl")
    present = [name for name in LOG_NAMES if (run / name).is_file()]
    dest = (dest if dest is not None else run.parent / f"{run.name}.tar.gz").resolve()
    if dest == run or dest.is_relative_to(run):
        raise ValueError("the archive must be written outside the run directory")
    dest.parent.mkdir(parents=True, exist_ok=True)
    expected = [f"{run.name}/{name}" for name in present]
    with tarfile.open(dest, "w:gz") as archive:
        for name in present:
            archive.add(run / name, arcname=f"{run.name}/{name}")
    with tarfile.open(dest, "r:gz") as archive:
        if archive.getnames() != expected:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"packed names do not match {expected}")
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    dest.with_name(dest.name + ".sha256").write_text(f"{digest}  {dest.name}\n", encoding="utf-8")
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--dest", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        dest = pack_run(args.run, args.dest)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
