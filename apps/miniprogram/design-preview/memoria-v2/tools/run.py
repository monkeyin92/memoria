import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

jobs = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
jobs = [j for j in jobs if not os.path.exists(j[0])]


def run(j):
    r = subprocess.run(["python3", "gen.py"] + j, capture_output=True, text=True)
    output = (r.stdout + r.stderr).strip()
    return output.splitlines()[-1] if output else "?"


with ThreadPoolExecutor(int(sys.argv[2]) if len(sys.argv) > 2 else 10) as ex:
    for line in ex.map(run, jobs):
        print(line, flush=True)
