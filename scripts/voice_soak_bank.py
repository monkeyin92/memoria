#!/usr/bin/env python3
"""Build the soak's voice bank: the test user's lines spoken by production Doubao voices.

macOS ``say`` is a poor stand-in for a child: on the test Mac only the Tingting voice produces audio at
all (Flo, Sandy, Shelley, Grandma ... write EMPTY 0.01 s files with ``-o``), and one robotic voice is a
narrow test for the wake word and the recogniser. The bank renders every scenario line, and the
interruptions, with a child-like, boy-like or emotional delivery in the companion voices the product ships.

    python scripts/voice_soak_bank.py items --scenario scripts/voice_soak_scenarios/child.json --out items.json
    python scripts/voice_soak_bank.py wake-items --out wake_items.json
    python scripts/voice_soak_bank.py render items.json --out outputs/voice-bank [--remote memoria-prod]

``render`` needs ssh access to the production host and runs ``voice_soak_bank_render.py`` inside the
bridge container (the provider key never leaves the server). Clips are named ``<step tag>.wav``
(``<step tag>-stop.wav`` for an interruption), which is what ``voice_soak.py --bank`` plays.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHILD = "像十岁左右的小学生在说话，声音清脆自然，语气随意，不要播音腔"
BOY = "像十岁左右的男孩在说话，声音自然，语气随意，不要播音腔"
# step tag -> (how it is said, rate)
EMOTION = {
    "lonely": ("低落小声，带一点孤单", 0.92), "no-play": ("委屈低落，小声", 0.92), "sad-exam": ("难过低落，带一点哽咽", 0.9),
    "bullied": ("委屈又害怕，声音发紧", 0.92), "anxious": ("紧张焦虑，语速偏快", 1.02), "teen-stress": ("疲惫又焦虑，声音低沉", 0.95),
    "angry": ("生气，语气冲", 1.05), "scared": ("害怕，声音发抖", 1.0), "cry": ("带着哭腔，很小声", 0.88),
    "tired": ("困倦，慢吞吞", 0.88), "secret": ("神秘，压低声音", 0.95), "haha": ("开心地笑着说", 1.05),
}
VOICES = ("bright_peer", "warm_companion", "soft_confidante", "calm_guide", "low_magnetic")
LOOKALIKES = ["魔力", "茉莉花", "摩里", "没力", "莫里亚", "梅莫里", "默哩", "墨利", "茉莉莉", "嘛哩", "磨砺", "末利"]
SENTENCES = [
    "今天天气真不错，我们一起去公园玩吧。", "请把那本书递给我一下，谢谢。", "明天早上八点在学校门口集合。", "你晚饭想吃什么，面条还是米饭？",
    "这个故事讲的是一只小兔子的冒险。", "新闻说周末可能会下雨，大家出门要带伞。", "我有一个好主意，我们来画一幅画吧。",
    "爸爸妈妈今天晚一点回来，你先写作业。", "电视里正在播放动画片，声音有点大。", "摩托车从马路上飞快地开过去了。",
    "茉莉花的香味真好闻。", "这个魔力棒是妈妈买给我的。",
]


def step_items(scenario: Path) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for index, step in enumerate(json.loads(scenario.read_text(encoding="utf-8"))):
        if not step.get("say"):
            continue
        voice = "bright_peer" if index % 4 != 3 else "warm_companion"
        base = CHILD if voice == "bright_peer" else BOY
        mood, rate = EMOTION.get(step["tag"], ("", 1.0))
        items.append({"id": step["tag"], "voice": voice, "text": step["say"], "rate": rate,
                      "instruction": base + ("，" + mood if mood else "")})
        interrupt = step.get("interrupt")
        if interrupt:
            items.append({"id": step["tag"] + "-stop", "voice": voice, "text": interrupt["say"], "rate": 1.05,
                          "instruction": base + "，打断别人说话，语气急一点"})
    return items


def wake_items() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for voice in VOICES:
        for rate in (0.9, 1.0, 1.15):
            items.append({"id": f"wake-{voice}-{rate}", "voice": voice, "text": "茉莉", "rate": rate,
                          "instruction": "像在叫一个熟悉的朋友，自然清晰"})
    items.append({"id": "wake-call-bright_peer", "voice": "bright_peer", "text": "茉莉，你在吗？", "rate": 1.0, "instruction": CHILD})
    items.append({"id": "wake-call-warm_companion", "voice": "warm_companion", "text": "茉莉，我想跟你说话。", "rate": 1.0, "instruction": BOY})
    for n, word in enumerate(LOOKALIKES):
        items.append({"id": f"look-{n:02d}", "voice": "bright_peer", "text": word, "rate": 1.0, "instruction": CHILD})
    for n, text in enumerate(SENTENCES):
        voice = "bright_peer" if n % 2 == 0 else "warm_companion"
        items.append({"id": f"sent-{n:02d}", "voice": voice, "text": text, "rate": 1.0,
                      "instruction": CHILD if voice == "bright_peer" else BOY})
    return items


def render(items_path: Path, out: Path, remote: str, container: str) -> int:
    config = base64.b64encode(json.dumps({"items": json.loads(items_path.read_text(encoding="utf-8"))["items"]}).encode()).decode()
    command = f"docker exec -i -e SAMPLE_CFG_B64={config} {container} /app/.venv/bin/python -"
    result = subprocess.run(["ssh", remote, command], input=(HERE / "voice_soak_bank_render.py").read_bytes(),
                            capture_output=True, check=False)
    out.mkdir(parents=True, exist_ok=True)
    written = errors = 0
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        if not line.startswith("{"):
            continue
        row = json.loads(line)
        if "error" in row:
            errors += 1
            print("render error:", row["id"], row["error"], file=sys.stderr)
            continue
        (out / f"{row['id']}.wav").write_bytes(base64.b64decode(row["wav_b64"]))
        written += 1
    print(f"voice_bank clips={written} errors={errors} dir={out}")
    return 0 if written and not errors else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("items", help="item list for a scenario's lines and interruptions")
    p.add_argument("--scenario", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p = sub.add_parser("wake-items", help="item list for the wake-word / look-alike / ordinary-sentence trials")
    p.add_argument("--out", required=True, type=Path)
    p = sub.add_parser("render", help="render an item list on the server and decode the clips")
    p.add_argument("items", type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--remote", default="memoria-prod")
    p.add_argument("--container", default="memoria-voice-core-media-bridge-1")
    args = ap.parse_args()
    if args.cmd == "items":
        args.out.write_text(json.dumps({"items": step_items(args.scenario)}, ensure_ascii=False), encoding="utf-8")
        return 0
    if args.cmd == "wake-items":
        args.out.write_text(json.dumps({"items": wake_items()}, ensure_ascii=False), encoding="utf-8")
        return 0
    return render(args.items, args.out, args.remote, args.container)


if __name__ == "__main__":
    sys.exit(main())
