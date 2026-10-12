#!/usr/bin/env python3
"""Verify a bench PCM journal from the PCMD lines of a serial log and write one WAV per channel (TODOLIST N-10).

A bench image answers `pcmarm` by recording a fixed window and `pcmdump` by streaming the sealed journal as log
lines. The format is documented in firmware/esp32/overlay/files/main/memoria/memoria_diag_pcm.h:

    PCMD <id> begin fmt=memoria-diagpcm-v1 bytes=<B> chunks=<N>
    PCMD <id> <seq>/<N> <crc> <base64>
    PCMD <id> end <N> <B> <crc> mic=<m> blocks=<p>,<f>,<a> drops=<p>,<f>,<a> cut=<p>,<f>,<a>
    PCMD <id> failed reason=<why>

    python scripts/diag_pcm_extract.py robot.log --out pcm/            # the latest journal
    python scripts/diag_pcm_extract.py robot.log --id 0001a2b3 --out pcm/
    python scripts/diag_pcm_extract.py robot.log --list

Fail closed: every check below must pass, or the journal is damaged and only manifest.json is written (--partial
writes the WAVs too, and the manifest still says damaged). Exit 0 intact, 2 damaged, 1 nothing usable.

The channels and only these: play_pre_i2s (24 kHz, the bytes written to I2S, not the exact DAC signal),
pre_afe_mic and pre_afe_ref (16 kHz; ref is the second ES7210 ADC channel, not a copy of playback), post_afe
(16 kHz, AEC and NS merged). blocks.jsonl stamps every block with mic_clock, 16 kHz mic samples since arm, the
one timebase all lanes share. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import enum
import json
import re
import struct
import sys
import time
import wave
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = "memoria-diagpcm-v1"
CHUNK_BYTES = 384
BLOCK_MAGIC = 0xD1A6
PREAMBLE_MAGIC = b"MDPCM1\0\0"
_PREAMBLE = struct.Struct("<8s6I")
_LANE_SUMMARY = struct.Struct("<BBHIIIHH")
_BLOCK = struct.Struct("<HBBIIIIHH")
PREAMBLE_BYTES = _PREAMBLE.size + 3 * _LANE_SUMMARY.size


class Lane(enum.IntEnum):
    PLAY = 0
    FEED = 1
    POST_AFE = 2


@dataclass(frozen=True)
class LaneSpec:
    rate: int
    channels: tuple[str, ...]


LANES: dict[Lane, LaneSpec] = {
    Lane.PLAY: LaneSpec(24000, ("play_pre_i2s",)),
    Lane.FEED: LaneSpec(16000, ("pre_afe_mic", "pre_afe_ref")),
    Lane.POST_AFE: LaneSpec(16000, ("post_afe",)),
}

FLAGS = {
    1 << 0: "settle_dropped",
    1 << 1: "output_reenabled",
    1 << 2: "wake_consumer",
    1 << 3: "uplink_consumer",
}

_LINE = re.compile(
    r"PCMD (?P<id>[0-9a-f]{8}) (?:"
    r"(?P<seq>\d+)/(?P<total>\d+) (?P<crc>[0-9a-f]{8}) (?P<data>[A-Za-z0-9+/=]+)"
    r"|begin fmt=(?P<fmt>\S+) bytes=(?P<begin_bytes>\d+) chunks=(?P<begin_total>\d+)"
    r"|end (?P<end_total>\d+) (?P<end_bytes>\d+) (?P<end_crc>[0-9a-f]{8}) mic=(?P<mic>\d+)"
    r" blocks=(?P<blocks>\d+,\d+,\d+) drops=(?P<drops>\d+,\d+,\d+) cut=(?P<cut>\d+,\d+,\d+)"
    r"|failed reason=(?P<reason>[a-z_]+)"
    r"|(?P<armed>armed) "
    r")"
)

# What the robot logs for a line it does not know: a product image has no PCM verbs.
IGNORED_COMMAND = "usb command ignored (not a command)"


class Check(enum.Enum):
    CHUNKS_COMPLETE = "chunks_complete"
    JOURNAL_CRC = "journal_crc"
    PREAMBLE = "preamble"
    END_AGREES = "end_agrees"
    BLOCK_FRAMING = "block_framing"
    LANE_SEQ = "lane_seq"
    LANE_SAMPLE = "lane_sample"
    FEED_ON_CLOCK = "feed_on_clock"
    MIC_MONOTONIC = "mic_monotonic"
    NO_DROPS = "no_drops"


class CaptureError(RuntimeError):
    """The log holds no usable journal (none, the robot refused, or nothing finished in time)."""


@dataclass(frozen=True)
class EndLine:
    total: int
    nbytes: int
    crc: int
    mic: int
    blocks: tuple[int, ...]
    drops: tuple[int, ...]
    cut: tuple[int, ...]


@dataclass
class Transcript:
    id: str
    armed: bool = False
    fmt: str | None = None
    total: int = 0
    nbytes: int = 0
    chunks: dict[int, bytes] = field(default_factory=dict)
    bad: dict[int, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    end: EndLine | None = None
    failed: str | None = None

    @property
    def done(self) -> bool:
        return self.end is not None or self.failed is not None

    def raw(self) -> bytes | None:
        if self.total == 0 or any(seq not in self.chunks for seq in range(1, self.total + 1)):
            return None
        return b"".join(self.chunks[seq] for seq in range(1, self.total + 1))


@dataclass(frozen=True)
class LaneSummary:
    lane: int
    channels: int
    rate: int
    used_bytes: int
    blocks: int
    drops: int
    cut: int


@dataclass(frozen=True)
class Preamble:
    magic: bytes
    id: int
    window_ms: int
    mic_samples_at_seal: int
    arm_us_lo: int
    seal_us_lo: int
    lane_count: int
    lanes: tuple[LaneSummary, ...]


@dataclass(frozen=True)
class Block:
    magic: int
    lane: int
    flags: int
    lane_seq: int
    lane_sample: int
    mic_clock: int
    t_us: int
    frames: int
    channels: int
    pcm: bytes


@dataclass
class Verdict:
    id: str
    checks: dict[Check, bool]
    details: list[str]
    preamble: Preamble | None = None
    lanes: dict[Lane, list[Block]] = field(default_factory=dict)

    @property
    def intact(self) -> bool:
        return all(self.checks.values())


def _triple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(","))


def collect(text: str) -> dict[str, Transcript]:
    """Every journal in `text` by id, in the order its first line appears. Other output is ignored."""
    found: dict[str, Transcript] = {}
    for line in text.splitlines():
        match = _LINE.search(line)
        if match is None:
            continue
        t = found.setdefault(match["id"], Transcript(match["id"]))
        if match["reason"] is not None:
            t.failed = match["reason"]
        elif match["armed"] is not None:
            t.armed = True
        elif match["fmt"] is not None:
            nbytes, total = int(match["begin_bytes"]), int(match["begin_total"])
            if t.total and (t.total, t.nbytes) != (total, nbytes):
                t.notes.append(f"begin lines disagree: {t.total}/{t.nbytes} then {total}/{nbytes}")
            t.fmt, t.total, t.nbytes = match["fmt"], total, nbytes
        elif match["end_total"] is not None:
            t.end = EndLine(
                int(match["end_total"]),
                int(match["end_bytes"]),
                int(match["end_crc"], 16),
                int(match["mic"]),
                _triple(match["blocks"]),
                _triple(match["drops"]),
                _triple(match["cut"]),
            )
        else:
            _add_chunk(t, int(match["seq"]), int(match["total"]), match["crc"], match["data"])
    return found


def _add_chunk(t: Transcript, seq: int, total: int, crc: str, data: str) -> None:
    if t.total and total != t.total:
        t.notes.append(f"chunk {seq} says {total} chunks, begin says {t.total}")
        return
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        t.bad[seq] = "not valid base64"
        return
    if zlib.crc32(decoded) != int(crc, 16):
        t.bad[seq] = "crc mismatch"
        return
    known = t.chunks.get(seq)
    if known is not None and known != decoded:
        t.notes.append(f"chunk {seq} appears twice with different data")
        return
    t.chunks[seq] = decoded
    t.bad.pop(seq, None)


def parse_journal(raw: bytes) -> tuple[Preamble, dict[Lane, list[Block]], list[str]]:
    """The preamble and every lane's blocks, plus framing problems. Raises ValueError if the preamble is unreadable."""
    if len(raw) < PREAMBLE_BYTES:
        raise ValueError(f"{len(raw)} bytes is shorter than the {PREAMBLE_BYTES}-byte preamble")
    magic, pid, window_ms, mic, arm_lo, seal_lo, lane_count = _PREAMBLE.unpack_from(raw, 0)
    summaries = []
    for n in range(len(Lane)):
        lane, channels, _reserved, rate, used, blocks, drops, cut = _LANE_SUMMARY.unpack_from(
            raw, _PREAMBLE.size + n * _LANE_SUMMARY.size
        )
        summaries.append(LaneSummary(lane, channels, rate, used, blocks, drops, cut))
    preamble = Preamble(magic, pid, window_ms, mic, arm_lo, seal_lo, lane_count, tuple(summaries))
    problems: list[str] = []
    lanes: dict[Lane, list[Block]] = {lane: [] for lane in Lane}
    at = PREAMBLE_BYTES
    for lane, summary in zip(Lane, summaries, strict=True):
        end = at + summary.used_bytes
        if end > len(raw):
            problems.append(f"lane {lane.name.lower()} claims {summary.used_bytes} bytes past the journal end")
            break
        while at < end:
            if at + _BLOCK.size > end:
                problems.append(f"lane {lane.name.lower()}: {end - at} trailing bytes are not a block")
                break
            fields = _BLOCK.unpack_from(raw, at)
            pcm_bytes = fields[7] * fields[8] * 2
            body = at + _BLOCK.size
            if body + pcm_bytes > end:
                problems.append(f"lane {lane.name.lower()}: block at {at} runs past the lane")
                break
            lanes[lane].append(Block(*fields, raw[body : body + pcm_bytes]))
            at = body + pcm_bytes
        at = end
    if at != len(raw):
        problems.append(f"lanes cover {at} bytes, the journal has {len(raw)}")
    return preamble, lanes, problems


