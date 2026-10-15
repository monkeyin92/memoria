"""The bench PCM journal checker against the robot's real recorder and encoder (TODOLIST N-10).

firmware/esp32/tests/diag_pcm_tool.cc drives the exact memoria_diag_pcm.h the bench image runs; these tests
feed its PCMD lines, interleaved with ordinary log output, to scripts/diag_pcm_extract.py and then break the
journal one way at a time: each damage must fail its own check and only that one.
"""

from __future__ import annotations

import base64
import json
import shutil
import struct
import subprocess
import wave
import zlib
from pathlib import Path

import pytest
from scripts import diag_pcm_extract as extract

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware" / "esp32"
TOOL_SOURCE = FIRMWARE / "tests" / "diag_pcm_tool.cc"
MEMORIA_DIR = FIRMWARE / "overlay" / "files" / "main" / "memoria"

SCENARIO = [
    "arm 0001a2b3 1000000",
    "feed 160 2 1 1000100",
    "post 512 12 1000150",
    "play 1440 24000 2 1000200",
    "feed 160 2 0 1010100",
    "feed 160 2 0 1020100",
    "post 512 4 1032000",
    "play 1440 24000 0 1060200",
    "tick 4000000",
    "dump",
]

# Byte offsets of the scenario's blocks: 112-byte preamble (4 lane summaries), then play (2 x 24+2880),
# feed (3 x 24+640), post_afe (2 x 24+1024). The raw TDM lane is empty in this scenario.
PLAY_2 = 112 + 2904
FEED_0 = 112 + 2 * 2904
POST_0 = FEED_0 + 3 * 664
POST_2 = POST_0 + 1048
MIC_CLOCK, LANE_SEQ, LANE_SAMPLE = 12, 4, 8


@pytest.fixture(scope="module")
def tool(tmp_path_factory) -> Path:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    binary = tmp_path_factory.mktemp("diag") / "diag_pcm_tool"
    built = subprocess.run(
        [compiler, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror", "-pthread",
         "-DCONFIG_MEMORIA_BENCH_SERIAL=1", "-I", str(MEMORIA_DIR), str(TOOL_SOURCE), "-o", str(binary)],
        capture_output=True, text=True, check=False,
    )
    assert built.returncode == 0, built.stderr
    return binary


def run(tool: Path, *lines: str) -> list[str]:
    done = subprocess.run([str(tool)], input="\n".join(lines) + "\n", text=True, capture_output=True, check=True)
    return done.stdout.splitlines()


def pcmd(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("PCMD ")]


def log_of(lines: list[str]) -> str:
    noisy = []
    for i, line in enumerate(lines):
        noisy.append(f"[12:00:{i % 60:02d}] I ({i}) MemoriaDiagPcm: {line}" if i % 3 == 0 else line)
        noisy.append("I (99) AudioService: unrelated output in between")
    return "\n".join(noisy)


def journal(lines: list[str]) -> bytes:
    chunks = [line.split()[4] for line in pcmd(lines) if "/" in line.split()[2]]
    return b"".join(base64.b64decode(chunk) for chunk in chunks)


