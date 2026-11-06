"""Server minus Mac clock, via `ssh REMOTE date`. Prints one JSON line: offset_s, error_s (half the round trip)."""
import argparse
import json
import subprocess
import time


def measure(cmd: list[str], tries: int = 15) -> dict:
    best = None
    for _ in range(tries):
        t0 = time.time()
        server = float(subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=15).stdout.strip())
        t1 = time.time()
        row = {"offset_s": round(server - (t0 + t1) / 2, 4), "error_s": round((t1 - t0) / 2, 4)}
        if best is None or row["error_s"] < best["error_s"]:
            best = row
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("--remote", default="memoria-prod")
    ap.add_argument("--cmd", default=None, help="override, for offline tests")
    a = ap.parse_args()
    cmd = a.cmd.split() if a.cmd else ["ssh", "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/memoria-clock-%r@%h", "-o", "ControlPersist=60", a.remote, "date +%s.%N"]
    print(json.dumps({"label": a.label, **measure(cmd)}))


if __name__ == "__main__":
    main()
