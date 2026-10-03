import json
import statistics
import sys
from collections import defaultdict

path = sys.argv[1] if len(sys.argv) > 1 else "full.out"
rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]
rows = [r for r in rows if r.get("kind") in ("cold", "warm")]
print(f"measurements: {len(rows)}  (cold {sum(r['kind']=='cold' for r in rows)}, warm {sum(r['kind']=='warm' for r in rows)})")


def pct(vals, p):
    s = sorted(vals)
    if not s:
        return float("nan")
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def decide(r, task, thr=0.5):
    if r["cfg"] in ("ds", "qw"):
        return (r["raw_live"] == "NEEDS_LIVE_LOOKUP") if task == "a" else (r["raw_close"] == "END_SESSION")
    p = r["p_live"] if task == "a" else r["p_close"]
    return None if p is None else p >= thr


def prob(r, task):
    return r["p_live"] if task == "a" else r["p_close"]


def metrics(rs, task, thr=0.5):
    tp = fp = fn = tn = bad = 0
    for r in rs:
        y = r[task]
        d = decide(r, task, thr)
        if d is None:
            bad += 1
            continue
        tp += d and y
        fp += d and not y
        fn += (not d) and y
        tn += (not d) and not y
    n = tp + fp + fn + tn
    acc = (tp + tn) / n if n else float("nan")
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    return dict(n=n, acc=acc, prec=prec, rec=rec, tp=tp, fp=fp, fn=fn, tn=tn, bad=bad)


def auc(rs, task):
    pos = [prob(r, task) for r in rs if r[task] and prob(r, task) is not None]
    neg = [prob(r, task) for r in rs if not r[task] and prob(r, task) is not None]
    if not pos or not neg:
        return float("nan")
    wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


names = {"ds": "DeepSeek flash (now)", "qw": "Qwen flash", "dm_single": "decision model, 1 question/request", "dm_fan": "decision model, 2 questions/request"}
cold = [r for r in rows if r["kind"] == "cold"]
by = defaultdict(list)
for r in cold:
    by[r["cfg"]].append(r)

print("\n================ ACCURACY (cold pass, all utterances) ================")
for task, label in (("a", "live-lookup (needs a realtime lookup)"), ("b", "close (asks to end the conversation)")):
    print(f"\n--- task {task}: {label}")
    print(f"{'config':<38}{'n':>4}{'acc':>7}{'prec':>7}{'rec':>7}{'TP':>4}{'FP':>4}{'FN':>4}{'TN':>4}  hard-acc  unsure")
    for cfg in ("ds", "qw", "dm_single", "dm_fan"):
        rs = by[cfg]
        m = metrics(rs, task)
        hard = metrics([r for r in rs if r["hard"]], task)
        uns = ""
        if cfg in ("ds", "qw"):
            key = "raw_live" if task == "a" else "raw_close"
            uns = f"{sum(r[key]=='UNSURE' for r in rs)}"
        print(f"{names[cfg]:<38}{m['n']:>4}{m['acc']:>7.3f}{m['prec']:>7.3f}{m['rec']:>7.3f}{m['tp']:>4}{m['fp']:>4}{m['fn']:>4}{m['tn']:>4}  {hard['acc']:>8.3f}  {uns}")
    for cfg in ("dm_fan",):
        rs = by[cfg]
        print(f"   decision model AUC = {auc(rs, task):.4f};  accuracy at thr 0.3/0.5/0.7/0.9: " +
              " / ".join(f"{metrics(rs, task, t)['acc']:.3f}" for t in (0.3, 0.5, 0.7, 0.9)))

print("\n--- errors by config (text -> decision; label)")
for task in ("a", "b"):
    for cfg in ("ds", "qw", "dm_fan"):
        errs = []
        for r in by[cfg]:
            d = decide(r, task)
            if d is None or bool(d) != bool(r[task]):
                tag = ""
                if cfg == "dm_fan":
                    tag = f" p={prob(r, task)}"
                elif cfg in ("ds", "qw"):
                    tag = f" {r['raw_live'] if task == 'a' else r['raw_close']}"
                errs.append(f"{r['text']} -> {'Y' if d else 'N'}{tag} (label {'Y' if r[task] else 'N'}{', hard' if r['hard'] else ''})")
        print(f"[{task}] {names[cfg]}: {len(errs)} error(s)")
        for e in errs[:14]:
            print("     ", e)

# single vs fan agreement for the decision model
by_i = defaultdict(dict)
for r in cold:
    if r["cfg"] in ("dm_single", "dm_fan"):
        by_i[r["i"]][r["cfg"]] = r
diffs = [abs(v["dm_single"]["p_live"] - v["dm_fan"]["p_live"]) for v in by_i.values() if len(v) == 2 and v["dm_single"]["p_live"] is not None and v["dm_fan"]["p_live"] is not None]
diffs += [abs(v["dm_single"]["p_close"] - v["dm_fan"]["p_close"]) for v in by_i.values() if len(v) == 2 and v["dm_single"]["p_close"] is not None and v["dm_fan"]["p_close"] is not None]
if diffs:
    print(f"\nsingle-question vs two-question probabilities: max |diff| = {max(diffs):.3f}, mean = {statistics.mean(diffs):.4f} (n={len(diffs)})")

print("\n================ LATENCY (ms) ================")
print("pair_ms = wall time until BOTH verdicts are in (what the commit path waits for); production cuts each call at 1200 ms")
for kind in ("cold", "warm"):
    print(f"\n--- {kind} connections")
    print(f"{'config':<38}{'n':>4}{'mean':>8}{'p50':>8}{'p90':>8}{'p99':>8}{'max':>8}   >1200ms")
    for cfg in ("ds", "qw", "dm_single", "dm_fan"):
        v = [r["pair_ms"] for r in rows if r["kind"] == kind and r["cfg"] == cfg and r.get("pair_ms") is not None]
        if not v:
            continue
        over = sum(x > 1200 for x in v)
        print(f"{names[cfg]:<38}{len(v):>4}{statistics.mean(v):>8.0f}{pct(v, .5):>8.0f}{pct(v, .9):>8.0f}{pct(v, .99):>8.0f}{max(v):>8.0f}   {over}/{len(v)}")
    for cfg in ("ds", "qw"):
        v = [x for r in rows if r["kind"] == kind and r["cfg"] == cfg for x in (r["ms_live"], r["ms_close"])]
        if v:
            print(f"   per call, {names[cfg]}: p50 {pct(v, .5):.0f}  p90 {pct(v, .9):.0f}  max {max(v):.0f}  (n={len(v)})")
sm = [r["server_ms"] for r in rows if r["cfg"] == "dm_fan" and r.get("server_ms") is not None]
if sm:
    print(f"\ndecision model server-side latency_ms (two questions): p50 {pct(sm, .5):.0f}  p90 {pct(sm, .9):.0f}  max {max(sm):.0f}")

bad = [r for r in rows if r["cfg"] in ("dm_single", "dm_fan") and (r.get("err") not in (None, [None, None]) or r.get("status") not in (200, [200, 200]))]
print(f"\ndecision-model non-200 / error measurements: {len(bad)}")
for r in bad[:5]:
    print("   ", r.get("status"), r.get("err"))