def relines(raw: bytes, end_tail: str, pid: str = "0001a2b3") -> list[str]:
    """The PCMD lines of a hand-modified journal, every CRC recomputed, so only the structure is wrong."""
    total = -(-len(raw) // 384)
    out = [f"PCMD {pid} begin fmt=memoria-diagpcm-v1 bytes={len(raw)} chunks={total}"]
    for seq in range(1, total + 1):
        chunk = raw[(seq - 1) * 384 : seq * 384]
        out.append(f"PCMD {pid} {seq}/{total} {zlib.crc32(chunk):08x} {base64.b64encode(chunk).decode()}")
    out.append(f"PCMD {pid} end {total} {len(raw)} {zlib.crc32(raw):08x} {end_tail}")
    return out


def poke(raw: bytes, at: int, value: int, fmt: str = "<I") -> bytes:
    buffer = bytearray(raw)
    struct.pack_into(fmt, buffer, at, value)
    return bytes(buffer)


def failed_checks(text: str) -> set[str]:
    (t,) = extract.collect(text).values()
    return {check.value for check, ok in extract.verify(t).checks.items() if not ok}


def end_tail(lines: list[str]) -> str:
    end = next(line for line in pcmd(lines) if " end " in line)
    return end.split(" ", 6)[6]


def test_an_intact_journal_passes_every_check_and_exports_sample_exact_channels(tool, tmp_path) -> None:
    lines = run(tool, *SCENARIO)
    assert lines[0] == "ok" and lines[1] == "sealed"
    assert pcmd(lines)[-1].endswith(
        "end 27 10008 "
        + pcmd(lines)[-1].split()[5]
        + " mic=480 blocks=2,3,2,0 drops=0,0,0,0 cut=0,0,0,0 raw_hz=24000 raw_bits=16 raw_slots=4"
    )

    (t,) = extract.collect(log_of(lines)).values()
    verdict = extract.export(t, tmp_path)
    assert verdict.intact, verdict.details

    def wav(name: str) -> tuple[int, tuple[int, ...]]:
        with wave.open(str(tmp_path / f"{name}.wav")) as handle:
            frames = handle.readframes(handle.getnframes())
            return handle.getframerate(), struct.unpack(f"<{len(frames) // 2}h", frames)

    assert wav("pre_afe_mic") == (16000, tuple(range(480)))
    assert wav("pre_afe_ref") == (16000, tuple(-v for v in range(480)))
    assert wav("post_afe") == (16000, tuple(range(1024)))
    assert wav("play_pre_i2s") == (24000, tuple(range(2880)))

    blocks = [json.loads(line) for line in (tmp_path / "blocks.jsonl").read_text().splitlines()]
    assert [(b["lane"], b["lane_seq"], b["mic_clock"], b["flags"]) for b in blocks] == [
        ("play_pre_i2s", 0, 160, ["output_reenabled"]),
        ("play_pre_i2s", 1, 480, []),
        ("pre_afe_mic+pre_afe_ref", 0, 0, ["settle_dropped"]),
        ("pre_afe_mic+pre_afe_ref", 1, 160, []),
        ("pre_afe_mic+pre_afe_ref", 2, 320, []),
        ("post_afe", 0, 160, ["wake_consumer", "uplink_consumer"]),
        ("post_afe", 1, 480, ["wake_consumer"]),
    ]
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["intact"] is True
    assert manifest["timebase"] == "mic_clock@16000"
    assert (manifest["raw_hz"], manifest["raw_bits"], manifest["raw_slots"]) == (24000, 16, 4)
    assert [lane["channels"] for lane in manifest["lanes"]] == [
        ["play_pre_i2s"],
        ["pre_afe_mic", "pre_afe_ref"],
        ["post_afe"],
        ["es7210_slot0", "es7210_slot1", "es7210_slot2", "es7210_slot3"],
    ]
    assert sorted(manifest["files"]) == sorted(
        [
            "blocks.jsonl",
            "play_pre_i2s.wav",
            "pre_afe_mic.wav",
            "pre_afe_ref.wav",
            "post_afe.wav",
            "es7210_slot0.wav",
            "es7210_slot1.wav",
            "es7210_slot2.wav",
            "es7210_slot3.wav",
        ]
    )
    assert wav("es7210_slot0") == (24000, ())


def test_raw_tdm_exports_four_slots_and_does_not_move_the_mic_clock(tool, tmp_path) -> None:
    lines = run(tool, "arm 00000041 0", "raw 4 10", "tick 3000000", "dump")
    assert lines[0] == "ok" and lines[1] == "sealed"
    end = pcmd(lines)[-1]
    assert " mic=0 " in end
    assert "blocks=0,0,0,1" in end
    (t,) = extract.collect(log_of(lines)).values()
    verdict = extract.export(t, tmp_path)
    assert verdict.intact, verdict.details

    def wav(name: str) -> tuple[int, ...]:
        with wave.open(str(tmp_path / f"{name}.wav")) as handle:
            frames = handle.readframes(handle.getnframes())
            assert handle.getframerate() == 24000
            return struct.unpack(f"<{len(frames) // 2}h", frames)

    assert wav("es7210_slot0") == (10, 10, 10, 10)
    assert wav("es7210_slot1") == (11, 11, 11, 11)
    assert wav("es7210_slot2") == (12, 12, 12, 12)
    assert wav("es7210_slot3") == (13, 13, 13, 13)


def test_a_re_dump_of_the_same_journal_is_one_journal(tool) -> None:
    lines = run(tool, *SCENARIO, "dump")
    found = extract.collect(log_of(lines))
    assert list(found) == ["0001a2b3"]
    assert extract.verify(found["0001a2b3"]).intact


def test_a_lost_chunk_fails_closed_and_names_the_chunk(tool, tmp_path) -> None:
    lines = [line for line in run(tool, *SCENARIO) if " 3/27 " not in line]
    (t,) = extract.collect(log_of(lines)).values()
    verdict = extract.export(t, tmp_path)
    assert not verdict.intact
    assert not verdict.checks[extract.Check.CHUNKS_COMPLETE]
    assert "missing chunks 3 of 27" in verdict.details
    assert json.loads((tmp_path / "manifest.json").read_text())["files"] == []


def test_a_flipped_pcm_byte_fails_only_the_journal_crc(tool) -> None:
    lines = run(tool, *SCENARIO)
    raw = bytearray(journal(lines))
    raw[-2] ^= 0xFF  # inside post_afe PCM: framing still parses
    damaged = relines(bytes(raw), end_tail(lines))
    damaged[-1] = damaged[-1].replace(f"{zlib.crc32(bytes(raw)):08x}", f"{zlib.crc32(journal(lines)):08x}")
    assert failed_checks("\n".join(damaged)) == {"journal_crc"}


@pytest.mark.parametrize(
    ("at", "value", "fmt", "check"),
    [
        (FEED_0 + 664 + LANE_SEQ, 5, "<I", "lane_seq"),
        (POST_2 + LANE_SAMPLE, 513, "<I", "lane_sample"),
        (FEED_0 + 2 * 664 + MIC_CLOCK, 400, "<I", "feed_on_clock"),
        (PLAY_2 + MIC_CLOCK, 0, "<I", "mic_monotonic"),
        (POST_0, 0xBEEF, "<H", "block_framing"),
        (0, 0x5A, "<B", "preamble"),
    ],
)
def test_each_structural_damage_fails_exactly_its_check(tool, at, value, fmt, check) -> None:
    lines = run(tool, *SCENARIO)
    assert failed_checks("\n".join(relines(poke(journal(lines), at, value, fmt), end_tail(lines)))) == {check}


def test_an_end_line_that_disagrees_with_the_preamble_fails_end_agrees(tool) -> None:
    lines = run(tool, *SCENARIO)
    tail = end_tail(lines).replace("drops=0,0,0", "drops=1,0,0")
    assert failed_checks("\n".join(relines(journal(lines), tail))) == {"end_agrees"}


def test_a_refused_playback_block_is_a_drop_and_the_journal_is_damaged(tool) -> None:
    scenario = SCENARIO[:-2] + ["play 480 16000 0 1070000"] + SCENARIO[-2:]
    lines = run(tool, *scenario)
    assert failed_checks("\n".join(lines)) == {"no_drops"}


def test_the_robot_refusing_is_reported_not_exported(tool, tmp_path) -> None:
    lines = run(tool, "dump", "arm 00000001 0", "dump")
    assert pcmd(lines) == ["PCMD 00000000 failed reason=empty", "PCMD 00000001 failed reason=recording"]
    verdict = extract.verify(extract.collect("\n".join(lines))["00000001"])
    assert not verdict.intact and verdict.details == ["the robot refused: reason=recording"]


def test_racing_writers_never_corrupt_a_sealed_journal(tool) -> None:
    lines = run(tool, "stress 200")
    assert lines[0] == "armed"
    # Three writers hammer their lanes far faster than real time, so lanes fill and refuse blocks; everything
    # that was committed must still frame, sequence and stamp exactly.
    assert failed_checks("\n".join(lines)) == {"no_drops"}


def test_main_exit_codes_say_intact_damaged_or_nothing(tool, tmp_path) -> None:
    log = tmp_path / "robot.log"
    log.write_text(log_of(run(tool, *SCENARIO)))
    assert extract.main([str(log), "--out", str(tmp_path / "ok")]) == 0
    assert (tmp_path / "ok" / "post_afe.wav").exists()

    damaged = tmp_path / "damaged.log"
    damaged.write_text("\n".join(line for line in run(tool, *SCENARIO) if " 7/27 " not in line))
    assert extract.main([str(damaged), "--out", str(tmp_path / "bad")]) == 2
    assert not (tmp_path / "bad" / "post_afe.wav").exists()
    assert extract.main([str(damaged), "--out", str(tmp_path / "partial"), "--partial"]) == 2
    assert not (tmp_path / "partial" / "post_afe.wav").exists()  # nothing to rebuild without chunk 7

    empty = tmp_path / "empty.log"
    empty.write_text("I (1) nothing here\n")
    assert extract.main([str(empty)]) == 1


def test_waiting_ends_on_the_answer_or_on_a_product_image(tmp_path, tool) -> None:
    log = tmp_path / "robot.log"
    log.write_text("old output\n")
    offset = log.stat().st_size
    log.write_text("old output\n" + "\n".join(run(tool, *SCENARIO)))
    t = extract.wait_for(log, offset, 1.0, extract.dumped, sleep=lambda _: None)
    assert t.id == "0001a2b3" and extract.verify(t).intact

    log.write_text("I (5) MemoriaUsbCommand: usb command ignored (not a command)\n")
    with pytest.raises(extract.CaptureError, match="not running a bench image"):
        extract.wait_for(log, 0, 1.0, extract.armed, sleep=lambda _: None)

    ticks = iter([0.0, 0.0, 2.0])
    log.write_text("")
    with pytest.raises(extract.CaptureError, match="no PCMD answer in 1 s"):
        extract.wait_for(log, 0, 1.0, extract.armed, sleep=lambda _: None, clock=lambda: next(ticks))
