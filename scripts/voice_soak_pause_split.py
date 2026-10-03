#!/usr/bin/env python3
"""Pause-tolerance experiment for the post-playback follow-up grace (TODOLIST N-14 tier 3).

After a reply the child's next sentence has no device VAD edge, so its ASR final is endpointed with a
fixed grace (1.2 s, ``MEDIA_PLAYBACK_FOLLOWUP_GRACE_S`` shortens it).  A shorter grace answers sooner but
may cut a sentence with a pause in it into two turns.  This kit measures exactly that: the same child-like
sentence is spoken in two halves with a controlled silence between them, once per pause length, and the
bridge log says how many turns the server committed for it.

    # 1. sentences.json: [{"tag": "dino", "a": "恐龙都", "b": "吃什么东西？"}, ...]
    python scripts/voice_soak_pause_split.py items --sentences sentences.json --out halves.json
    python scripts/voice_soak_bank.py render halves.json --out outputs/pause-halves
    # 2. glue every pair with a silence of P seconds (P=0 is the unsplit control)
    python scripts/voice_soak_pause_split.py clips --sentences sentences.json --halves outputs/pause-halves \\
        --pauses 0,0.3,0.6,0.9,1.2 --repeat 10 --out outputs/pause-bank --scenario outputs/pause-scenario.json
    # 3. the robot round (needs the user's go): voice_soak.py --scenario outputs/pause-scenario.json \\
    #    --bank outputs/pause-bank --serial-log ... --out outputs/acceptance/run-<stamp>-pause
    # 4. turns per pause length
    python scripts/voice_soak_pause_split.py report --run outputs/acceptance/run-<stamp>-pause

One pre-glued clip per step keeps the pause exact (two ``afplay`` calls would add scheduling jitter).  The
report needs ``ts`` in the timeline rows (``voice_soak.py`` writes it) and the bridge log ``docker logs -t``.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import wave
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PAUSE_TAG = re.compile(r"-p(\d+(?:\.\d+)?)$")
# One committed user turn / one split of a pending turn after a reply, as the bridge logs them.
COMMITTED = "media turn committed"
SPLIT = "media pending turn split after reply"
BRIDGE_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z ")


def pause_tag(tag: str, pause: float) -> str:
    return f"{tag}-p{pause:g}"


def halves_items(sentences: list[dict[str, str]]) -> list[dict[str, object]]:
    """Voice-bank items for both halves of every sentence (``voice_soak_bank.py render`` input)."""

    from voice_soak_bank import BOY, CHILD  # noqa: PLC0415 (path inserted above)

    items: list[dict[str, object]] = []
    for index, sentence in enumerate(sentences):
        voice = "bright_peer" if index % 4 != 3 else "warm_companion"
        instruction = CHILD if voice == "bright_peer" else BOY
        for half in ("a", "b"):
            items.append(
                {
                    "id": f"{sentence['tag']}-{half}",
                    "voice": voice,
                    "text": sentence[half],
                    "rate": 1.0,
                    "instruction": instruction,
                }
            )
    return items


def glue(first: Path, second: Path, pause_s: float, out: Path) -> float:
    """Write ``first + pause_s of silence + second``; returns the pause actually inserted."""

    with wave.open(str(first), "rb") as a, wave.open(str(second), "rb") as b:
        params = a.getparams()
        if (b.getnchannels(), b.getsampwidth(), b.getframerate()) != (
            params.nchannels,
            params.sampwidth,
            params.framerate,
        ):
            raise ValueError(f"{first.name} and {second.name} differ in channels, width or rate")
        head, tail = a.readframes(a.getnframes()), b.readframes(b.getnframes())
    silent_frames = round(pause_s * params.framerate)
    silence = b"\x00" * (silent_frames * params.nchannels * params.sampwidth)
    with wave.open(str(out), "wb") as target:
        target.setnchannels(params.nchannels)
        target.setsampwidth(params.sampwidth)
        target.setframerate(params.framerate)
        target.writeframes(head + silence + tail)
    return silent_frames / params.framerate


def scenario_steps(
    sentences: list[dict[str, str]], pauses: list[float], repeat: int
) -> list[dict[str, object]]:
    """Every round visits every pause length; the sentence shifts by one each round, so with as many rounds
    as sentences every sentence meets every pause length (no sentence is stuck with one pause)."""

    steps: list[dict[str, object]] = []
    for round_index in range(repeat):
        for offset, pause in enumerate(pauses):
            sentence = sentences[(round_index + offset) % len(sentences)]
            steps.append(
                {
                    "tag": pause_tag(sentence["tag"], pause),
                    "say": sentence["a"] + "……" + sentence["b"],
                    "rate": 165,
                    "reply_timeout": 30,
                    "gap": 3.0,
                }
            )
    return steps


def build_clips(
    sentences: list[dict[str, str]],
    halves: Path,
    pauses: list[float],
    repeat: int,
    out: Path,
    scenario: Path,
) -> int:
    out.mkdir(parents=True, exist_ok=True)
    for sentence in sentences:
        for pause in pauses:
            glue(
                halves / f"{sentence['tag']}-a.wav",
                halves / f"{sentence['tag']}-b.wav",
                pause,
                out / f"{pause_tag(sentence['tag'], pause)}.wav",
            )
    steps = scenario_steps(sentences, pauses, repeat)
    scenario.write_text(json.dumps(steps, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return len(steps)


def _bridge_times(log: Path, needle: str) -> list[float]:
    """Epoch seconds of every bridge log line containing ``needle`` (``docker logs -t`` stamps are UTC)."""

    times: list[float] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        match = BRIDGE_TS.match(line)
        if match is None or needle not in line:
            continue
        moment = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        times.append(moment.timestamp() + float(match.group(2) or 0))
    return times


def report(run: Path, bridge_log: Path | None = None) -> str:
    rows = [
        json.loads(line)
        for line in (run / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    said = [row for row in rows if row.get("kind") == "said" and PAUSE_TAG.search(row["tag"])]
    if not said:
        return "no pause-split steps in the timeline\n"
    if any("ts" not in row for row in said):
        raise SystemExit("the timeline has no `ts` field: rerun with the current voice_soak.py")
    latency = {row["tag"]: row.get("latency_s") for row in rows if row.get("kind") == "reply"}
    committed = _bridge_times(bridge_log or run / "bridge.log", COMMITTED)
    splits = _bridge_times(bridge_log or run / "bridge.log", SPLIT)
    last_end = said[-1]["ts"] + 60.0  # the last step's reply and any late commit
    by_pause: dict[float, list[tuple[int, int, float | None]]] = defaultdict(list)
    for index, row in enumerate(said):
        start = row["ts"] - 0.5
        end = said[index + 1]["ts"] - 0.5 if index + 1 < len(said) else last_end
        match = PAUSE_TAG.search(row["tag"])
        assert match is not None
        by_pause[float(match.group(1))].append(
            (
                sum(start <= t < end for t in committed),
                sum(start <= t < end for t in splits),
                latency.get(row["tag"]),
            )
        )
    lines = [
        "| pause (s) | steps | one turn | two or more turns | split rate | pending-turn splits | median speech end → reply (s) |",
        "|---|---|---|---|---|---|---|",
    ]
    for pause in sorted(by_pause):
        cells = by_pause[pause]
        whole = sum(1 for commits, _, _ in cells if commits == 1)
        cut = sum(1 for commits, _, _ in cells if commits >= 2)
        known = [value for _, _, value in cells if value is not None]
        median = f"{statistics.median(known):.2f}" if known else "-"
        lines.append(
            f"| {pause:g} | {len(cells)} | {whole} | {cut} | {cut / len(cells):.0%} | "
            f"{sum(count for _, count, _ in cells)} | {median} |"
        )
    return "\n".join(lines) + "\n"


def _pauses(raw: str) -> list[float]:
    values = [float(item) for item in raw.split(",") if item.strip()]
    if not values or any(value < 0 or value > 3 for value in values):
        raise argparse.ArgumentTypeError("pauses are 0-3 seconds, comma separated")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    items = commands.add_parser("items")
    items.add_argument("--sentences", type=Path, required=True)
    items.add_argument("--out", type=Path, required=True)
    clips = commands.add_parser("clips")
    clips.add_argument("--sentences", type=Path, required=True)
    clips.add_argument("--halves", type=Path, required=True)
    clips.add_argument("--pauses", type=_pauses, default=_pauses("0,0.3,0.6,0.9,1.2"))
    clips.add_argument("--repeat", type=int, default=10)
    clips.add_argument("--out", type=Path, required=True)
    clips.add_argument("--scenario", type=Path, required=True)
    summary = commands.add_parser("report")
    summary.add_argument("--run", type=Path, required=True)
    summary.add_argument("--bridge-log", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "items":
        sentences = json.loads(args.sentences.read_text(encoding="utf-8"))
        args.out.write_text(
            json.dumps(halves_items(sentences), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    elif args.command == "clips":
        sentences = json.loads(args.sentences.read_text(encoding="utf-8"))
        count = build_clips(sentences, args.halves, args.pauses, args.repeat, args.out, args.scenario)
        print(f"{count} steps -> {args.scenario}")
    else:
        sys.stdout.write(report(args.run, args.bridge_log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
