"""Per-turn timestamps and latency split from timeline.jsonl + bridge.log. Missing points stay 缺, never estimated."""
import argparse
import difflib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from voice_soak_analyze import pct  # noqa: E402  same P95 as voice_soak_verdict / keepawake_verdict
from voice_soak_evidence import UNVERIFIABLE_STOP_REASONS  # noqa: E402

TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z")
COMMIT_RE = re.compile(r"media turn commit timing .*result=committed.*final_to_done_ms=(\d+)")
MARKS = {"llm_first": "name=llm_first_content_token", "tts_first_pcm": "name=doubao_first_pcm",
         "first_frame_sent": "event=first_frame_sent"}
SEGMENTS = [("收音", "said_end", "asr_final"), ("ASR", "asr_final", "commit"), ("LLM", "commit", "llm_first"),
            ("TTS", "llm_first", "tts_first_pcm"), ("下发播放", "tts_first_pcm", "device_speaking")]
# The "ASR" label is asr_final→commit (bridge final_to_done_ms), not speech→ASR final.
# Card 3 knife baseline soak-232237 (measured): ASR p50 0.91 / p90 1.18 / max 1.31 s on 62 replies.
ASR_FINAL_TO_COMMIT_SEGMENT = "ASR"
BASELINE_232237_ASR_FINAL_TO_COMMIT_S = {"p50": 0.91, "p90": 1.18, "max": 1.31, "n_replies": 62}


def utc_epoch(s: str) -> float:
    head, frac = s.split(".")
    return datetime.fromisoformat(head + "+00:00").timestamp() + float("0." + frac)


