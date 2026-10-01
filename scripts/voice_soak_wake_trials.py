#!/usr/bin/env python3
"""Wake-word trials: production Doubao voices (and the one macOS voice that works) say the wake word,
words that sound like it, and ordinary sentences. Run against the live device over its serial console;
nothing on the device or the server is changed.

    python scripts/voice_soak_bank.py wake-items --out wake_items.json
    python scripts/voice_soak_bank.py render wake_items.json --out outputs/voice-bank
    uv run --no-project --with pyserial python scripts/voice_soak_wake_trials.py OUT_DIR VOLUME [true_wake,lookalikes,sentences] --bank outputs/voice-bank

An earlier version used the macOS voices Flo/Sandy/Shelley, which write EMPTY audio with `say -o` (0.01 s
files), so its "only Tingting wakes the robot" result was an artefact. Every clip is checked to be longer
than half a second before it is played.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_soak as soak  # noqa: E402

BANK = Path("outputs/voice-bank")
PROB_RE = re.compile(r"Custom wake word detected: command_id=\d+, string=\s*([a-z ]*), prob=([0-9.]+)")


def clip_seconds(path: Path) -> float:
    info = subprocess.run(["afinfo", str(path)], capture_output=True, text=True).stdout
    match = re.search(r"estimated duration: ([0-9.]+)", info)
    return float(match.group(1)) if match else 0.0


def play(path: Path) -> tuple[float, float, float]:
    clip = clip_seconds(path)
    if clip < 0.5:
        raise RuntimeError(f"{path.name} is {clip:.2f}s long: refusing to play a silent clip")
    start = soak.now()
    subprocess.run(["afplay", str(path)], check=True)
    return start, soak.now(), clip


def tingting(text: str, rate: int, out: Path, tag: str) -> Path:
    path = out / f"{tag}.aiff"
    subprocess.run(["say", "-v", "Tingting", "-r", str(rate), "-o", str(path), text], check=True)
    return path


def main() -> int:
    global BANK
    argv = list(sys.argv[1:])
    if "--bank" in argv:
        at = argv.index("--bank")
        BANK = Path(argv[at + 1])
        del argv[at : at + 2]
    out = Path(argv[0])
    out.mkdir(parents=True, exist_ok=False)
    only = argv[2].split(",") if len(argv) > 2 else ["true_wake", "lookalikes", "sentences"]
    watcher = soak.SerialWatcher(out / "serial.log")
    watcher.start()
    previous_volume = soak.set_volume(int(argv[1]) if len(argv) > 1 else 55)

    trials: dict[str, list[tuple[str, str, Path]]] = {"true_wake": [], "lookalikes": [], "sentences": []}
    for voice in ("bright_peer", "warm_companion", "soft_confidante", "calm_guide", "low_magnetic"):
        for rate in ("0.9", "1.0", "1.15"):
            trials["true_wake"].append((f"{voice}@{rate}", "茉莉", BANK / f"wake-{voice}-{rate}.wav"))
    trials["true_wake"].append(("call-bright_peer", "茉莉，你在吗？", BANK / "wake-call-bright_peer.wav"))
    trials["true_wake"].append(("call-warm_companion", "茉莉，我想跟你说话。", BANK / "wake-call-warm_companion.wav"))
    for rate in (150, 175, 200):
        trials["true_wake"].append((f"Tingting@{rate}", "茉莉", tingting("茉莉", rate, out, f"tingting-{rate}")))
    words = ["魔力", "茉莉花", "摩里", "没力", "莫里亚", "梅莫里", "默哩", "墨利", "茉莉莉", "嘛哩", "磨砺", "末利"]
    for n, word in enumerate(words):
        trials["lookalikes"].append((f"look-{word}", word, BANK / f"look-{n:02d}.wav"))
    sentences = ["今天天气真不错，我们一起去公园玩吧。", "请把那本书递给我一下，谢谢。", "明天早上八点在学校门口集合。", "你晚饭想吃什么，面条还是米饭？",
                 "这个故事讲的是一只小兔子的冒险。", "新闻说周末可能会下雨，大家出门要带伞。", "我有一个好主意，我们来画一幅画吧。",
                 "爸爸妈妈今天晚一点回来，你先写作业。", "电视里正在播放动画片，声音有点大。", "摩托车从马路上飞快地开过去了。",
                 "茉莉花的香味真好闻。", "这个魔力棒是妈妈买给我的。"]
    for n, text in enumerate(sentences):
        trials["sentences"].append((f"sent-{n:02d}", text, BANK / f"sent-{n:02d}.wav"))

    results: dict[str, list[dict[str, object]]] = {k: [] for k in trials}
    try:
        if watcher.wait_state({"idle", "listening"}, 25) is None:
            with watcher.cond:
                watcher.state = "idle"
                watcher.state_since = soak.now()
        time.sleep(18 if watcher.booted_at else 1)  # a board reset by the port open needs ~16 s to arm the wake word
        for bucket in only:
            for label, text, path in trials[bucket]:
                if watcher.wait_state({"idle"}, 120) is None:
                    results[bucket].append({"label": label, "skipped": "device not idle", "state": watcher.state})
                    continue
                time.sleep(1.5)
                mark = len(watcher.events)
                t0, t1, clip = play(path)
                woke = watcher.wait_state({"connecting", "listening"}, 5.0, since=t0 - 0.2) is not None
                time.sleep(0.6)
                lines = [f"{soak.stamp(e[0])} {e[2]}" for e in watcher.events[mark:] if e[1] in {"note", "state"}][:6]
                row = {"label": label, "text": text, "clip_s": round(clip, 2), "woke": woke, "t": soak.stamp(t0), "events": lines}
                results[bucket].append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        soak.set_volume(previous_volume)
        watcher.stop.set()
        time.sleep(0.5)
        log_text = (out / "serial.log").read_text(encoding="utf-8", errors="replace")
        results["detections_in_serial"] = [{"string": m.group(1).strip(), "prob": float(m.group(2))} for m in PROB_RE.finditer(log_text)]  # type: ignore[assignment]
        (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        for bucket in only:
            rows = results[bucket]
            print(f"{bucket}: woke {sum(1 for r in rows if r.get('woke'))}/{len(rows)} (skipped {sum(1 for r in rows if r.get('skipped'))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
