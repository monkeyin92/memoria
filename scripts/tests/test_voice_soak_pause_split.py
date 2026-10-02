"""The pause-tolerance experiment kit (offline: synthetic WAV halves, a synthetic run directory).

The kit exists so the follow-up grace experiment (TODOLIST N-14 tier 3) needs nothing but the robot round
itself: an exact pause per clip, a balanced scenario, and a per-pause count of how many turns the server
committed for one spoken sentence.
"""

from __future__ import annotations

import json
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest
from scripts import voice_soak_pause_split as kit

RATE = 16_000


def _wav(path: Path, seconds: float, *, rate: int = RATE, width: int = 2, value: int = 100) -> Path:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes((value.to_bytes(width, "little", signed=True)) * frames)
    return path


def _frames(path: Path) -> tuple[int, bytes]:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes(), handle.readframes(handle.getnframes())


def test_glue_inserts_exactly_the_requested_silence(tmp_path: Path) -> None:
    first, second = _wav(tmp_path / "a.wav", 0.5), _wav(tmp_path / "b.wav", 0.25, value=-7)

    inserted = kit.glue(first, second, 0.6, tmp_path / "ab.wav")

    count, data = _frames(tmp_path / "ab.wav")
    assert inserted == 0.6
    assert count == int(0.5 * RATE) + round(0.6 * RATE) + int(0.25 * RATE)
    head = int(0.5 * RATE) * 2
    silence = round(0.6 * RATE) * 2
    assert data[:head] == (100).to_bytes(2, "little", signed=True) * int(0.5 * RATE)
    assert data[head : head + silence] == b"\x00" * silence
    assert data[head + silence :] == (-7).to_bytes(2, "little", signed=True) * int(0.25 * RATE)


def test_a_zero_pause_is_the_unsplit_control(tmp_path: Path) -> None:
    first, second = _wav(tmp_path / "a.wav", 0.1), _wav(tmp_path / "b.wav", 0.1)

    kit.glue(first, second, 0.0, tmp_path / "ab.wav")

    assert _frames(tmp_path / "ab.wav")[0] == 2 * int(0.1 * RATE)


def test_halves_of_a_different_format_are_refused(tmp_path: Path) -> None:
    first, second = _wav(tmp_path / "a.wav", 0.1), _wav(tmp_path / "b.wav", 0.1, rate=24_000)

    with pytest.raises(ValueError, match="differ"):
        kit.glue(first, second, 0.3, tmp_path / "ab.wav")


SENTENCES = [
    {"tag": "dino", "a": "恐龙都", "b": "吃什么东西？"},
    {"tag": "rain", "a": "为什么", "b": "会下雨呀？"},
]


def test_the_scenario_visits_every_pause_length_in_every_round() -> None:
    steps = kit.scenario_steps(SENTENCES, [0, 0.3, 0.6, 0.9, 1.2], repeat=3)

    assert len(steps) == 15
    for round_index in range(3):
        block = steps[round_index * 5 : (round_index + 1) * 5]
        assert sorted(kit.PAUSE_TAG.search(step["tag"]).group(1) for step in block) == [
            "0",
            "0.3",
            "0.6",
            "0.9",
            "1.2",
        ]
    assert {step["tag"].rsplit("-p", 1)[0] for step in steps} == {"dino", "rain"}
    assert all(step["say"] for step in steps)


def test_no_sentence_is_stuck_with_one_pause_length() -> None:
    pauses = [0, 0.3, 0.6, 0.9, 1.2]
    sentences = [{"tag": f"s{n}", "a": "甲", "b": "乙"} for n in range(5)]

    steps = kit.scenario_steps(sentences, pauses, repeat=5)

    seen = {tuple(step["tag"].rsplit("-p", 1)) for step in steps}
    assert len(seen) == 25, "five sentences x five pauses, each pair exactly once"


def test_halves_items_are_bank_items_for_both_halves() -> None:
    items = kit.halves_items(SENTENCES)

    assert [item["id"] for item in items] == ["dino-a", "dino-b", "rain-a", "rain-b"]
    assert [item["text"] for item in items][:2] == ["恐龙都", "吃什么东西？"]
    assert all(item["voice"] and item["instruction"] for item in items)