def bridge_events(path: Path) -> list[tuple[float, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = TS_RE.match(line)
        if m:
            rows.append((utc_epoch(m.group(1)), line))
    return rows


def first_after(events, start, end, needle=None, regex=None):
    for t, line in events:
        if start <= t <= end and ((needle and needle in line) or (regex and regex.search(line))):
            return t, line
    return None


def split_turn(events, said_end, speaking, window_end, clock):
    p = {"said_end": said_end, "device_speaking": speaking}
    end = speaking if speaking else window_end
    c = first_after(events, said_end - 1.0, end, regex=COMMIT_RE)
    if c:
        p["commit"] = c[0]
        p["asr_final"] = c[0] - int(COMMIT_RE.search(c[1]).group(1)) / 1000
        for key, needle in MARKS.items():
            hit = first_after(events, c[0], end, needle=needle)
            if hit:
                p[key] = hit[0]
    segs, broke = {}, None
    for name, a, b in SEGMENTS:
        if p.get(a) is None or p.get(b) is None:
            segs[name] = "缺"
            broke = broke or name
        else:
            segs[name] = round(p[b] - p[a], 2)
    reference_only = False
    if segs["下发播放"] != "缺":
        # 首包 is server clock, 设备开口 is Mac clock: convert 首包 to Mac time.
        if clock["offset_s"] is not None:
            segs["下发播放"] = round(p["device_speaking"] - (p["tts_first_pcm"] - clock["offset_s"]), 2)
        reference_only = clock["error_s"] is None or clock["error_s"] > 0.1
    known = {k: v for k, v in segs.items() if v != "缺" and not (k == "下发播放" and reference_only)}
    if reference_only:
        segs["下发播放"] = f"{segs['下发播放']}(仅参考)"
    return p, segs, (max(known, key=known.get) if known else "缺"), broke


def p95(xs):
    return pct(xs, 0.95) if xs else None


def load_clock(path: Path | None) -> dict:
    """Mean server-minus-Mac offset; error = worst half round trip + drift between pre and post."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path and path.exists() else []
    if not rows:
        return {"offset_s": None, "error_s": None, "note": "未测时钟差"}
    offs = [r["offset_s"] for r in rows]
    err = max(r["error_s"] for r in rows) + (max(offs) - min(offs))
    return {"offset_s": round(sum(offs) / len(offs), 4), "error_s": round(err, 4), "samples": rows}


REPEAT_RATIO = 0.8         # 相邻两条回复 difflib 字符相似度 >= 0.8 算重复
HALF_P50_DELTA_MAX_S = 0.3  # 唯一已定退化线：后半段相对前半段 P50 增幅


def slope(xs, ys):
    n = len(xs)
    if n < 3:
        return None, None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx:
        return None, None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    resid = sum((y - my - b * (x - mx)) ** 2 for x, y in zip(xs, ys, strict=True))
    se = (resid / (n - 2) / sxx) ** 0.5
    return b, (b / se if se else float("inf"))


def half_stats(ts):
    lat = [t["latency_s"] for t in ts if t["kind"] == "reply" and t["latency_s"] is not None]
    texts = [t["reply_text"] for t in ts if t["kind"] == "reply" and t.get("reply_text")]
    pairs = list(zip(texts, texts[1:], strict=False))
    rep = sum(difflib.SequenceMatcher(None, a, b).ratio() >= REPEAT_RATIO for a, b in pairs)
    return {"turns": len(ts), "replies": sum(t["kind"] == "reply" for t in ts),
            "reply_rate": round(sum(t["kind"] == "reply" for t in ts) / len(ts), 3) if ts else None,
            "repeat_rate": round(rep / len(pairs), 3) if pairs else "缺",
            "repeat_pairs": len(pairs),
            "latency_p50_s": pct(lat, 0.5) if lat else None, "latency_p95_s": p95(lat),
            "threshold_status": {"reply_rate": "待产品确认", "repeat_rate": "待产品确认"}}


def _numeric_segment(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def segment_aggregates(turns: list[dict]) -> dict:
    """Percentiles per latency segment for reply turns that have a numeric value.

    The ASR segment is asr_final→commit (derived from ``final_to_done_ms``), which is the
    card-3 knife. Compare ``segments_s[ASR]`` to ``baseline_232237_asr_final_to_commit_s``.
    """

    replies = [t for t in turns if t.get("kind") == "reply"]
    segments: dict[str, dict] = {}
    for name, _a, _b in SEGMENTS:
        values = [
            seconds
            for turn in replies
            if (seconds := _numeric_segment((turn.get("segments_s") or {}).get(name))) is not None
        ]
        if not values:
            segments[name] = {"n": 0}
            continue
        segments[name] = {
            "n": len(values),
            "p50_s": pct(values, 0.5),
            "p90_s": pct(values, 0.9),
            "max_s": max(values),
        }
    worst_counts: dict[str, int] = {}
    for turn in replies:
        worst = turn.get("卡在哪段")
        if isinstance(worst, str) and worst and worst != "缺":
            worst_counts[worst] = worst_counts.get(worst, 0) + 1
    return {
        "segments_s": segments,
        "worst_segment_counts": worst_counts,
        "asr_final_to_commit_field": ASR_FINAL_TO_COMMIT_SEGMENT,
        "asr_final_to_commit_means": "asr_final→commit via final_to_done_ms",
        "baseline_232237_asr_final_to_commit_s": dict(BASELINE_232237_ASR_FINAL_TO_COMMIT_S),
        "this_run_asr_final_to_commit_s": segments.get(ASR_FINAL_TO_COMMIT_SEGMENT, {"n": 0}),
    }


def degradation(turns):
    """Split by time at the midpoint of the first and last turn; 第几说 = order among all turns.

    Only half P50 delta ≤ 0.3 s is a pass/fail line. Reply rate, repeat rate, and slope
    are reported with 待产品确认 and do not drive the verdict.
    """
    if len(turns) < 4:
        return {"verdict": "样本不足", "turns": len(turns)}
    mid = (turns[0]["播放结束"] + turns[-1]["播放结束"]) / 2
    first = half_stats([t for t in turns if t["播放结束"] < mid])
    second = half_stats([t for t in turns if t["播放结束"] >= mid])
    pts = [(i + 1, t["latency_s"]) for i, t in enumerate(turns) if t["kind"] == "reply" and t["latency_s"] is not None]
    b, tval = slope([x for x, _ in pts], [y for _, y in pts])
    first_p50, second_p50 = first["latency_p50_s"], second["latency_p50_s"]
    delta = None
    if first_p50 is not None and second_p50 is not None:
        delta = round(second_p50 - first_p50, 3)
    reasons = []
    if delta is None:
        verdict = "样本不足"
        reasons.append("前后半段 P50 不可测")
    elif delta > HALF_P50_DELTA_MAX_S:
        verdict = "退化"
        reasons.append(f"后半段 P50 增幅 {delta:.3f} s > {HALF_P50_DELTA_MAX_S} s")
    else:
        verdict = "未见退化"
    return {"thresholds": {"half_p50_delta_max_s": HALF_P50_DELTA_MAX_S, "repeat_ratio": REPEAT_RATIO,
                           "reply_rate": "待产品确认", "repeat_rate": "待产品确认",
                           "latency_slope_s_per_turn": "待产品确认"},
            "split_at": round(mid, 3), "first_half": first, "second_half": second,
            "half_p50_delta_s": delta,
            "latency_slope_s_per_turn": round(b, 4) if b is not None else None,
            "latency_slope_t": round(tval, 2) if tval is not None else None,
            "repeat_note": None if "缺" not in (first["repeat_rate"], second["repeat_rate"]) else "缺回复文本: timeline 的 reply 行没有 reply_text，bridge.log 只记 text_len",
            "verdict": verdict, "reasons": reasons}


def build(out: Path, stop_tags: dict[str, str], clock_path: Path | None = None) -> dict:
    clock = load_clock(clock_path)
    rows = [json.loads(line) for line in (out / "timeline.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    events = bridge_events(out / "bridge.log")
    said, turns = {}, []
    for _i, r in enumerate(rows):
        if r.get("kind") == "said":
            said[r["tag"]] = r
        if r.get("kind") in ("reply", "no_reply") and r["tag"] in said:
            s = said[r["tag"]]
            said_end = s["ts"]  # the said row is written right at playback end
            said_start = said_end - (datetime.strptime(s["end"], "%H:%M:%S.%f") - datetime.strptime(s["start"], "%H:%M:%S.%f")).total_seconds()
            speaking = r.get("reply_started_ts")
            pts, segs, worst, broke = split_turn(events, said_end, speaking, r["ts"], clock)
            t = {"tag": r["tag"], "kind": r["kind"], "latency_s": r.get("latency_s"),
                 "播放开始": round(said_start, 3), "播放结束": round(said_end, 3),
                 "首包": round(pts["tts_first_pcm"], 3) if "tts_first_pcm" in pts else "缺",
                 "设备开口": speaking if speaking else "缺",
                 "segments_s": segs, "卡在哪段": worst, "reply_text": r.get("reply_text")}
            if r["kind"] == "no_reply":
                t["断在"] = broke or "设备开口"
            base = r["tag"].split("-", 1)[1] if "-" in r["tag"] else r["tag"]
            if base in stop_tags:
                kind = stop_tags[base]
                reason = r.get("stop_reason") or r.get("note")
                if kind == "backchannel":  # 附和: must keep playing; playback_completed = PASS, stopped = FAIL
                    ok = (
                        r.get("stop_reason") == "playback_completed"
                        or r.get("stop_reason") in UNVERIFIABLE_STOP_REASONS
                        or r.get("interrupted") is False
                    )
                else:
                    ok = (
                        r.get("stop_verdict") == "stopped"
                        or r.get("interrupted") is False
                        or r.get("stop_reason") in UNVERIFIABLE_STOP_REASONS
                    )
                t["stop"] = {"kind": kind, "interrupted": r.get("interrupted"), "verdict": r.get("stop_verdict"),
                             "reason": reason, "pass": ok}
                if not ok:
                    t["断在"] = reason or "缺"
            turns.append(t)
    lat = [t["latency_s"] for t in turns if t["kind"] == "reply" and t["latency_s"] is not None]
    def rate(kind):
        xs = [t for t in turns if t.get("stop", {}).get("kind") == kind]
        n = sum(1 for t in xs if t["stop"]["pass"])
        return {"total": len(xs), "passed": n, "pass_rate": round(n / len(xs), 3) if xs else None,
                "tags": [t["tag"] for t in xs]}
    return {
        "clock": {k: v for k, v in clock.items() if k != "samples"} | {"下发播放": "仅参考" if clock["error_s"] is None or clock["error_s"] > 0.1 else "已校正"},
        "no_reply": sum(1 for t in turns if t["kind"] == "no_reply"),
        "tool_wait_timeout": sum(1 for r in rows if r.get("kind") == "tool_wait_timeout"),
        "abort": next((r.get("verdict") for r in rows if r.get("kind") == "abort"), None),
        "reply_latency_p95_s": p95(lat),
        "slowest3": [{"tag": t["tag"], "latency_s": t["latency_s"], "卡在哪段": t["卡在哪段"]}
                     for t in sorted((t for t in turns if t["latency_s"] is not None), key=lambda t: -t["latency_s"])[:3]],
        "interrupt": rate("stop"),
        "backchannel": rate("backchannel"),
        "long_session": degradation(turns),
        "knife_asr_final_to_commit": segment_aggregates(turns),
        "turns": turns,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--clock", default=None, help="clock.jsonl from clock_offset.py")
    ap.add_argument("--tags", default="", help="comma list: print only these turn prefixes")
    a = ap.parse_args()
    stop_tags = {s["tag"]: s["interrupt"].get("kind", "stop")
                 for s in json.loads(Path(a.scenario).read_text(encoding="utf-8")) if s.get("interrupt")}
    rep = build(Path(a.out), stop_tags, Path(a.clock) if a.clock else None)
    if a.tags:
        want = a.tags.split(",")
        rep = [t for t in rep["turns"] if t["tag"].split("-")[0] in want]
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
