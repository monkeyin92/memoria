import json, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
jobs = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
import os
jobs = [j for j in jobs if not os.path.exists(j[0])]
def run(j):
    r = subprocess.run(["python3", "gen.py"] + j, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else "?"
with ThreadPoolExecutor(int(sys.argv[2]) if len(sys.argv) > 2 else 10) as ex:
    for line in ex.map(run, jobs): print(line, flush=True)
