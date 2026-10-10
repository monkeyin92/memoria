#!/usr/bin/env python3
"""Turn a voice_soak run directory into a findings table.

    python scripts/voice_soak_analyze.py RUN_DIR START END   (START/END: 'YYYY-MM-DD HH:MM:SS' Asia/Shanghai)

Reads the run's timeline/serial/bridge/edge logs, and (read-only, over ssh) the archived turns of
the bound child for the same window: what the server heard (`speech.utterance_finalized`) and what
the robot said (`assistant.playout_stopped`). Prints markdown.
"""

from __future__ import annotations

import difflib
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

if __package__:
    from .voice_soak_evidence import audit_interruptions
else:
    from voice_soak_evidence import audit_interruptions

PSQL = (
    "sudo docker exec -i memoria-data-postgres-1 psql -U memoria_admin -d memoria -At -F '\t' -f -"
)


def archive_rows(start: str, end: str) -> list[dict[str, str]]:
    sql = f"""
select to_char(occurred_at at time zone 'Asia/Shanghai','HH24:MI:SS'), event_type,
       replace(replace(coalesce(payload->>'text',''),E'\\n',' '),E'\\t',' ')
from archive_evidence_events
where event_type in ('speech.utterance_finalized','assistant.playout_stopped')
  and occurred_at >= timestamptz '{start}+08' and occurred_at <= timestamptz '{end}+08'
order by occurred_at;
"""
    out = subprocess.run(["ssh", "memoria-prod", PSQL], input=sql, capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append({"at": parts[0], "type": parts[1], "text": parts[2]})
    return rows


def norm(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;“”\"'《》（）()\-—_]+", "", text)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main() -> int:
    run = Path(sys.argv[1])
    start, end = sys.argv[2], sys.argv[3]
    rows = [json.loads(line) for line in (run / "timeline.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    said = [r for r in rows if r.get("kind") == "said"]
    replies = {r["tag"]: r for r in rows if r.get("kind") == "reply"}
    bridge_text = (run / "bridge.log").read_text(encoding="utf-8", errors="replace") if (run / "bridge.log").exists() else ""
    serial_text = (run / "serial.log").read_text(encoding="utf-8", errors="replace") if (run / "serial.log").exists() else ""
    for tag, assessment in audit_interruptions(rows, bridge_text, serial_text).items():
        replies[tag] = {**replies[tag], **assessment}
    no_reply = {r["tag"] for r in rows if r.get("kind") == "no_reply"}
    wakes = [r for r in rows if r.get("kind") in {"wake_try", "woke"}]
    archive = archive_rows(start, end)
    heard = [a for a in archive if a["type"] == "speech.utterance_finalized"]
    spoken = [a for a in archive if a["type"] == "assistant.playout_stopped"]

    print("# 对话长稳测试结果\n")
    print(f"- 窗口：{start} – {end}（北京时间）；说话 {len(said)} 句；唤醒尝试 {sum(1 for w in wakes if w['kind']=='wake_try')} 次，成功 {sum(1 for w in wakes if w['kind']=='woke')} 次")
    lat = [r["latency_s"] for r in replies.values() if r.get("latency_s") is not None]
    dur = [r["reply_s"] for r in replies.values() if r.get("reply_s") is not None]
    if lat:
        print(f"- 有回复 {len(replies)} / 无回复 {len(no_reply)}；说完到开口 p50 {statistics.median(lat):.1f}s p95 {pct(lat, .95):.1f}s 最长 {max(lat):.1f}s")
    if dur:
        print(f"- 回复时长 p50 {statistics.median(dur):.1f}s p95 {pct(dur, .95):.1f}s 最长 {max(dur):.1f}s")
    interrupted = [r for r in replies.values() if r.get("interrupted")]
    if interrupted:
        ok = [r for r in interrupted if r.get("stopped") is True]
        failed = sum(r.get("stopped") is False for r in interrupted)
        unknown = sum(r.get("stopped") is None for r in interrupted)
        delays = [r["stop_delay_s"] for r in ok if r.get("stop_delay_s") is not None]
        print(f"- 打断 {len(interrupted)} 次，确认停止 {len(ok)} 次，未停止 {failed} 次，证据不足 {unknown} 次"
              + (f"，素材起点到设备退出说话 p50 {statistics.median(delays):.1f}s（非声学测量）" if delays else ""))

    print("\n## 逐句\n")
    print("| # | 场景 | 我说的 | 服务器听到的 | 字符相似度 | 说完到开口 | 回复时长 | 备注 |")
    print("|---|---|---|---|---:|---:|---:|---|")
    used: set[int] = set()
    accuracies: list[float] = []
    for index, row in enumerate(said, 1):
        tag = row["tag"]
        match_index = None
        for i, h in enumerate(heard):
            if i in used:
                continue
            if h["at"] >= row["start"][:8]:
                ratio = difflib.SequenceMatcher(None, norm(row["text"]), norm(h["text"])).ratio()
                if ratio > 0.45:
                    match_index = i
                    break
        heard_text, ratio = "—", 0.0
        if match_index is not None:
            used.add(match_index)
            heard_text = heard[match_index]["text"]
            ratio = difflib.SequenceMatcher(None, norm(row["text"]), norm(heard_text)).ratio()
            accuracies.append(ratio)
        reply = replies.get(tag, {})
        note = "无回复" if tag in no_reply else ("；".join(filter(None, [
            (f"打断{'成功' if reply.get('stopped') is True else ('未停' if reply.get('stopped') is False else '未验证')}（{reply.get('stop_reason')}）"
             if reply.get("interrupted") else ""),
            f"结束于 {reply.get('end_state')}" if reply.get("end_state") and reply.get("end_state") != "listening" else "",
        ])))
        print(f"| {index} | {tag} | {row['text']} | {heard_text} | {ratio:.2f} | {reply.get('latency_s', '—')} | {reply.get('reply_s', '—')} | {note} |")
    if accuracies:
        print(f"\n听到的文字与原话相似度：平均 {statistics.mean(accuracies):.2f}，≥0.9 的 {sum(1 for a in accuracies if a >= 0.9)}/{len(accuracies)}，没匹配上的 {len(said) - len(accuracies)} 句。")

    print("\n## 机器人说的话（档案里的实际播放文本，逐条，按时间）\n")
    for item in spoken:
        print(f"- {item['at']} {item['text']}")
    if not spoken:
        print("（窗口内档案没有 assistant.playout_stopped；回复文字可改用提示词实验复核）")

    print("\n## 回复文本体检（档案里实际播放的文本）\n")
    if spoken:
        texts = [item["text"] for item in spoken]
        chars = [len(re.sub(r"[\s，。！？、；：,.!?;:“”\"'…]+", "", text)) for text in texts]
        sentences = [len(re.findall(r"[。！？!?；;]", text)) for text in texts]
        markdown = [t for t in texts if "**" in t or re.search(r"(^|[。\s])\d+\s*[.、]\s*\*?\*?\S", t)]
        leaks = [t for t in texts if any(marker in t for marker in ("控制响应计划", "ResponsePlan", "系统提示", "【不可变", "【当轮语义策略】"))]
        print(f"- 回复 {len(texts)} 条；字数 p50 {statistics.median(chars):.0f}、p95 {pct(chars, .95)}、最长 {max(chars)}；句数 p50 {statistics.median(sentences):.0f}、最多 {max(sentences)}")
        print(f"- 带 Markdown 标记（加粗 / 序号列表）的 {len(markdown)} 条；念出内部计划 / 提示词字样的 {len(leaks)} 条")
        for text in leaks:
            print(f"  - 泄露：{text[:80]}")
    else:
        print("（窗口内档案没有 assistant.playout_stopped）")

    print("\n## 日志异常计数\n")
    for name in ("bridge", "edge", "control", "serial"):
        path = run / f"{name}.log"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        counters = {
            "Traceback": len(re.findall(r"Traceback", text)),
            "ERROR": len(re.findall(r"\bERROR\b", text)),
            "WARNING": len(re.findall(r"\bWARNING\b", text)),
            "turn discarded": len(re.findall(r"turn discarded", text)),
            "ASR tail timeout": len(re.findall(r"ASR tail timeout", text)),
            "preempted": len(re.findall(r"preempted", text)),
            "disconnected": len(re.findall(r"disconnected", text)),
            "E (": len(re.findall(r"\bE \(\d+\)", text)),
            "screen off": len(re.findall(r"screen off", text)),
            "screen on": len(re.findall(r"screen on", text)),
            "Wake word detected": len(re.findall(r"Wake word detected", text)),
        }
        shown = {k: v for k, v in counters.items() if v}
        print(f"- {name}: {shown or '无'}")
    rates = re.findall(r"speech_plan_selected emotion=(\w+).*?rate=([0-9.]+)", (run / "bridge.log").read_text(encoding="utf-8", errors="replace")) if (run / "bridge.log").exists() else []
    if rates:
        print(f"- speech_plan 语速：{sorted(set(r for _, r in rates))}；情绪分布：{ {e: sum(1 for x, _ in rates if x == e) for e in sorted(set(x for x, _ in rates))} }")
    return 0


if __name__ == "__main__":
    sys.exit(main())