def verify(t: Transcript) -> Verdict:
    """Run every check; never raises. A check that cannot run (no journal bytes) fails."""
    checks = {check: False for check in Check}
    details: list[str] = []
    verdict = Verdict(t.id, checks, details)
    if t.failed is not None:
        details.append(f"the robot refused: reason={t.failed}")
        return verdict

    missing = [seq for seq in range(1, t.total + 1) if seq not in t.chunks]
    if t.fmt != FORMAT:
        details.append(f"format {t.fmt!r}, expected {FORMAT}")
    if t.end is None:
        details.append("no end line: the stream is cut or still running")
    if missing:
        details.append(f"missing chunks {_ranges(missing)} of {t.total}")
    details.extend(f"chunk {seq}: {why}" for seq, why in sorted(t.bad.items()))
    details.extend(t.notes)
    checks[Check.CHUNKS_COMPLETE] = (
        t.fmt == FORMAT and t.end is not None and t.total > 0 and not missing and not t.notes and t.end.total == t.total
    )
    raw = t.raw()
    if raw is None:
        return verdict

    crc = zlib.crc32(raw)
    checks[Check.JOURNAL_CRC] = t.end is not None and crc == t.end.crc and len(raw) == t.end.nbytes == t.nbytes
    if not checks[Check.JOURNAL_CRC]:
        details.append(f"journal crc {crc:08x} / {len(raw)} bytes does not match the begin and end lines")

    try:
        preamble, lanes, framing = parse_journal(raw)
    except ValueError as exc:
        details.append(str(exc))
        return verdict
    verdict.preamble, verdict.lanes = preamble, lanes
    summaries = preamble.lanes

    shape_ok = all(
        (s.lane, s.channels, s.rate) == (lane.value, len(LANES[lane].channels), LANES[lane].rate)
        for lane, s in zip(Lane, summaries, strict=True)
    )
    checks[Check.PREAMBLE] = (
        preamble.magic == PREAMBLE_MAGIC
        and preamble.lane_count == len(Lane)
        and shape_ok
        and f"{preamble.id:08x}" == t.id
        and PREAMBLE_BYTES + sum(s.used_bytes for s in summaries) == len(raw)
    )
    if not checks[Check.PREAMBLE]:
        details.append("preamble does not describe this journal (magic, id, lane shapes or sizes)")

    if t.end is not None:
        checks[Check.END_AGREES] = (
            t.end.mic == preamble.mic_samples_at_seal
            and t.end.blocks == tuple(s.blocks for s in summaries)
            and t.end.drops == tuple(s.drops for s in summaries)
            and t.end.cut == tuple(s.cut for s in summaries)
        )
        if not checks[Check.END_AGREES]:
            details.append("end line counters disagree with the preamble")

    for lane, summary in zip(Lane, summaries, strict=True):
        for block in lanes[lane]:
            if block.magic != BLOCK_MAGIC or block.lane != lane or block.channels != len(LANES[lane].channels):
                framing.append(f"lane {lane.name.lower()} seq {block.lane_seq}: bad magic, lane or channel count")
                break
        if len(lanes[lane]) != summary.blocks:
            framing.append(f"lane {lane.name.lower()}: {len(lanes[lane])} blocks, preamble says {summary.blocks}")
    checks[Check.BLOCK_FRAMING] = not framing
    details.extend(framing)

    seq_ok = sample_ok = mono_ok = True
    for lane in Lane:
        blocks = lanes[lane]
        for index, block in enumerate(blocks):
            if block.lane_seq != index:
                details.append(f"lane_seq lane={lane.name.lower()} expected {index}, got {block.lane_seq}")
                seq_ok = False
                break
        for prev, block in zip(blocks, blocks[1:], strict=False):
            if block.lane_sample != prev.lane_sample + prev.frames:
                details.append(f"lane_sample lane={lane.name.lower()} gap after seq {prev.lane_seq}")
                sample_ok = False
                break
        if blocks and blocks[0].lane_sample != 0:
            details.append(f"lane_sample lane={lane.name.lower()} starts at {blocks[0].lane_sample}")
            sample_ok = False
        for prev, block in zip(blocks, blocks[1:], strict=False):
            if block.mic_clock < prev.mic_clock:
                details.append(f"mic_clock lane={lane.name.lower()} goes back at seq {block.lane_seq}")
                mono_ok = False
                break
        if blocks and blocks[-1].mic_clock > preamble.mic_samples_at_seal:
            details.append(f"mic_clock lane={lane.name.lower()} passes the seal")
            mono_ok = False
    checks[Check.LANE_SEQ] = seq_ok
    checks[Check.LANE_SAMPLE] = sample_ok
    checks[Check.MIC_MONOTONIC] = mono_ok

    feed = lanes[Lane.FEED]
    offsets = {block.mic_clock - block.lane_sample for block in feed}
    checks[Check.FEED_ON_CLOCK] = len(offsets) <= 1
    if not checks[Check.FEED_ON_CLOCK]:
        details.append("feed lane is not contiguous on the mic clock: a feed block was lost")

    checks[Check.NO_DROPS] = all(s.drops == 0 for s in summaries)
    if not checks[Check.NO_DROPS]:
        details.append("drops " + ",".join(str(s.drops) for s in summaries) + " (play, feed, post_afe)")
    if any(s.cut for s in summaries):
        details.append("cut " + ",".join(str(s.cut) for s in summaries) + " (a block raced the seal; reported only)")
    return verdict


