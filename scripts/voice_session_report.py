#!/usr/bin/env python3
"""Report raw per-turn timings from one captured Memoria voice session.

Read-only: parses `serial.log` plus the streamed bridge/agent/edge logs from a
capture directory produced by `scripts/voice_session_capture.py` (or a legacy
`capture.py`).  It prints the per-delivery server chain and the device-side
`Device VAD end -> first downlink frame received` gap, so the 1.5s silence
criterion is judged from raw per-turn numbers rather than an average.  It
deliberately does not restate the pass/fail gates; the capture's own verifier
keeps that job.

Identity rules
--------------
* Server delivery facts are keyed by the complete delivery identity
  `(session_id, media epoch, turn_id, generation_id, tool_epoch)` recovered from
  `delivery_id=`.  Nothing is aggregated across sessions, epochs or tool epochs:
  two media sessions that both use turn 2 / generation 2 stay separate, and
  per-turn silence gaps never span a session boundary.
* Only `media turn committed` with a complete, exactly matching delivery key
  can supply a commit timestamp. Legacy PHASE and agent commits are partial
  evidence even if just one delivery is present. Repeated complete commits
  leave timing unknown. No nearest/latest-session attribution is performed.
* The close event's epoch is a device stream epoch, NOT a media session epoch.
  The two are different namespaces, so they are never matched by equal numeric
  value, by time proximity, or by a per-session latest/maximum guess.  Close
  timing is resolved only when the logs carry an explicit, complete and unique
  binding of that stream epoch to a complete delivery key
  `(session_id, media epoch, turn_id, generation_id, tool_epoch)`, such as a
  complete commit or pacing line that names both.  A missing binding, or one
  stream epoch mapping to several keys or media epochs (many-to-many), leaves
  close timing unknown.  The Edge does derive its connection epoch from the
  device media token and forwards it to Voice Core as StreamEpoch
  (services/media_edge/device_ws_session.go:146, :474, :522;
  device_ws_downlink.go:385; media_bridge_server.py:272), but that provenance
  only shows where the stream epoch came from: it still does not tie it to any
  media session epoch until a log line binds it to a full delivery key.
* Device serial lines carry no session or epoch identifier at all.  Device
  timings are therefore reported on independent local timelines that reset at
  every reboot/reconnect boundary, so a frame received after a reconnect can
  never inherit an anchor from the previous device session.
* A delivery that ends in a non-normal event (preempted/error/no_audio/
  transport_rejected/skipped) is reported as that terminal fact, never as a
  missing log line.

Evidence layers
---------------
`MemoriaProtocol: First playable downlink frame` is emitted by
`firmware/esp32/overlay/files/main/memoria/memoria_protocol.cc` when a frame
reaches the device media boundary, i.e. it is a receive/queue fact about the
first packet the device accepted for playback.  This report names it
`first_received` and never calls it audible: `audible` stays
`not_measured` until device playback terminal evidence plus operator listening
back it up (PROJECT_RULES).  A gap above 1.5s is printed as the real number with
an explicit marker instead of being hidden as stale.

Device playback metering is reported with its own scope.  The current firmware
emits "media playback supply wait" / "media playback supply summary" for
layer=playout_queue scope=software_queue_wait; a counted wait is a software
playout-queue wait, not a proven I2S/DMA underrun, and first_output_latency_ms is
a generation-announcement-to-codec-submission latency, not a first audible
sound.  Historical "media playback starved"/"meter" lines are kept and labelled
as the legacy meter.  A channel_flush close is reported verbatim and never
guessed as EOS, cancel, pause or a disconnect.

The downlink pacing line is reported as an after-pacer send measurement.  It is
parsed as key=value pairs, so the historical `produced_ratio` field and the
renamed `send_audio_ratio` together with `measurement=` and the
`session_epoch`/`stream_epoch`/`turn_id`/`generation_id`/`tool_epoch` fence
are both shown; a field this report does not know about is passed through rather
than dropped.  The ratio is never read as audio or DAC output.

Usage: `python scripts/voice_session_report.py <capture-dir>
[--firmware-receipt PATH]`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ------------------------------------------------------------------ device side

SERIAL_TS = re.compile(r"^\[(?P<ts>[^\]]+)\]")
SERIAL_UPTIME = re.compile(r"^[A-Z] \((?P<ms>\d+)\) ")
BOOT_MARKER = re.compile(r"^(?:ESP-ROM:|rst:0x)")
VAD = re.compile(
    r"MemoriaProtocol: Device VAD (?P<edge>start|end) at sample=(?P<sample>\d+) rms=(?P<rms>[\d.]+)"
)
FRAME = re.compile(
    r"MemoriaProtocol: First playable downlink frame generation=(?P<gen>\d+) seq=(?P<seq>\d+)"
)
STATE = re.compile(r"StateMachine: State: (?P<src>\S+) -> (?P<dst>\S+)")
SUPPLY = re.compile(r"media playback (?P<kind>supply wait|supply summary) ")
LEGACY_METER = re.compile(r"media playback (?P<kind>starved|meter|underrun(?: wait| summary)?) ")
# A reboot or a reconnect makes every earlier anchor meaningless, so the device
# timeline is cut into independent segments at these transitions: a new media
# session starts the next segment, and a return to a waiting state closes the
# current one.
SESSION_START = frozenset({"connecting"})
SESSION_END = frozenset({"idle", "recovering", "unknown", "starting", "activating"})
ACTIVITY_KINDS = frozenset({"vad.start", "vad.end", "frame_received"})

# ------------------------------------------------------------------ server side

LOG_TS = re.compile(r"^(?P<ts>\S+Z)\s")
DELIVERY = re.compile(
    r"media reply delivery session=(?P<sid>\S+) "
    r"delivery_id=(?P=sid)/epoch-(?P<epoch>\d+)/turn-(?P<turn>\d+)"
    r"/generation-(?P<gen>\d+)/tool-(?P<tool>\d+) event=(?P<event>\S+)"
)
MEDIA_COMMIT = re.compile(r"media turn committed ")
PHASE = re.compile(
    r"interaction_phase from=(?P<src>\S+) to=(?P<dst>\S+) cause=(?P<cause>\S+)"
    r" session_id=(?P<sid>\S+) turn_id=(?P<turn>\d+) generation_id=(?P<gen>\d+)"
)
TURN_COMMITTED = re.compile(
    r"services\.agent\.src\.agent:turn_committed turn_id=(?P<turn>\d+)"
    r" generation_id=(?P<gen>\d+)(?: tool_epoch=(?P<tool>\d+))?"
)
# The pacing line gained and renamed fields over time (measurement=, stream_epoch=,
# turn_id=/generation_id=/tool_epoch=, send_audio_ratio= replacing produced_ratio),
# so only the stable prefix is matched here and every field is read as a key=value
# pair.  A field this report does not know about is simply carried along.
PACING = re.compile(r"media downlink pacing ")
KEY_VALUE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>[^\s]*)")
CONVERSATION_CLOSE = re.compile(
    r"media edge projected conversation close session=(?P<sid>\S+) device=(?P<dev>\S+)"
    r" epoch=(?P<epoch>\d+) reason=(?P<reason>\S+)(?: control_sequence=(?P<ctrl>\d+))?"
)

STAGE_ORDER = ("first_frame_sent", "provider_completed", "actual_heard", "playback_ended")
NON_NORMAL_EVENTS = frozenset({"preempted", "error", "no_audio", "transport_rejected", "skipped"})
RECEIVE_TARGET_S = 1.5
KNOWN_PACING_FIELDS = frozenset(
    {
        "session",
        "measurement",
        "session_epoch",
        "stream_epoch",
        "turn_id",
        "generation_id",
        "tool_epoch",
        "reason",
        "frames",
        "audio_ms",
        "wall_ms",
        "max_gap_ms",
        "queue_high_water",
    }
)


@dataclass(frozen=True, order=True)
class DeliveryKey:
    """Complete server-side delivery identity; the aggregate key of this report."""

    session: str
    epoch: int
    turn: int
    generation: int
    tool: int

    def label(self) -> str:
        return (
            f"session {self.session} epoch {self.epoch} turn {self.turn} "
            f"gen {self.generation} tool {self.tool}"
        )


@dataclass
class DeviceEvent:
    when: datetime
    kind: str
    detail: str
    uptime_ms: int | None
    line: int
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryEvent:
    when: datetime
    event: str
    terminal: str
    terminal_reason: str
    fields: dict[str, str]


@dataclass
class Delivery:
    events: list[DeliveryEvent] = field(default_factory=list)

    def stage_time(self, stage: str) -> tuple[datetime | None, str]:
        times = {event.when for event in self.events if event.event == stage}
        if not times:
            return None, f"{stage} not observed in this capture"
        if len(times) != 1:
            return None, f"conflicting {stage} timestamps ({len(times)})"
        return next(iter(times)), ""

    def has(self, stage: str) -> bool:
        return any(event.event == stage for event in self.events)


@dataclass
class DeviceSegment:
    """One uninterrupted device timeline; anchors never cross into another."""

    index: int
    opened_by: str
    events: list[DeviceEvent] = field(default_factory=list)


@dataclass
class ServerFacts:
    deliveries: dict[DeliveryKey, Delivery] = field(default_factory=dict)
    duplicates: Counter[str] = field(default_factory=Counter)
    commits: list[tuple[datetime, dict[str, str]]] = field(default_factory=list)
    partial_commits: list[tuple[datetime, str, dict[str, str]]] = field(default_factory=list)
    pacing: list[tuple[datetime, dict[str, str]]] = field(default_factory=list)
    closes: list[tuple[datetime, str, str, int, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _device_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _server_time(raw: str) -> datetime:
    # Docker log timestamps carry nanoseconds; datetime keeps microsecond precision.
    head, _, rest = raw.partition(".")
    digits = re.match(r"\d+", rest)
    fraction = digits.group(0)[:6] if digits else ""
    return datetime.fromisoformat(f"{head}.{fraction or '0'}+00:00")


def _seconds(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


def _stamp(when: datetime) -> str:
    return when.strftime("%H:%M:%S.%f")[:-3]


def _read(path: Path) -> str:
    return path.read_text(errors="replace") if path.exists() else ""


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def _full_key(fields: dict[str, str]) -> tuple[DeliveryKey | None, str]:
    names = ("session", "session_epoch", "turn_id", "generation_id", "tool_epoch")
    missing = [name for name in names if fields.get(name) in (None, "", "unknown", "?")]
    if missing:
        return None, f"partial identity; missing {','.join(missing)}"
    invalid = [name for name in names[1:] if not re.fullmatch(r"[0-9]+", fields[name])]
    if invalid:
        return None, f"invalid identity fields: {','.join(invalid)}"
    return DeliveryKey(fields["session"], *(int(fields[name]) for name in names[1:])), ""


def device_segments(run: Path) -> tuple[list[DeviceSegment], list[str]]:
    """Split `serial.log` into independent device timelines.

    Device lines have no session identity, so a reboot (`ESP-ROM`/`rst` or a
    decreasing uptime counter) and every transition into a waiting state start a
    new segment.  A timing is only ever measured inside one segment.
    """

    notes: list[str] = []
    segments: list[DeviceSegment] = []
    current: DeviceSegment | None = None
    pending_reason: str | None = None
    previous_uptime: int | None = None
    parsed = 0
    seen_lines: dict[tuple[str, str], int] = {}
    repeats: list[tuple[int, int]] = []

    def open_segment(reason: str) -> DeviceSegment:
        segment = DeviceSegment(index=len(segments) + 1, opened_by=reason)
        segments.append(segment)
        return segment

    for number, raw in enumerate(_read(run / "serial.log").splitlines(), start=1):
        line = raw.strip()
        stamp = SERIAL_TS.match(line)
        if not stamp:
            continue
        duplicate_key = (stamp.group("ts"), line[stamp.end() :].strip())
        first_seen = seen_lines.get(duplicate_key)
        if first_seen is None:
            seen_lines[duplicate_key] = number
        else:
            # A byte-identical line is kept: dropping it could merge two real
            # firmware episodes (e.g. two supply summaries for one generation).
            repeats.append((number, first_seen))
        body = line[stamp.end() :].lstrip()
        try:
            when = _device_time(stamp.group("ts"))
        except ValueError:
            notes.append(f"serial.log line {number} has an invalid timestamp")
            continue
        uptime_match = SERIAL_UPTIME.match(body)
        uptime = int(uptime_match.group("ms")) if uptime_match else None
        parsed += 1

        state_match = STATE.search(body)
        destination = state_match.group("dst") if state_match else None
        is_boot = bool(BOOT_MARKER.match(body))

        if is_boot and (current is None or any(event.kind != "boot" for event in current.events)):
            current = open_segment(f"line {number}: device boot ({body.split(',')[0]})")
            pending_reason = None
            previous_uptime = None
        elif state_match is not None and destination in SESSION_START:
            current = open_segment(
                f"line {number}: device started a new media session at {_stamp(when)} "
                f"({state_match.group('src')} -> {destination}); anchors cleared"
            )
            pending_reason = None
        elif current is None:
            current = open_segment(f"line {number}: first parseable device line")
        elif uptime is not None and previous_uptime is not None and uptime < previous_uptime:
            current = open_segment(
                f"line {number}: device uptime counter reset ({previous_uptime} -> {uptime} ms)"
            )
            pending_reason = None
        elif pending_reason is not None:
            current = open_segment(pending_reason)
            pending_reason = None
        if uptime is not None:
            previous_uptime = uptime

        if is_boot:
            current.events.append(DeviceEvent(when, "boot", body.split(",")[0], uptime, number))
            continue

        if match := VAD.search(body):
            current.events.append(
                DeviceEvent(
                    when,
                    f"vad.{match.group('edge')}",
                    f"sample={match.group('sample')} rms={match.group('rms')}",
                    uptime,
                    number,
                )
            )
        elif match := FRAME.search(body):
            current.events.append(
                DeviceEvent(
                    when,
                    "frame_received",
                    f"generation={match.group('gen')} seq={match.group('seq')}",
                    uptime,
                    number,
                )
            )
        elif match := SUPPLY.search(body):
            current.events.append(
                DeviceEvent(
                    when,
                    f"supply.{match.group('kind').replace(' ', '_')}",
                    " ".join(f"{key}={value}" for key, value in KEY_VALUE.findall(body)),
                    uptime,
                    number,
                    dict(KEY_VALUE.findall(body)),
                )
            )
        elif match := LEGACY_METER.search(body):
            current.events.append(
                DeviceEvent(
                    when,
                    f"legacy_{match.group('kind').replace(' ', '_')}",
                    " ".join(f"{key}={value}" for key, value in KEY_VALUE.findall(body)),
                    uptime,
                    number,
                    dict(KEY_VALUE.findall(body)),
                )
            )
        elif state_match is not None:
            current.events.append(
                DeviceEvent(
                    when,
                    "state",
                    f"{state_match.group('src')} -> {destination}",
                    uptime,
                    number,
                )
            )
            if destination in SESSION_END and any(
                event.kind in ACTIVITY_KINDS for event in current.events
            ):
                pending_reason = (
                    f"line {number}: device left the active session at {_stamp(when)} "
                    f"({state_match.group('src')} -> {destination}); anchors cleared"
                )

    if repeats:
        examples = ", ".join(f"line {line} repeats line {first}" for line, first in repeats[:5])
        notes.append(
            f"serial.log has {len(repeats)} byte-identical repeated line(s), kept in original "
            f"order and not merged: {examples}"
        )
    if parsed == 0:
        notes.append("serial.log has no parseable device lines")
    kept = [segment for segment in segments if segment.events]
    for index, segment in enumerate(kept, start=1):
        segment.index = index
    return kept, notes


def server_facts(run: Path) -> tuple[ServerFacts, list[str]]:
    facts = ServerFacts()
    notes: list[str] = []
    for name in ("bridge.log", "agent.log", "edge.log"):
        text = _read(run / name)
        if not text:
            notes.append(f"{name} is missing or empty")
            continue
        for raw in text.splitlines():
            stamp = LOG_TS.match(raw)
            if not stamp:
                continue
            try:
                when = _server_time(stamp.group("ts"))
            except ValueError:
                notes.append(f"{name} contains an invalid timestamp")
                continue
            if match := DELIVERY.search(raw):
                key = DeliveryKey(
                    session=match.group("sid"),
                    epoch=int(match.group("epoch")),
                    turn=int(match.group("turn")),
                    generation=int(match.group("gen")),
                    tool=int(match.group("tool")),
                )
                event = match.group("event")
                kv = dict(KEY_VALUE.findall(raw[match.start() :]))
                delivery_event = DeliveryEvent(
                    when=when,
                    event=event,
                    terminal=kv.get("terminal", ""),
                    terminal_reason=kv.get("terminal_reason", ""),
                    fields=kv,
                )
                delivery = facts.deliveries.setdefault(key, Delivery())
                if delivery_event in delivery.events:
                    facts.duplicates[f"{key.label()} {event}"] += 1
                else:
                    delivery.events.append(delivery_event)
            elif match := PHASE.search(raw):
                if match.group("cause") == "turn_committed":
                    facts.partial_commits.append(
                        (
                            when,
                            "legacy PHASE",
                            {
                                "session": match.group("sid"),
                                "turn_id": match.group("turn"),
                                "generation_id": match.group("gen"),
                            },
                        )
                    )
            elif match := MEDIA_COMMIT.search(raw):
                fields = dict(KEY_VALUE.findall(raw[match.end() :]))
                key, _ = _full_key(fields)
                if key is None:
                    facts.partial_commits.append((when, "media turn committed", fields))
                else:
                    facts.commits.append((when, fields))
            elif match := TURN_COMMITTED.search(raw):
                fields = {
                    "turn_id": match.group("turn"),
                    "generation_id": match.group("gen"),
                }
                if match.group("tool") is not None:
                    fields["tool_epoch"] = match.group("tool")
                facts.partial_commits.append((when, "agent turn_committed", fields))
            elif match := PACING.search(raw):
                facts.pacing.append(
                    (when, dict(KEY_VALUE.findall(raw.split(match.group(0), 1)[1])))
                )
            elif match := CONVERSATION_CLOSE.search(raw):
                facts.closes.append(
                    (
                        when,
                        match.group("sid"),
                        match.group("dev"),
                        int(match.group("epoch")),
                        match.group("reason"),
                    )
                )
    if not facts.deliveries:
        notes.append("no media reply delivery events found (capture may lack --server-logs)")
    return facts, notes


def _receipt_of(payload: dict[str, object]) -> tuple[str, dict[str, object]] | None:
    for name in ("firmware_receipt", "flash_readback_verification"):
        value = payload.get(name)
        if isinstance(value, dict):
            return name, value
    return None


def _receipt_fields(receipt: dict[str, object] | None) -> str:
    if receipt is None:
        return "not recorded"
    fields = (
        receipt.get("release_head"),
        receipt.get("candidate_app_sha256"),
        receipt.get("verified_at"),
    )
    if not any(fields):
        return "no release_head/candidate_app_sha256/verified_at recorded"
    return f"release_head={fields[0]} candidate_app_sha256={fields[1]} verified_at={fields[2]}"


def _receipt_binding(receipt: dict[str, object]) -> str:
    """Evidence class of a capture-recorded receipt, without granting it a binding."""

    if receipt.get("read_from_board_this_run") is not None or receipt.get("path") is not None:
        return "bound_by_capture_tool"
    if receipt.get("sha256") is None:
        # A legacy record with no path/hash/read_from_board_this_run proves nothing.
        return (
            "unbound_legacy_record (no path/sha256/read_from_board_this_run; not binding "
            "evidence and not a live read)"
        )
    return "unbound_legacy_record"


def print_identity(
    run: Path, supplied_path: Path | None, supplied_receipt: dict[str, object] | None
) -> None:
    print("\n== firmware and source identity")
    capture = run / "capture.json"
    recorded: dict[str, object] | None = None
    if not capture.exists():
        print("  capture.json: missing (no capture provenance available)")
    else:
        try:
            loaded = json.loads(capture.read_text())
        except json.JSONDecodeError as error:
            print(f"  capture.json: unreadable ({error})")
            loaded = None
        if isinstance(loaded, dict):
            source = loaded.get("source_revision")
            if isinstance(source, dict):
                print(
                    f"  capture source revision: git_head={source.get('git_head')} "
                    f"dirty={source.get('dirty')} (from capture.json source_revision)"
                )
            elif loaded.get("git_head"):
                print(f"  capture source revision: git_head={loaded.get('git_head')}")
            print(f"  capture started: {loaded.get('started_at_local')} port={loaded.get('port')}")
            found = _receipt_of(loaded)
            if found is None:
                print("  capture firmware receipt: not recorded")
            else:
                source_name, recorded = found
                print(
                    f"  capture firmware receipt (source {source_name}): "
                    f"{_receipt_fields(recorded)}"
                )
                print(f"    capture_receipt_binding={_receipt_binding(recorded)}")
            print(
                "  note: a capture firmware receipt is the operator's flash receipt, not a live "
                "read of the board's on-chip version."
            )
    if supplied_path is not None:
        detail = (
            _receipt_fields(supplied_receipt)
            if supplied_receipt is not None
            else "unreadable or not a JSON object"
        )
        print(
            f"  --firmware-receipt: {supplied_path} sha256={_file_sha256(supplied_path)} {detail}"
        )
    if recorded is not None and supplied_receipt is not None:
        disagreements = [
            f"{name}: capture={recorded.get(name)} supplied={supplied_receipt.get(name)}"
            for name in ("release_head", "candidate_app_sha256", "candidate_elf_sha256")
            if recorded.get(name)
            and supplied_receipt.get(name)
            and recorded[name] != supplied_receipt[name]
        ]
        if disagreements:
            print(
                "  MISMATCH: capture.json records a different flash receipt than --firmware-receipt"
            )
            for line in disagreements:
                print(f"    {line}")
            print(
                "    -> treat this capture's firmware identity as unverified: the recorded receipt "
                "came from the directory the capture tool ran in, not from this session."
            )
        else:
            print("  receipts agree on release_head/candidate app/elf digests")


def print_device(segments: list[DeviceSegment]) -> None:
    print("\n== device timeline (independent local clock; device lines carry no session/epoch id)")
    print("  frame_received = 'First playable downlink frame' from memoria_protocol.cc: the first")
    print("  frame accepted at the device media boundary, a receive-side fact, not audible output.")
    print("  audible=not_measured (needs device playback terminal evidence + operator listening).")
    print("  vad_end->first_received pairs a frame with the most recent unconsumed Device VAD end")
    print("  in the same segment; a frame with no fresh VAD end is reported as unattributable")
    print("  instead of being paired with an older event, and a reboot/reconnect starts a segment.")
    print("  Unconsumed VAD ends (the device never reported a first frame for them) are retained")
    print("  and printed, not silently discarded.")
    print("  Supply lines are device software-queue observations: not DMA underrun, not audible.")
    if not segments:
        print("  none")
        return
    for segment in segments:
        print(f"\n  -- device segment {segment.index}: {segment.opened_by}")
        anchors: list[DeviceEvent] = []
        vad_count = 0
        paired_count = 0
        for event in segment.events:
            suffix = ""
            if event.kind == "vad.end":
                vad_count += 1
                anchors.append(event)
            elif event.kind == "frame_received":
                anchor = anchors.pop() if anchors else None
                if anchor is None:
                    suffix = (
                        "  vad_end->first_received=unattributable "
                        "(no unconsumed Device VAD end in this segment)"
                    )
                else:
                    paired_count += 1
                    gap = _seconds(event.when, anchor.when)
                    if gap < 0:
                        suffix = (
                            f"  vad_end->first_received={gap:.3f}s "
                            "[negative/out-of-order: not a latency]"
                        )
                    else:
                        suffix = f"  vad_end->first_received={gap:.3f}s"
                        if gap > RECEIVE_TARGET_S:
                            suffix += (
                                f" (over {RECEIVE_TARGET_S}s receive-side target; "
                                "still not an audibility claim)"
                            )
            elif event.kind.startswith("supply.") or event.kind.startswith("legacy_"):
                if event.kind.startswith("supply."):
                    evidence = "software_queue_wait_v2"
                else:
                    evidence = "legacy_playback_meter"
                suffix = (
                    f"  producer_format={evidence} evidence_scope=device software queue only; "
                    "not DMA underrun, not audible"
                )
            print(f"  {_stamp(event.when)}  {event.kind:<14} {event.detail}{suffix}")
        print(
            f"  vad_end_count={vad_count} locally_paired={paired_count} "
            f"unconsumed={len(anchors)} (unconsumed VAD ends are retained as unknown, "
            "not silently discarded)"
        )
        for anchor in anchors:
            print(
                f"    unconsumed vad.end at {_stamp(anchor.when)}: "
                "no first_received observed in this device segment"
            )
    summaries: dict[str, int] = {}
    for segment in segments:
        for event in segment.events:
            if event.kind == "supply.supply_summary":
                generation = event.fields.get("generation", "?")
                summaries[generation] = summaries.get(generation, 0) + 1
    if summaries:
        detail = " ".join(f"gen {gen}={count}" for gen, count in sorted(summaries.items()))
        print(
            "\n  supply_summaries_per_generation (each episode is kept in log order; a generation "
            f"flushed more than once keeps every summary): {detail}"
        )


def _commit_timing(key: DeliveryKey, facts: ServerFacts) -> str:
    matches = []
    for when, fields in facts.commits:
        commit_key, _ = _full_key(fields)
        if commit_key == key:
            matches.append(when)
    if not matches:
        return "  commit->first_frame_sent=unknown (no complete matching media turn committed)"
    if len(matches) > 1:
        return (
            f"  commit->first_frame_sent=unknown ({len(matches)} complete commits share this "
            "full key; duplicate commit evidence is ambiguous)"
        )
    first, reason = facts.deliveries[key].stage_time("first_frame_sent")
    if first is None:
        terminal = _terminal_detail(facts.deliveries[key])
        return f"  commit->first_frame_sent=unknown ({reason}; {terminal})"
    gap = _seconds(first, matches[0])
    if gap < 0:
        return f"  commit->first_frame_sent={gap:.3f}s [out of order; not a latency]"
    return f"  commit->first_frame_sent={gap:.3f}s"


def _terminal_detail(delivery: Delivery) -> str:
    terminals = {event.terminal for event in delivery.events if event.terminal}
    reasons = {event.terminal_reason for event in delivery.events if event.terminal_reason}
    detail = []
    if terminals:
        detail.append("terminal=" + ",".join(sorted(terminals)))
    if reasons:
        detail.append("terminal_reason=" + ",".join(sorted(reasons)))
    return " ".join(detail) if detail else "terminal state not recorded"


def print_server(facts: ServerFacts) -> None:
    print(
        "\n== server deliveries (UTC; key = session + media epoch + turn + generation + tool epoch)"
    )
    by_session: dict[str, list[DeliveryKey]] = {}
    for key in facts.deliveries:
        by_session.setdefault(key.session, []).append(key)
    if not by_session:
        print("  none")
    for session in sorted(by_session):
        print(f"\n  -- session {session}")
        for key in sorted(by_session[session]):
            delivery = facts.deliveries[key]
            present = [name for name in STAGE_ORDER if delivery.has(name)]
            missing = [name for name in STAGE_ORDER if not delivery.has(name)]
            non_normal = [event for event in delivery.events if event.event in NON_NORMAL_EVENTS]
            line = f"    {key.label()}: stages={','.join(present) or 'none'}"
            if missing:
                line += f" missing={','.join(missing)}"
                if non_normal:
                    line += " [not a log gap: non-normal event on this delivery]"
            print(line + _commit_timing(key, facts))
            events = "; ".join(
                f"event={event.event} terminal={event.terminal or '?'} "
                f"terminal_reason={event.terminal_reason or '?'}"
                for event in delivery.events
            )
            print(f"      delivery_events: {events}")
            if non_normal:
                print(
                    "      non_normal_event_observed=true "
                    f"({','.join(event.event for event in non_normal)}); this capture observed a "
                    "terminal/non-normal event, so trailing stages are not a missing log"
                )

    print("\n== turn commits (only a complete media turn committed key is used for timing)")
    attributed: dict[DeliveryKey, datetime] = {}
    problems: list[str] = []
    for when, fields in facts.commits:
        key, _ = _full_key(fields)
        if key is None:
            continue
        matches = [key] if key in facts.deliveries else []
        if len(matches) == 1:
            attributed.setdefault(matches[0], when)
        elif not matches:
            problems.append(
                f"{key.label()}: committed {_stamp(when)} but no delivery carries this full key "
                "(delivery not observed in this capture; device arrival is unknown)"
            )
    if not facts.commits and not facts.partial_commits:
        print("  none")
    for key in sorted(attributed):
        detail = _commit_timing(key, facts).strip().removeprefix("commit->")
        print(f"  {key.label()}: committed {_stamp(attributed[key])} -> {detail}")
    for problem in problems:
        print(f"  {problem}")
    if facts.partial_commits:
        print(
            "\n  partial commit evidence (listed only, never used for timing or delivery binding): "
            f"{len(facts.partial_commits)}"
        )
        for when, origin, fields in facts.partial_commits:
            field_text = " ".join(f"{name}={value or '?'}" for name, value in fields.items())
            print(
                f"    {_stamp(when)} source={origin} {field_text} "
                "(session_epoch/tool_epoch/full key absent or legacy format)"
            )


def print_silence_gaps(facts: ServerFacts) -> None:
    print(
        "\n== server gaps between consecutive generations (same complete session/epoch/turn/tool key)"
    )
    groups: dict[tuple[str, int, int, int], list[DeliveryKey]] = {}
    for key in facts.deliveries:
        groups.setdefault((key.session, key.epoch, key.turn, key.tool), []).append(key)
    if not groups:
        print("  none")
    for session, epoch, turn, tool in sorted(groups):
        keys = sorted(groups[(session, epoch, turn, tool)])
        print(
            f"\n  -- session {session} epoch {epoch} turn {turn} tool {tool}: "
            f"{len(keys)} delivery(ies)"
        )
        generations = [key.generation for key in keys]
        if len(set(generations)) != len(generations):
            print("    (multiple deliveries share a generation here; each is listed above)")
        for previous, following in zip(keys, keys[1:], strict=False):
            if previous.generation == following.generation:
                continue
            label = f"    gen {previous.generation}->{following.generation}"
            end, end_reason = facts.deliveries[previous].stage_time("playback_ended")
            start, start_reason = facts.deliveries[following].stage_time("first_frame_sent")
            if end is None or start is None:
                why = end_reason if end is None else start_reason
                terminal = _terminal_detail(
                    facts.deliveries[previous] if end is None else facts.deliveries[following]
                )
                print(f"{label}: unknown ({why}; {terminal})")
                continue
            gap = _seconds(start, end)
            if gap < 0:
                print(
                    f"{label}: out_of_order ({gap:.3f}s; gen {following.generation} started before "
                    f"gen {previous.generation} ended; not a silence gap)"
                )
            else:
                print(f"{label}: {gap:.3f}s")


def _pacing_ratio(fields: dict[str, str]) -> tuple[str, str]:
    """Return the send ratio and the field name it came from."""

    if fields.get("wall_ms") == "0":
        # A single frame names no rate, so any ratio field on that line is not a rate.
        return "unknown", "wall_zero"
    for key in ("send_audio_ratio", "produced_ratio"):
        if key in fields:
            try:
                value = float(fields[key])
            except (TypeError, ValueError):
                return "unknown", key
            if not math.isfinite(value):
                return "unknown", key
            return fields[key], key
    return "unknown", "absent"


def _pacing_binding(fields: dict[str, str], facts: ServerFacts) -> str:
    """Bind a pacing line only through its complete media delivery fence.

    A pacing line carries a device stream_epoch, which lives in a different
    namespace from the delivery media epoch, so the two are never compared.  The
    binding is decided by the complete
    (session, session_epoch, turn, generation, tool_epoch) key alone.
    """

    key, why = _full_key(fields)
    if key is None:
        return f"bound_to_delivery=unknown ({why}; pacing key is incomplete)"
    if key not in facts.deliveries:
        return f"bound_to_delivery=unknown (no delivery with complete key {key.label()})"
    return f"bound_to_delivery={key.label()} (session_epoch compared exactly)"


def _pacing_format(fields: dict[str, str], ratio_key: str) -> str:
    """Name the producer generation of a pacing line without guessing its scope."""

    if fields.get("measurement") == "post_pacer_send":
        complete = all(
            fields.get(name) not in (None, "")
            for name in (
                "session",
                "session_epoch",
                "stream_epoch",
                "turn_id",
                "generation_id",
                "tool_epoch",
            )
        )
        return "post_pacer_send_v2" if complete else "post_pacer_send_partial"
    if ratio_key == "produced_ratio":
        return "legacy_produced_ratio"
    return "unknown_producer_format"


def print_pacing(facts: ServerFacts) -> None:
    print("\n== downlink pacing (post-pacer send measurement, bridge sender side)")
    if not facts.pacing:
        print("  none")
        return
    for when, fields in facts.pacing:
        ratio, ratio_key = _pacing_ratio(fields)
        parts = [f"  {_stamp(when)}", f"session={fields.get('session', '?')}"]
        measurement = fields.get("measurement")
        parts.append(f"measurement={measurement}" if measurement else "measurement=unlabelled")
        for name in (
            "session_epoch",
            "stream_epoch",
            "turn_id",
            "generation_id",
            "tool_epoch",
            "reason",
            "frames",
        ):
            if name in fields:
                parts.append(f"{name}={fields[name]}")
        for name in ("audio_ms", "wall_ms", "max_gap_ms", "queue_high_water"):
            if name in fields:
                parts.append(f"{name}={fields[name]}")
        for name in sorted(fields):
            if name not in KNOWN_PACING_FIELDS and name != ratio_key:
                parts.append(f"{name}={fields[name]}")
        parts.append(f"producer_format={_pacing_format(fields, ratio_key)}")
        if ratio == "unknown":
            detail = (
                "wall_ms=0: a single frame names no rate"
                if fields.get("wall_ms") == "0"
                else "ratio field absent or not a finite number"
            )
            parts.append(f"after_pacer_send_ratio=unknown ({detail})")
        else:
            parts.append(f"after_pacer_send_ratio={ratio}")
        print(" ".join(parts))
        provenance = []
        if ratio_key == "produced_ratio":
            provenance.append("legacy produced_ratio kept as an after-pacer sender field")
        elif ratio_key == "wall_zero":
            provenance.append("wall_ms=0 overrides any ratio field on this line")
        elif ratio_key == "absent":
            provenance.append("no ratio field on this line")
        provenance.append(_pacing_binding(fields, facts))
        print(f"    {'; '.join(provenance)}")
    print("  note: after_pacer_send_ratio is the pacer's post-pacer send measurement: audio")
    print(
        "  milliseconds emitted per wall-clock millisecond after pacing, logged as send_audio_ratio"
    )
    print(
        "  (history: produced_ratio). A value near 1.0 says the sender kept up with real time and"
    )
    print(
        "  had no backlog; wall_ms=0 is reported as unknown because a single frame names no rate."
    )
    print("  stream_epoch is a device stream identifier, not the delivery media epoch, and is not")
    print("  compared with it. The ratio is not production completion, device consumption, DMA")
    print("  supply, codec output, or audible evidence: no audio/DAC/listener inference.")


def stream_bindings(facts: ServerFacts) -> dict[tuple[str, int], set[DeliveryKey]]:
    """Explicit device stream -> media delivery bindings named by complete log keys.

    The close event's epoch is a device stream epoch, which is a different
    namespace from the media session epoch.  It is therefore never treated as one
    and never paired by taking a per-session maximum.  Only a log line that names
    both a device stream_epoch and a complete media delivery key creates a
    binding, and repeated/ambiguous pairs stay unusable.
    """

    bindings: dict[tuple[str, int], set[DeliveryKey]] = {}
    sources = [fields for _, fields in facts.pacing]
    sources += [fields for _, fields in facts.commits]
    for fields in sources:
        stream = fields.get("stream_epoch")
        key, _ = _full_key(fields)
        if key is None or stream is None or not stream.isdigit():
            continue
        bindings.setdefault((key.session, int(stream)), set()).add(key)
    return bindings


def _close_delivery(
    session: str,
    stream_epoch: int,
    facts: ServerFacts,
    bindings: dict[tuple[str, int], set[DeliveryKey]],
) -> tuple[datetime | None, str]:
    candidates = bindings.get((session, stream_epoch), set())
    if not candidates:
        return None, (
            f"no explicit device_stream_epoch={stream_epoch} -> media epoch binding in this "
            "capture (stream and media epochs are separate namespaces; not paired by proximity)"
        )
    epochs = {key.epoch for key in candidates}
    if len(epochs) != 1 or len(candidates) != 1:
        return None, (
            f"device_stream_epoch={stream_epoch} maps to {len(candidates)} delivery key(s) across "
            f"{len(epochs)} media epoch(s); many-to-many binding, not resolvable"
        )
    key = next(iter(candidates))
    played, why = facts.deliveries[key].stage_time("playback_ended")
    if played is None:
        return None, f"bound to {key.label()}, but {why}"
    return played, ""


def print_receipts_and_close(facts: ServerFacts) -> None:
    print("\n== delivery ledger receipts (server side; not operator listening)")
    missing = [
        key for key, delivery in facts.deliveries.items() if not delivery.has("actual_heard")
    ]
    print("  none" if not missing else "  " + ", ".join(key.label() for key in sorted(missing)))
    print("  note: actual_heard=True is the delivery ledger's own receipt. PROJECT_RULES requires")
    print("  device playback terminal evidence plus operator listening before audible is claimed.")

    print(
        "\n== per-session close (projected conversation close; close epoch is a DEVICE STREAM "
        "epoch)"
    )
    print("  device stream epoch and media session epoch are different namespaces; an equal")
    print("  numeric value, time proximity, or a latest/maximum guess never links them. A close")
    print("  is associated with playback only through an explicit, complete and unique log")
    print("  binding of that stream epoch to a full delivery key")
    print("  (session_id, media epoch, turn_id, generation_id, tool_epoch).")
    bindings = stream_bindings(facts)
    if not facts.closes:
        print("  none (no edge conversation close in this capture)")
    for when, session, device, stream_epoch, reason in facts.closes:
        played, why = _close_delivery(session, stream_epoch, facts, bindings)
        if played is None:
            print(
                f"  session {session}: closed {_stamp(when)} reason={reason} "
                f"device={device} device_stream_epoch={stream_epoch}; gap=unknown ({why})"
            )
            continue
        gap = _seconds(when, played)
        detail = (
            f"out_of_order ({gap:.3f}s; close logged before that playback_ended)"
            if gap < 0
            else f"{gap:.3f}s"
        )
        print(
            f"  session {session}: {detail} (reason={reason}, device={device}, "
            f"device_stream_epoch={stream_epoch})"
        )
    closed = {session for _, session, _, _, _ in facts.closes}
    for session in sorted({key.session for key in facts.deliveries} - closed):
        print(f"  session {session}: no conversation close in this capture; farewell gap unknown")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument(
        "--firmware-receipt",
        type=Path,
        default=None,
        help="flash receipt that actually describes the firmware on the board for this capture",
    )
    args = parser.parse_args(argv)
    run: Path = args.run
    if not run.is_dir():
        print(f"error: {run} is not a capture directory")
        return 2

    supplied_receipt: dict[str, object] | None = None
    if args.firmware_receipt is not None and args.firmware_receipt.exists():
        try:
            loaded = json.loads(args.firmware_receipt.read_text())
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            supplied_receipt = loaded

    segments, device_notes = device_segments(run)
    facts, server_notes = server_facts(run)
    for note in (*device_notes, *server_notes):
        print(f"note: {note}")
    for name, count in sorted(facts.duplicates.items()):
        print(f"note: ignored {count} duplicate delivery event line(s) for {name}")

    print_identity(run, args.firmware_receipt, supplied_receipt)
    print_device(segments)
    print_server(facts)
    print_silence_gaps(facts)
    print_pacing(facts)
    print_receipts_and_close(facts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