def test_build_clips_writes_one_clip_per_sentence_and_pause_and_the_scenario(tmp_path: Path) -> None:
    halves = tmp_path / "halves"
    halves.mkdir()
    for sentence in SENTENCES:
        _wav(halves / f"{sentence['tag']}-a.wav", 0.2)
        _wav(halves / f"{sentence['tag']}-b.wav", 0.2)

    count = kit.build_clips(
        SENTENCES, halves, [0, 0.6], 2, tmp_path / "bank", tmp_path / "scenario.json"
    )

    assert count == 4
    assert sorted(path.name for path in (tmp_path / "bank").iterdir()) == [
        "dino-p0.6.wav",
        "dino-p0.wav",
        "rain-p0.6.wav",
        "rain-p0.wav",
    ]
    steps = json.loads((tmp_path / "scenario.json").read_text(encoding="utf-8"))
    assert [step["tag"] for step in steps] == ["dino-p0", "rain-p0.6", "rain-p0", "dino-p0.6"]
    # Every step names a clip that exists, which is what voice_soak.py --bank plays.
    assert all((tmp_path / "bank" / f"{step['tag']}.wav").exists() for step in steps)


def _utc_stamp(epoch: float) -> str:
    moment = datetime.fromtimestamp(epoch, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{int((epoch % 1) * 1e6):06d}000Z"


def _run(tmp_path: Path, plan: list[tuple[str, float, list[float], float | None]]) -> Path:
    """plan: (step tag, said at, bridge commit times after it, reply latency)."""

    base = 1_790_000_000.0
    run = tmp_path / "run"
    run.mkdir()
    rows = [{"kind": "start", "ts": base}]
    bridge: list[str] = ["2026-10-03T00:00:00.000000000Z unrelated line"]
    for index, (tag, offset, commits, latency) in enumerate(plan, start=1):
        said_at = base + offset
        rows.append({"kind": "said", "tag": f"t{index:03d}-{tag}", "ts": said_at})
        if latency is not None:
            rows.append({"kind": "reply", "tag": f"t{index:03d}-{tag}", "latency_s": latency, "ts": said_at + 5})
        bridge.extend(f"{_utc_stamp(said_at + c)} media turn committed session=s turn_id=1" for c in commits)
    bridge.append(f"{_utc_stamp(base + 12.5)} media pending turn split after reply session=s boundary=x")
    (run / "timeline.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (run / "bridge.log").write_text("\n".join(bridge) + "\n", encoding="utf-8")
    return run


def test_report_counts_the_turns_the_server_committed_per_pause_length(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        [
            ("dino-p0", 10, [2.0], 3.1),
            ("dino-p0.6", 30, [2.5], 3.3),
            ("rain-p0.6", 50, [2.4, 4.9], 3.2),  # cut into two turns
            ("rain-p0.9", 70, [2.6, 5.0], 3.4),
            ("dino-p0.9", 90, [2.2, 6.1], 3.0),
        ],
    )

    table = kit.report(run)

    rows = {line.split("|")[1].strip(): [c.strip() for c in line.split("|")[2:-1]] for line in table.splitlines()[2:]}
    assert rows["0"][:4] == ["1", "1", "0", "0%"]
    assert rows["0.6"][:4] == ["2", "1", "1", "50%"]
    assert rows["0.9"][:4] == ["2", "0", "2", "100%"]
    assert rows["0.6"][5] == "3.25"
    assert sum(int(cells[4]) for cells in rows.values()) == 1, "the one pending-turn split is counted once"


def test_report_without_pause_steps_or_without_epoch_stamps_says_so(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "timeline.jsonl").write_text(json.dumps({"kind": "said", "tag": "t001-hello", "ts": 1}) + "\n")
    (run / "bridge.log").write_text("")
    assert "no pause-split steps" in kit.report(run)

    (run / "timeline.jsonl").write_text(json.dumps({"kind": "said", "tag": "t001-dino-p0.3"}) + "\n")
    with pytest.raises(SystemExit, match="ts"):
        kit.report(run)
