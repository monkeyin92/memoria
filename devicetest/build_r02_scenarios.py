#!/usr/bin/env python3
"""Build the R02 interrupt and backchannel scenarios for the tapwake driver.

The fixed word lists come from the Notion R02 card and the gate page; do not
substitute them. Every trial is one interrupt attempt against a reply that is
still playing, so one word contributes ten denominators.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# Notion R02 card: stop 8 x 10, backchannel 6 x 10, content barge-in 10.
STOP_WORDS: tuple[tuple[str, str], ...] = (
    ("ting", "停。"),
    ("ting-yixia", "停一下。"),
    ("bie-shuo-le", "别说了。"),
    ("bu-yao-shuo-le", "不要说了。"),
    ("hao-le-hao-le", "好了好了。"),
    ("gou-le", "够了。"),
    ("anjing", "安静。"),
    ("deng-yixia", "等一下。"),
)
BACKCHANNEL_WORDS: tuple[tuple[str, str], ...] = (
    ("en", "嗯。"),
    ("dui", "对。"),
    ("hao", "好。"),
    ("haha", "哈哈。"),
    ("shi-ma", "是吗？"),
    ("ranhou-ne", "然后呢？"),
)
CONTENT_QUESTION = "等一下，我想问另一个问题，云是怎么变出来的？"

# Ordinary child questions whose replies run long enough that the interrupt still
# lands with playback left (the evidence script drops an attempt under 2.5 s).
PROMPTS: tuple[tuple[str, str], ...] = (
    ("story", "给我讲一个故事吧。"),
    ("panda", "介绍一下大熊猫。"),
    ("giraffe", "讲讲长颈鹿为什么脖子那么长。"),
    ("dino", "霸王龙有多大呀？"),
    ("sky", "为什么天是蓝色的？"),
)


def _prompt(index: int) -> tuple[str, str]:
    return PROMPTS[index % len(PROMPTS)]


def build_stop() -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    for word_index, (slug, word) in enumerate(STOP_WORDS, 1):
        for trial in range(1, 11):
            prompt_key, prompt = _prompt(word_index + trial)
            steps.append(
                {
                    "tag": f"stop{word_index:02d}-{slug}-{trial:02d}",
                    "say": prompt,
                    "rate": 165,
                    "gap": 2,
                    "interrupt": {
                        "after": 4,
                        "say": word,
                        "rate": 170,
                        "settle": 12,
                        "kind": "stop",
                        "prompt": prompt_key,
                    },
                }
            )
    return steps


def build_backchannel() -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    for word_index, (slug, word) in enumerate(BACKCHANNEL_WORDS, 1):
        for trial in range(1, 11):
            prompt_key, prompt = _prompt(word_index + trial)
            steps.append(
                {
                    "tag": f"back{word_index:02d}-{slug}-{trial:02d}",
                    "say": prompt,
                    "rate": 165,
                    "gap": 2,
                    "interrupt": {
                        "after": 4,
                        "say": word,
                        "rate": 170,
                        "settle": 2,
                        "kind": "backchannel",
                        "expect_playing": True,
                        "prompt": prompt_key,
                    },
                }
            )
    return steps


def build_content() -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    for trial in range(1, 11):
        prompt_key, prompt = _prompt(trial)
        steps.append(
            {
                "tag": f"content-{trial:02d}",
                "say": prompt,
                "rate": 165,
                "gap": 2,
                "interrupt": {
                    "after": 4,
                    "say": CONTENT_QUESTION,
                    "rate": 165,
                    "settle": 15,
                    "kind": "stop",
                    "expect_answer": True,
                    "answer_timeout": 25,
                    "prompt": prompt_key,
                },
            }
        )
    return steps


SUITES = {
    "a": ("stop", build_stop),
    "b": ("backchannel", build_backchannel),
    "c": ("content", build_content),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path, help="directory for the scenario JSON files")
    ap.add_argument("--suite", choices=("a", "b", "c", "all"), default="all")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    suites = SUITES.keys() if args.suite == "all" else (args.suite,)
    for key in suites:
        name, build = SUITES[key]
        steps = build()
        path = args.out / f"r02-{key}-{name}.json"
        path.write_text(json.dumps(steps, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"{path} steps={len(steps)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
