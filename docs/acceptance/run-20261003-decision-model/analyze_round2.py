import json
import statistics
import sys
from collections import defaultdict

rows = [json.loads(x) for x in open(sys.argv[1], encoding="utf-8") if x.strip()]
rows = [r for r in rows if r.get("kind") == "r"]
names = {"ds": "DeepSeek flash (now)", "dm_v1": "decision model v1 (yes/no)", "dm_v2": "decision model v2 (categories)"}


def decide(r, task, thr):
    if r["cfg"] == "ds":
        return (r["raw_live"] == "NEEDS_LIVE_LOOKUP") if task == "a" else (r["raw_close"] == "END_SESSION")
    p = r["p_live"] if task == "a" else r["p_close"]
    return None if p is None else p >= thr


def conf(rs, task, thr):
    tp = fp = fn = tn = bad = 0
    for r in rs:
        d = decide(r, task, thr)
        if d is None:
            bad += 1
            continue
        y = bool(r[task])
        tp += d and y
        fp += d and not y
        fn += (not d) and y
        tn += (not d) and not y
    n = tp + fp + fn + tn
    return dict(n=n, acc=(tp + tn) / n if n else float("nan"), tp=tp, fp=fp, fn=fn, tn=tn, bad=bad)


def auc(rs, task):
    key = "p_live" if task == "a" else "p_close"
    pos = [r[key] for r in rs if r[task] and r.get(key) is not None]
    neg = [r[key] for r in rs if not r[task] and r.get(key) is not None]
    return sum((p > q) + 0.5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg)) if pos and neg else float("nan")


by = defaultdict(list)
for r in rows:
    by[(r["set"], r["cfg"])].append(r)

for setname in ("dev", "held"):
    print(f"\n================ {setname.upper()} set ================")
    for task, label in (("a", "live-lookup"), ("b", "close")):
        print(f"\n--- {label}")
        print(f"{'config':<34}{'thr':>5}{'acc':>7}{'TP':>4}{'FP':>4}{'FN':>4}{'TN':>4}   AUC")
        for cfg in ("ds", "dm_v1", "dm_v2"):
            rs = by[(setname, cfg)]
            if cfg == "ds":
                c = conf(rs, task, 0.5)
                print(f"{names[cfg]:<34}{'-':>5}{c['acc']:>7.3f}{c['tp']:>4}{c['fp']:>4}{c['fn']:>4}{c['tn']:>4}")
                continue
            a = auc(rs, task)
            for thr in (0.5, 0.7, 0.9):
                c = conf(rs, task, thr)
                print(f"{names[cfg]:<34}{thr:>5.1f}{c['acc']:>7.3f}{c['tp']:>4}{c['fp']:>4}{c['fn']:>4}{c['tn']:>4}   {a if thr == 0.5 else ''}" if thr != 0.5 else
                      f"{names[cfg]:<34}{thr:>5.1f}{c['acc']:>7.3f}{c['tp']:>4}{c['fp']:>4}{c['fn']:>4}{c['tn']:>4}   {a:.4f}")

print("\n================ HELD-OUT errors (decision at 0.5) ================")
for task, label in (("a", "live-lookup"), ("b", "close")):
    for cfg in ("ds", "dm_v1", "dm_v2"):
        errs = []
        for r in by[("held", cfg)]:
            d = decide(r, task, 0.5)
            if d is None or bool(d) != bool(r[task]):
                p = "" if cfg == "ds" else f" p={(r['p_live'] if task == 'a' else r['p_close'])}"
                errs.append(f"{r['text']} -> {'Y' if d else 'N'}{p} (label {'Y' if r[task] else 'N'}{', hard' if r['hard'] else ''})")
        print(f"[{label}] {names[cfg]}: {len(errs)} error(s)")
        for e in errs[:10]:
            print("     ", e)

print("\n================ latency (cold, ms) ================")
for cfg in ("ds", "dm_v1", "dm_v2"):
    v = [r["pair_ms"] for r in rows if r["cfg"] == cfg]
    v.sort()
    print(f"{names[cfg]:<34} n={len(v):>3} p50={statistics.median(v):.0f} p90={v[int(0.9 * (len(v) - 1))]:.0f} max={v[-1]:.0f}")
bad = [r for r in rows if r["cfg"] != "ds" and (r.get("status") != 200)]
print("decision-model non-200:", len(bad))