def _ranges(numbers: list[int]) -> str:
    out: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:] + [None]:  # type: ignore[list-item]
        if number is not None and number == previous + 1:
            previous = number
            continue
        out.append(str(start) if start == previous else f"{start}-{previous}")
        if number is not None:
            start = previous = number
    return ", ".join(out)


def channel_pcm(lane: Lane, blocks: list[Block]) -> dict[str, bytes]:
    """Each channel of a lane as little-endian s16 mono, blocks in lane order."""
    names = LANES[lane].channels
    joined = b"".join(block.pcm for block in blocks)
    if len(names) == 1:
        return {names[0]: joined}
    samples = struct.unpack(f"<{len(joined) // 2}h", joined)
    return {name: struct.pack(f"<{len(samples[i :: len(names)])}h", *samples[i :: len(names)]) for i, name in enumerate(names)}


def _write_wav(path: Path, rate: int, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)


def export(t: Transcript, out: Path, *, partial: bool = False) -> Verdict:
    """Write manifest.json always; the WAVs and blocks.jsonl only for an intact journal or with `partial`."""
    verdict = verify(t)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    if verdict.preamble is not None and (verdict.intact or partial):
        with (out / "blocks.jsonl").open("w", encoding="utf-8") as handle:
            for lane in Lane:
                for block in verdict.lanes[lane]:
                    record = {
                        "lane": "+".join(LANES[lane].channels),
                        "lane_seq": block.lane_seq,
                        "lane_sample": block.lane_sample,
                        "mic_clock": block.mic_clock,
                        "t_us": block.t_us,
                        "frames": block.frames,
                        "flags": [name for bit, name in FLAGS.items() if block.flags & bit],
                    }
                    handle.write(json.dumps(record) + "\n")
        written.append("blocks.jsonl")
        for lane in Lane:
            for name, pcm in channel_pcm(lane, verdict.lanes[lane]).items():
                _write_wav(out / f"{name}.wav", LANES[lane].rate, pcm)
                written.append(f"{name}.wav")
    manifest = {
        "format": FORMAT,
        "id": t.id,
        "intact": verdict.intact,
        "checks": {check.value: ok for check, ok in verdict.checks.items()},
        "details": verdict.details,
        "timebase": "mic_clock@16000",
        "window_ms": verdict.preamble.window_ms if verdict.preamble else None,
        "mic_samples_at_seal": verdict.preamble.mic_samples_at_seal if verdict.preamble else None,
        "lanes": [
            {
                "channels": list(LANES[lane].channels),
                "rate": LANES[lane].rate,
                "blocks": s.blocks,
                "drops": s.drops,
                "cut": s.cut,
            }
            for lane, s in zip(Lane, verdict.preamble.lanes, strict=True)
        ]
        if verdict.preamble
        else [],
        "files": written,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return verdict


def read_text(path: Path, offset: int = 0) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def wait_for(
    path: Path,
    offset: int,
    timeout: float,
    done: Callable[[Transcript], bool],
    *,
    poll: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Transcript:
    """The first journal from log position `offset` on for which `done` holds, or a refusal."""
    deadline = clock() + timeout
    while True:
        text = read_text(path, offset)
        for t in collect(text).values():
            if t.failed is not None or done(t):
                return t
        if IGNORED_COMMAND in text:
            raise CaptureError(
                "the robot ignored the command (usb command ignored): it is not running a bench image; "
                "build and flash one with firmware/esp32/scripts/build.sh --bench and flash.sh --bench"
            )
        if clock() >= deadline:
            raise CaptureError(f"no PCMD answer in {timeout:.0f} s")
        sleep(poll)


def armed(t: Transcript) -> bool:
    return t.armed


def dumped(t: Transcript) -> bool:
    return t.end is not None and t.fmt is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("log", type=Path, help="serial log with the PCMD lines (the logger's output)")
    parser.add_argument("--out", type=Path, default=None, help="directory for the WAVs and manifest.json")
    parser.add_argument("--id", default=None, help="which journal (8 hex digits); default: the latest")
    parser.add_argument("--list", action="store_true", help="list the journals in the log and exit")
    parser.add_argument("--partial", action="store_true", help="write the WAVs of a damaged journal too")
    args = parser.parse_args(argv)

    found = collect(read_text(args.log))
    if args.list:
        for t in found.values():
            verdict = verify(t)
            print(f"{t.id} {len(t.chunks)}/{t.total} chunks: {'intact' if verdict.intact else '; '.join(verdict.details)}")
        return 0
    candidates = [t for t in found.values() if t.total or t.failed]
    if args.id is not None:
        candidates = [t for t in candidates if t.id == args.id]
    if not candidates:
        print(f"error: no dumped PCMD journal{f' {args.id}' if args.id else ''} in {args.log}", file=sys.stderr)
        return 1
    t = candidates[-1]
    out = args.out or Path(f"pcm-{t.id}")
    verdict = export(t, out, partial=args.partial)
    state = "intact" if verdict.intact else "DAMAGED: " + "; ".join(verdict.details)
    print(f"journal {t.id} -> {out}: {state}")
    return 0 if verdict.intact else 2


if __name__ == "__main__":
    sys.exit(main())
