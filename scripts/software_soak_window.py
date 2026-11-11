"""Run the real Edge HTTP/WSS test server for 900 seconds with a synthetic core.

Build first with go test -C services/media_edge -c -race -o /tmp/edge.test.
This window does not run the production Python bridge or Control service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from scripts.local_process_probe import rss_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    (args.output / "config.json").write_text(
        json.dumps(
            {
                "candidate": candidate,
                "duration_s": 900,
                "interval_s": 5,
                "bridge": "synthetic deviceTestCore",
                "control": "not running",
                "binary": args.binary,
                "binary_sha256": hashlib.sha256(Path(args.binary).read_bytes()).hexdigest(),
                "race": True,
                "dirty": subprocess.check_output(["git", "status", "--porcelain"], text=True),
            },
            indent=2,
        )
    )
    with (args.output / "edge.log").open("w") as log:
        process = subprocess.Popen(
            [args.binary, "-test.run=^TestSoftwareSoakWindow$", "-test.v", "-test.timeout=17m"],
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "MEMORIA_SOFTWARE_SOAK": "900"},
        )
        began = time.monotonic()
        try:
            with (args.output / "process.jsonl").open("w") as series:
                while process.poll() is None:
                    descriptors = subprocess.run(
                        ["/usr/sbin/lsof", "-nP", "-p", str(process.pid), "-FftT"],
                        capture_output=True,
                        text=True,
                    )
                    rows = descriptors.stdout.splitlines()
                    json.dump(
                        {
                            "elapsed_s": time.monotonic() - began,
                            "pid": process.pid,
                            "rss_bytes": rss_bytes(process.pid),
                            "fd_count": sum(
                                row.startswith("f") and row[1:].isdigit() for row in rows
                            )
                            if descriptors.returncode == 0
                            else None,
                            "established_tcp": rows.count("TST=ESTABLISHED")
                            if descriptors.returncode == 0
                            else None,
                        },
                        series,
                    )
                    series.write("\n")
                    series.flush()
                    time.sleep(5)
            elapsed = time.monotonic() - began
            result = {
                "exit_code": process.returncode,
                "elapsed_s": elapsed,
                "window_completed": process.returncode == 0 and elapsed >= 900,
            }
            (args.output / "result.json").write_text(json.dumps(result, indent=2))
            return 0 if result["window_completed"] else 1
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
