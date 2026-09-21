#!/usr/bin/env python3
"""Offline self-check for the machine-only automated audio acceptance artifacts.

Two independent groups of fixtures, none of which touches an audio device, a serial
port or the network:

* audio fixtures -- synthetic WAVs with known energy in known windows, run through
  auto_audio_analyze so the window alignment, the dBFS maths and the verdict table
  have to behave;
* log fixtures -- real-shaped serial and bridge lines from the 2026-09-15 capture, run
  through the driver's LogSession/TurnTracker/cleanup so the acknowledgement-is-not-the-
  answer rule, the session binding and the cleanup contract have to behave.

    python3 selfcheck_analyzer.py
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import signal
import sys
import wave
from array import array
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import auto_audio_analyze as analyzer  # noqa: E402
import auto_audio_session as driver  # noqa: E402

RATE = 16000
ORIGIN = datetime.fromisoformat("2026-09-15T12:00:00.000+08:00")
FLOOR_AMPLITUDE = 3
OUR_SESSION = "684348e9-8f4e-4c6a-b5b9-58e0306d3875"
OTHER_SESSION = "11111111-2222-3333-4444-555555555555"
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  {status} {name} {detail}")
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def stamp(seconds: float) -> str:
    return (ORIGIN + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def utc(seconds: float) -> str:
    naive = ORIGIN.replace(tzinfo=None) + timedelta(seconds=seconds) - timedelta(hours=8)
    return naive.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# --------------------------------------------------------------------------- audio


def write_wav(path: Path, seconds: float, loud: list[tuple[float, float, float]]) -> None:
    """A WAV whose only non-floor samples are the given (start, end, amplitude) spans."""

    total = int(seconds * RATE)
    # A quiet alternating floor keeps the RMS away from an exact zero.
    samples = array("h")
    for index in range(total):
        samples.append(FLOOR_AMPLITUDE if index % 2 == 0 else -FLOOR_AMPLITUDE)
    for start, end, amplitude in loud:
        first, last = int(start * RATE), min(total, int(end * RATE))
        rng = random.Random(1701 + first)
        for index in range(first, last):
            samples[index] = int(amplitude * rng.randint(-32767, 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())


def crop_reference(source: Path, dest: Path, start: float, end: float) -> None:
    samples, rate = analyzer.read_wav_mono(source)
    with wave.open(str(dest), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples[int(start * rate):int(end * rate)].tobytes())


def build_audio_case(root: Path, name: str, *, prompt_amplitude: float, robot_amplitude: float,
                     selftest_amplitude: float | None, recording_seconds: float = 12.0) -> Path:
    case = root / name
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    fence = {"session_id": OUR_SESSION, "session_epoch": 1, "stream_epoch": 7,
             "turn_id": 1, "generation_id": 3, "tool_epoch": 0}
    master = case / "prompt-master.wav"
    write_wav(master, 12, [(2, 4, .25), (10, 11, .25)])
    crop_reference(master, case / "prompt-1.wav", 2, 4)
    crop_reference(master, case / "prompt-2.wav", 10, 11)
    master.unlink()
    events: list[dict[str, object]] = [
        {"kind": "recording_start", "recording": "microphone", "start_local": stamp(0.0),
         "t_local": stamp(0.0)},
        {"kind": "baseline", "recording": "microphone", "name": "baseline",
         "start_local": stamp(0.0), "end_local": stamp(2.0), "t_local": stamp(2.0)},
        {"kind": "prompt", "recording": "microphone", "name": "prompt_1", "turn_index": 1,
         "reference_path": str(case / "prompt-1.wav"),
         "start_local": stamp(2.0), "end_local": stamp(4.0), "t_local": stamp(4.0)},
        {"kind": "prompt", "recording": "microphone", "name": "prompt_2", "turn_index": 2,
         "reference_path": str(case / "prompt-2.wav"),
         "start_local": stamp(10.0), "end_local": stamp(11.0), "t_local": stamp(11.0)},
        {"kind": "device_speaking", "recording": "microphone", "name": "device_speaking",
         "turn_index": 1, "start_local": stamp(6.0), "end_local": stamp(9.0),
         "t_local": stamp(9.0), "content_matched": True, **fence},
    ]
    write_wav(case / "microphone.wav", recording_seconds,
              [(2.0, 4.0, prompt_amplitude), (6.0, 9.0, robot_amplitude), (10, 11, prompt_amplitude)])
    if selftest_amplitude is not None:
        events.extend([
            {"kind": "recording_start", "recording": "selftest", "start_local": stamp(0.0),
             "t_local": stamp(0.0)},
            {"kind": "baseline", "recording": "selftest", "name": "baseline",
             "start_local": stamp(0.0), "end_local": stamp(3.0), "t_local": stamp(3.0)},
            {"kind": "selftest_play", "recording": "selftest", "name": "selftest_play",
             "start_local": stamp(3.0), "end_local": stamp(5.0), "t_local": stamp(5.0)},
        ])
        planned_amplitude = selftest_amplitude or .25
        # A changing envelope also exercises the sustained-level gate.
        selftest_spans = [
            (3.0, 3.10, planned_amplitude * 0.20),
            (3.10, 3.20, planned_amplitude),
            (3.20, 3.30, planned_amplitude * 0.35),
            (3.30, 3.45, planned_amplitude * 0.80),
            (3.45, 3.60, planned_amplitude * 0.15),
            (3.60, 3.80, planned_amplitude * 0.60),
            (3.80, 4.00, planned_amplitude * 0.25),
            (4.00, 4.20, planned_amplitude * 0.90),
            (4.20, 4.50, planned_amplitude * 0.30),
            (4.50, 5.00, planned_amplitude * 0.70),
        ]
        write_wav(master, 6, selftest_spans)
        crop_reference(master, case / "selftest-prompt.wav", 3, 5)
        master.unlink()
        if selftest_amplitude == 0:
            selftest_spans = []
        write_wav(case / "selftest.wav", recording_seconds, selftest_spans)
    (case / "events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8"
    )
    (case / "results.json").write_text(
        json.dumps({"turns": [{"index": 1, "question": "明天南京天气怎么样？",
                               "verdict": "turn_completed", "verdict_log": "turn_completed",
                               "content_window": {"fence": fence}, "completion": fence}]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return case


def analyze(case: Path) -> dict[str, object]:
    raw = case / "results.json"
    before = hashlib.sha256(raw.read_bytes()).hexdigest()
    code = analyzer.main(["--run-dir", str(case)])
    check("raw results bytes remain unchanged", hashlib.sha256(raw.read_bytes()).hexdigest() == before)
    if code != 0:
        raise SystemExit(f"analyzer returned {code} for {case}")
    return json.loads((case / "audio-analysis.json").read_text(encoding="utf-8"))


def audio_checks(root: Path) -> None:
    print("audio fixtures")
    loud = analyze(build_audio_case(root, "robot_audible", prompt_amplitude=0.25,
                                    robot_amplitude=0.05, selftest_amplitude=0.25))
    turn = loud["turns"][0]
    check("audible fenced reply core; log verdict preserved",
          turn["verdict_audio"] == "energy_detected_in_fenced_reply_window"
          and turn["verdict"] == "turn_completed",
          f'verdict={turn["verdict"]} rms={turn["audio"]["answer_window"]["rms_dbfs"]}')

    quiet = analyze(build_audio_case(root, "robot_silent", prompt_amplitude=0.25,
                                     robot_amplitude=0.0, selftest_amplitude=0.25))
    turn = quiet["turns"][0]
    check("silent fenced reply core; log verdict preserved",
          turn["verdict_audio"] == "no_sustained_energy_in_fenced_reply_window"
          and turn["verdict"] == "turn_completed",
          f'verdict={turn["verdict"]} rms={turn["audio"]["answer_window"]["rms_dbfs"]}')

    muted = analyze(build_audio_case(root, "output_muted", prompt_amplitude=0.0,
                                     robot_amplitude=0.05, selftest_amplitude=0.0))
    check("muted output blocks the run", muted["selftest"]["verdict"] == "blocked_output",
          muted["selftest"]["evidence"])

    healthy = analyze(build_audio_case(root, "selftest_ok", prompt_amplitude=0.25,
                                       robot_amplitude=0.05, selftest_amplitude=0.25))
    check("healthy self-test passes", healthy["selftest"]["verdict"] == "selftest_passed",
          healthy["selftest"]["evidence"])

    environmental_noise = build_audio_case(root, "environmental_noise", prompt_amplitude=0.0,
                                           robot_amplitude=0.0, selftest_amplitude=None)
    noise_events = [
        {"kind": "recording_start", "recording": "selftest", "start_local": stamp(0.0),
         "t_local": stamp(0.0)},
        {"kind": "baseline", "recording": "selftest", "name": "baseline",
         "start_local": stamp(0.0), "end_local": stamp(3.0), "t_local": stamp(3.0)},
    ]
    (environmental_noise / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in noise_events), encoding="utf-8"
    )
    write_wav(environmental_noise / "selftest.wav", 12.0, [(3.0, 10.0, 0.25)])
    shutil.copyfile(root / "selftest_ok" / "selftest-prompt.wav",
                    environmental_noise / "selftest-prompt.wav")
    noise = analyze(environmental_noise)
    check("steady environmental noise is not a self-test pass",
          noise["selftest"]["verdict"] == "blocked_output", noise["selftest"]["evidence"])

    # A short file means the capture never really ran, so it must not pass even though its
    # windows would show a healthy loudness margin.
    truncated = analyze(build_audio_case(root, "truncated", prompt_amplitude=0.25,
                                         robot_amplitude=0.05, selftest_amplitude=0.25,
                                         recording_seconds=2.0))
    check("short recording is not a pass", truncated["selftest"]["verdict"] == "blocked_microphone",
          truncated["selftest"]["evidence"])

    # The length gate is numeric: a take measured from samples-live lands above it, and one
    # that falls short is called a microphone problem rather than a pass.
    just_over = analyze(build_audio_case(root, "take_8_6s", prompt_amplitude=0.25,
                                         robot_amplitude=0.05, selftest_amplitude=0.25,
                                         recording_seconds=8.6))
    check("a take above the length gate passes",
          just_over["selftest"]["verdict"] == "selftest_passed",
          just_over["selftest"]["evidence"]["recording_s"])
    just_under = analyze(build_audio_case(root, "take_8_4s", prompt_amplitude=0.25,
                                          robot_amplitude=0.05, selftest_amplitude=0.25,
                                          recording_seconds=8.4))
    check("a take below the length gate is a microphone failure, not a pass",
          just_under["selftest"]["verdict"] == "blocked_microphone",
          just_under["selftest"]["evidence"]["recording_s"])


def negative_audio_checks(root: Path) -> None:
    print("fail-closed audio fixtures")
    case = build_audio_case(root, "negative_audio", prompt_amplitude=.25,
                            robot_amplitude=.05, selftest_amplitude=.25)
    # Speech-like energy alone is not proof that our known prompt was captured.
    write_wav(case / "unrelated.wav", 2, [(0, 2, .25)])
    shutil.copyfile(case / "unrelated.wav", case / "selftest-prompt.wav")
    noise = analyze(case)
    check("fluctuating unrelated sound cannot pass selftest",
          noise["selftest"]["verdict"] == "blocked_reference_match", noise["selftest"])
    (case / "selftest-prompt.wav").unlink()
    missing = analyze(case)
    check("missing reference is a tool blocker not an output diagnosis",
          missing["selftest"]["verdict"] == "blocked_reference_unavailable")

    base = build_audio_case(root, "fence_negative", prompt_amplitude=.25,
                            robot_amplitude=.05, selftest_amplitude=None)
    good = analyze(base)
    measured = good["recordings"]["microphone"]["windows"]
    for name in ("ack", "cross_session", "missing_fence", "missing_stream_epoch", "duplicate_content"):
        raw = json.loads((base / "results.json").read_text())
        windows = json.loads(json.dumps(measured))
        answer = next(w for w in windows if w["name"] == "device_speaking")
        if name == "ack":
            raw["turns"][0]["completion"]["acknowledgement_marker"] = True
        elif name == "cross_session":
            answer["session_id"] = OTHER_SESSION
        elif name == "missing_fence":
            raw["turns"][0].pop("content_window")
        elif name == "missing_stream_epoch":
            answer.pop("stream_epoch")
        else:
            windows.append(dict(answer))
        analyzer.refine_turns(raw, windows, min_active_ms=600)
        check(name + " cannot provide a content window",
              raw["turns"][0]["verdict_audio"] == "unverified_content_window"
              and raw["turns"][0]["verdict"] == "turn_completed")

    original_events = analyzer.load_events(base)
    for name, reason in (
        ("clock_drift", "inconsistent_sample_wall_clock"),
        ("unclosed", "window_not_closed"),
        ("no_post_anchor", "at_least_two_reference_anchors_required"),
        ("outside_recording", "recording_does_not_cover_window"),
        ("computer_overlap", "computer_playback_overlaps_window"),
    ):
        events = json.loads(json.dumps(original_events))
        reply = next(w for w in events if w["kind"] == "device_speaking")
        if name == "clock_drift":
            post = next(w for w in events if w.get("name") == "prompt_2")
            post.update(start_local=stamp(12), end_local=stamp(13))
        elif name == "unclosed":
            reply.pop("end_local")
        elif name == "no_post_anchor":
            events = [w for w in events if w.get("name") != "prompt_2"]
        elif name == "outside_recording":
            reply.update(start_local=stamp(6), end_local=stamp(6.2))
        else:
            events.append({"kind": "prompt", "name": "overlap_without_reference",
                           "start_local": stamp(7), "end_local": stamp(8)})
        recording = analyzer.analyse_recording(base / "microphone.wav", stamp(0), events,
                                                active_dbfs=analyzer.ACTIVE_DBFS)
        result = next(w for w in recording["windows"] if w["name"] == "device_speaking")
        check(name + " refuses a measured reply", result.get("unverified_reason") == reason
              and result["alignment_verified"] is False, result.get("unverified_reason"))

    samples, rate = analyzer.read_wav_mono(base / "microphone.wav")
    with patch.dict(sys.modules, {"numpy": None}):
        result = analyzer.match_reference(samples, rate, base / "prompt-1.wav")
    check("missing numpy fails closed with dependency evidence", not result["matched"]
          and result["reason"] == "reference_decode_unavailable"
          and result["numpy_available"] is False)
    fake_aiff = base / "fake.aiff"
    fake_aiff.write_bytes(b"not an aiff")
    with patch.object(analyzer.subprocess, "check_output", side_effect=FileNotFoundError("ffmpeg")):
        result = analyzer.match_reference(samples, rate, fake_aiff)
    check("missing decoder fails closed", not result["matched"]
          and result["reason"] == "reference_decode_unavailable")
    invalid_wav = base / "invalid.wav"
    invalid_wav.write_bytes(b"")
    result = analyzer.match_reference(samples, rate, invalid_wav)
    check("corrupt reference fails closed", not result["matched"]
          and result["reason"] == "reference_decode_unavailable")
    before = (base / "results.json").read_bytes()
    try:
        analyzer.main(["--run-dir", str(base), "--output", str(base / "results.json")])
    except SystemExit as error:
        check("raw output overwrite rejected", "refusing to overwrite" in str(error))
    else:
        check("raw output overwrite rejected", False)
    check("overwrite refusal preserves raw bytes", (base / "results.json").read_bytes() == before)


# ------------------------------------------------------------------ reference window

CHUNK_SECONDS = 0.9
ROOM_NOISE_AMPLITUDE = 10000.0


def _chunk(seed: int, samples: int, amplitude: float) -> array:
    """One reference chunk of room-ish noise.

    A three-sample moving average keeps the correlation peak a few samples wide, so the
    match behaves like audio that has passed through a room instead of an ideal impulse.
    """

    rng = random.Random(seed)
    raw = [int(amplitude * rng.randint(-32767, 32767)) for _ in range(samples)]
    smoothed = array("h")
    for index in range(samples):
        window = raw[max(0, index - 1):index + 2]
        smoothed.append(int(sum(window) / len(window)))
    return smoothed


def write_samples(path: Path, samples: array) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())


def build_reference_window_case(root: Path, name: str, *, offsets: tuple[float, float],
                                break_third_chunk: bool = False) -> Path:
    """A take whose chunks sit off the rigid lattice, as the 2026-09-20 self-test did.

    The reference holds three distinct chunks; the take holds them at
    `0 / -offsets[0] / -offsets[1]` relative to their lattice positions, with the room's own
    noise on top, so the third chunk's true peak falls outside a ±0.15 s lattice window.
    `break_third_chunk` replaces that chunk with unrelated sound, which must stay unmatched.
    """

    case = root / name
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    samples_per_chunk = int(CHUNK_SECONDS * RATE)
    chunks = [_chunk(910 + index, samples_per_chunk, .25) for index in range(3)]
    reference = array("h")
    for chunk in chunks:
        reference.extend(chunk)
    write_samples(case / "selftest-prompt.wav", reference)

    play_at = 4.0
    total = int(10.0 * RATE)
    take = array("h", [FLOOR_AMPLITUDE if index % 2 == 0 else -FLOOR_AMPLITUDE
                       for index in range(total)])
    rng = random.Random(4242)
    for index in range(total):
        take[index] += rng.randint(-int(ROOM_NOISE_AMPLITUDE), int(ROOM_NOISE_AMPLITUDE))
    starts = [play_at,
              play_at + CHUNK_SECONDS - offsets[0],
              play_at + 2 * CHUNK_SECONDS - offsets[1]]
    for number, start in enumerate(starts):
        chunk = _chunk(555, samples_per_chunk, .25) if break_third_chunk and number == 2 \
            else chunks[number]
        first = int(start * RATE)
        for offset, value in enumerate(chunk):
            index = first + offset
            if 0 <= index < total:
                take[index] = max(-32768, min(32767, take[index] + value))
    write_samples(case / "selftest.wav", take)
    return case


def reference_window_checks(root: Path) -> None:
    """The matcher's readout on the 2026-09-20 geometry.

    This is deliberately NOT a pass/fail fixture for the field case: whether that field
    reading was a real capture distortion or a peak that locked onto neighbouring speech is
    not established, and the lattice bound stays at 0.15 s until it is.  What is asserted
    here is the *diagnostic*: the widest search finds a peak the old window would have
    hidden, and a refusal says whether a chunk was weak or the chunks disagreed.
    """

    print("reference-window fixtures (diagnostics, not a gate)")
    case = build_reference_window_case(root, "reference_window", offsets=(0.148, 0.170))
    samples, rate = analyzer.read_wav_mono(case / "selftest.wav")
    located = analyzer.match_reference(samples, rate, case / "selftest-prompt.wav")
    candidate = located.get("candidate") if isinstance(located.get("candidate"), dict) else {}
    check("a refused prompt still reports its strongest candidate",
          bool(candidate) and located["matched"] is False, located.get("reason"))
    peaks = candidate.get("chunk_global_peak") or []
    check("the wider search finds a third-chunk peak that the old ±0.15 s window hid",
          len(peaks) == 3 and abs(float(peaks[2]["offset_s"])) > 0.15
          and float(peaks[2]["ncc"]) >= analyzer.REFERENCE_MIN_NCC, peaks)
    check("the chosen peak is that found peak, so 'weak chunk' and 'clipped search' differ",
          len(candidate.get("chunk_ncc") or []) == 3
          and min(candidate["chunk_ncc"]) >= analyzer.REFERENCE_MIN_NCC,
          candidate.get("chunk_ncc"))
    check("the refusal names the lattice spread and the residuals behind it",
          candidate.get("rejection") == "lattice_spread_exceeds_bound"
          and float(candidate.get("spread_s", 0.0)) > analyzer.REFERENCE_MAX_SKEW_S
          and len(candidate.get("chunk_residual_s") or []) == 3, candidate)
    check("the lattice bound was not loosened to make this case pass",
          analyzer.REFERENCE_MAX_SKEW_S == 0.15
          and analyzer.REFERENCE_CHUNK_SEARCH_S == 0.60,
          (analyzer.REFERENCE_MAX_SKEW_S, analyzer.REFERENCE_CHUNK_SEARCH_S))
    readout = driver.reference_readout(located)
    check("the self-test blocker carries that readout to the operator",
          "rejection=lattice_spread_exceeds_bound" in readout and "chunk_ncc=" in readout,
          readout)

    broken = build_reference_window_case(root, "reference_window_broken",
                                         offsets=(0.148, 0.170), break_third_chunk=True)
    samples, rate = analyzer.read_wav_mono(broken / "selftest.wav")
    missed = analyzer.match_reference(samples, rate, broken / "selftest-prompt.wav")
    weak = missed.get("candidate") if isinstance(missed.get("candidate"), dict) else {}
    check("a take missing one chunk is refused as a weak chunk, not as a disagreement",
          missed["matched"] is False and weak.get("rejection") == "chunk_ncc_below_min"
          and min(weak.get("chunk_ncc") or [1.0]) < analyzer.REFERENCE_MIN_NCC, weak)

    # Two occurrences stay two: uniqueness is what keeps a match off the wrong playback.
    twice = build_reference_window_case(root, "reference_window_twice", offsets=(0.0, 0.0))
    samples, rate = analyzer.read_wav_mono(twice / "selftest.wav")
    both = analyzer.match_reference(samples + samples, rate, twice / "selftest-prompt.wav")
    check("two occurrences are reported as two, never silently resolved",
          len(both["matches"]) >= 2, len(both["matches"]))


# ----------------------------------------------------------------------------- logs


def serial_line(seconds: float, source: str, destination: str) -> tuple[float, str, str]:
    stamp_text = stamp(seconds)
    return (seconds, "serial",
            f"[{stamp_text}] I (1000) StateMachine: State: {source} -> {destination}")


def bridge_line(seconds: float, body: str) -> tuple[float, str, str]:
    return (seconds, "bridge", f"{utc(seconds)} INFO:services.agent.src: {body}")


PHASE_TAG = "services.agent.src.duplex_runtime:interaction_phase"
DELIVERY_TAG = "services.agent.src.voice_core.media_session_output_dispatch:media reply delivery"
COMMIT_TAG = "services.agent.src.voice_core.media_session_commit:media turn committed"


def scoped_commit(seconds: float, turn: int, generation: int, *, stream_epoch: int = 1955,
                  session: str = OUR_SESSION, tool_epoch: int = 0) -> tuple[float, str, str]:
    return bridge_line(seconds, f"{COMMIT_TAG} session={session} session_epoch=1 "
                                f"stream_epoch={stream_epoch} turn_id={turn} "
                                f"generation_id={generation} tool_epoch={tool_epoch}")


def phase_line(seconds: float, source: str, destination: str, cause: str, *,
               session: str = OUR_SESSION, turn: int = 3,
               generation: int = 4) -> tuple[float, str, str]:
    return bridge_line(seconds, f"{PHASE_TAG} from={source} to={destination} cause={cause} "
                                f"session_id={session} turn_id={turn} generation_id={generation}")


def delivery_line(seconds: float, body: str, *, session: str = OUR_SESSION, epoch: int = 1,
                  turn: int = 3, generation: int = 4, tool: int = 0) -> tuple[float, str, str]:
    return bridge_line(seconds, f"{DELIVERY_TAG} session={session} "
                                f"delivery_id={session}/epoch-{epoch}/turn-{turn}/"
                                f"generation-{generation}/tool-{tool} {body}")


def turn_lines() -> list[tuple[float, str, str]]:
    """Turn 3 of the 2026-09-15 capture: generation 4 is the acknowledgement, 5 is the answer."""

    return [
        serial_line(16.031, "listening", "speaking"),
        # This agent-side line carries no session, so it may not be attributed to anyone.
        bridge_line(17.483, "services.agent.src.agent:turn_committed turn_id=3 "
                            "generation_id=4 tool_epoch=0 text_len=42"),
        scoped_commit(17.500, 3, 4),
        phase_line(18.009, "thinking_silent", "speaking", "assistant_speaking", generation=4),
        delivery_line(18.010, "event=first_frame_sent terminal= first_frame_sent=True",
                      generation=4),
        serial_line(20.770, "speaking", "listening"),
        delivery_line(20.772, "event=playback_ended terminal=playback_ended "
                              "terminal_reason=playback_completed", generation=4),
        phase_line(20.781, "speaking", "tool_waiting", "media_playback_ack", generation=4),
    ]


def continuation_lines() -> list[tuple[float, str, str]]:
    return [
        # A later commit for another turn must not re-point the fixed fence.
        scoped_commit(20.900, 7, 9),
        # A delivery frame outside the fence (wrong tool epoch) must be refused.
        delivery_line(20.950, "event=first_frame_sent", generation=5, tool=1),
        # Another device's conversation, while we are already bound.
        phase_line(21.000, "speaking", "listening", "media_playback_ack",
                   session=OTHER_SESSION, turn=9, generation=9),
        delivery_line(21.050, "event=playback_ended terminal_reason=playback_completed",
                      session=OTHER_SESSION, epoch=7, turn=9, generation=9),
        # A reconnect that still carries the previous stream epoch.
        scoped_commit(21.100, 3, 5, stream_epoch=1954),
        bridge_line(25.414, "services.agent.src.providers.open_meteo_weather:open meteo weather "
                            "lookup completed model=open-meteo start_offset=0 days=3 elapsed_ms=7930"),
        phase_line(25.798, "thinking_silent", "speaking", "assistant_speaking", generation=5),
        delivery_line(25.800, "event=first_frame_sent terminal= first_frame_sent=True",
                      generation=5),
        serial_line(25.820, "listening", "speaking"),
        serial_line(31.240, "speaking", "listening"),
        delivery_line(31.242, "event=playback_ended terminal=playback_ended "
                              "terminal_reason=playback_completed", generation=5),
        phase_line(31.248, "speaking", "listening", "media_playback_ack", generation=5),
    ]


def feed(session: driver.LogSession, lines: list[tuple[float, str, str]]) -> None:
    for seconds, source, raw in sorted(lines, key=lambda item: item[0]):
        session.feed(source, raw, arrived=seconds)


def resumed_tracker(session: driver.LogSession) -> driver.TurnTracker:
    tracker = driver.TurnTracker()
    for line in session.lines:
        tracker.observe(line)
    for line in session.lookups():
        tracker.observe(line)
    return tracker


def log_checks() -> None:
    print("log fixtures")
    session = driver.LogSession({})
    # Another device's conversation arrives BEFORE we know our own identity.
    feed(session, [
        phase_line(10.0, "speaking", "listening", "media_playback_ack",
                   session=OTHER_SESSION, turn=9, generation=9),
        delivery_line(10.1, "event=playback_ended terminal_reason=playback_completed",
                      session=OTHER_SESSION, epoch=7, turn=9, generation=9),
    ])
    check("nothing is bound from a log line", not session.bound, session.binding)
    session.bind(OUR_SESSION, stream_epoch=1955, source="fixture")
    check("pre-binding foreign conversation is rejected, not adopted",
          len(session.foreign) == 2 and not session.lines,
          f"foreign={len(session.foreign)} admitted={len(session.lines)}")

    feed(session, turn_lines())
    tracker = resumed_tracker(session)
    check("acknowledgement is not the answer", tracker.completion() is None,
          f"generations={sorted(tracker.generations)} listening_gen={tracker.listening_generation}")
    check("acknowledgement was seen and completed",
          (tracker.generations.get(4) or {}).get("playback_reason") == "playback_completed",
          tracker.generations.get(4))
    check("a session-less control marker is not attributed",
          any(item["kind"] == "turn_committed" for item in session.unattributed),
          session.unattributed)

    feed(session, continuation_lines())
    check("a stale stream epoch is refused",
          any(item["reason"] == "stream_epoch" for item in session.foreign), session.foreign)
    check("the foreign conversation stayed out",
          len([item for item in session.foreign if item["reason"] == "session_id"]) == 4
          and all(OTHER_SESSION not in line.raw for line in session.lines),
          f"foreign={len(session.foreign)}")

    tracker2 = resumed_tracker(session)
    completion = tracker2.completion()
    check("content generation completes the turn",
          bool(completion) and completion["generation_id"] == 5, completion)
    check("both generations are recorded",
          bool(completion) and completion["generations_seen"] == [4, 5]
          and completion["acknowledgement_generations"] == [4],
          completion and completion["generations_seen"])
    check("content generation started after the (unscoped) weather lookup",
          bool(completion) and completion["content_after_weather"] is True
          and completion["weather_correlation"] == "unscoped_provider_log", completion)
    check("session-less provider lines stay correlation-only",
          len(session.lookups()) == 1 and session.lookups()[0].session_id is None,
          len(session.lookups()))
    check("the fence stayed fixed on the first commit",
          tracker2.turn_id == 3 and tracker2.session_epoch == 1 and tracker2.tool_epoch == 0
          and any(item["turn_id"] == 7 for item in tracker2.ignored_commits),
          f"turn={tracker2.turn_id} ignored={tracker2.ignored_commits}")
    check("a delivery frame outside the fence is refused",
          any(item["reason"] == "tool_epoch" for item in tracker2.fence_rejected),
          tracker2.fence_rejected)


def welcome_checks() -> None:
    print("wake / welcome fixtures")
    session = driver.LogSession({})
    feed(session, [serial_line(0.0, "idle", "listening"),
                   phase_line(1.0, "speaking", "listening", "media_playback_ack",
                              session=OTHER_SESSION, turn=8, generation=8)])
    session.bind(OUR_SESSION, source="fixture")
    check("a foreign greeting does not count as ours",
          driver._welcome_done(session, 0) is None, "waiting before our greeting")
    feed(session, [phase_line(2.0, "speaking", "listening", "media_playback_ack",
                              session=OUR_SESSION, turn=1, generation=1)])
    check("our greeting completes the welcome",
          driver._welcome_done(session, 0) is not None, "our listening phase")

    # A welcome has to be the greeting of *this* wake, never an earlier transition.
    stale = driver.LogSession({})
    feed(stale, [serial_line(0.0, "idle", "listening"),
                 phase_line(1.0, "speaking", "listening", "media_playback_ack",
                            turn=1, generation=1)])
    stale.bind(OUR_SESSION, source="fixture")
    check("welcome does not reuse an earlier transition",
          driver._welcome_done(stale, stale.mark()) is None, "mark taken after the transition")


REAL_REPLAY = HERE / "fixtures" / "real-turns-20260915.log"


def load_real_rows() -> list[tuple[str, str]]:
    """Verbatim lines from a real passing run, in chronological order."""

    if not REAL_REPLAY.exists():
        return []
    rows: list[tuple[str, str]] = []
    for line in REAL_REPLAY.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        source, _, raw = line.partition("\t")
        rows.append((source, raw))
    return rows


def _replay(rows: list[tuple[str, str]]) -> driver.LogSession:
    session = driver.LogSession({})
    session.bind(OUR_SESSION, stream_epoch=1955, source="fixture")
    for source, raw in rows:
        session.feed(source, raw)
    return session


def _replay_tracker(session: driver.LogSession) -> driver.TurnTracker:
    tracker = driver.TurnTracker()
    for line in session.lines:
        tracker.observe(line)
    for line in session.lookups():
        tracker.observe(line)
    return tracker


def real_replay_checks() -> None:
    """Replay the real, passing two-weather-turn window from an acceptance capture.

    The real `interaction_phase` lines carry session/turn/generation but no session or
    tool epoch, so this is the fixture that proves the auxiliary match does not fence the
    phase lines out while completion still demands a full-fence generation.
    """

    print("real bridge.log replay (two weather turns)")
    rows = load_real_rows()
    check("the real replay fixture is present", len(rows) == 37, len(rows))
    if not rows:
        return

    def index_of(*needles: str) -> int:
        for position, (_, raw) in enumerate(rows):
            if all(needle in raw for needle in needles):
                return position
        return -1

    turn2_commit = index_of("media turn committed", "turn_id=2 generation_id=2 tool_epoch=0")
    turn2_ack_end = index_of("to=tool_waiting cause=media_playback_ack", "turn_id=2 generation_id=2")
    turn2_end = index_of("to=listening cause=media_playback_ack", "turn_id=2 generation_id=3")
    turn3_commit = index_of("media turn committed", "turn_id=3 generation_id=4 tool_epoch=0")
    turn3_ack_end = index_of("to=tool_waiting cause=media_playback_ack", "turn_id=3 generation_id=4")
    turn3_end = index_of("to=listening cause=media_playback_ack", "turn_id=3 generation_id=5")
    check("both weather turns are inside the fixture",
          min(turn2_commit, turn2_ack_end, turn2_end, turn3_commit, turn3_ack_end, turn3_end) >= 0,
          [turn2_commit, turn2_ack_end, turn2_end, turn3_commit, turn3_ack_end, turn3_end])

    full = _replay(rows)
    check("every real line belongs to this capture",
          not full.foreign and not full.unattributed,
          f"foreign={len(full.foreign)} unattributed={len(full.unattributed)}")
    check("the real weather lines stay correlation-only",
          len(full.lookups()) == 2 and all(line.session_id is None for line in full.lookups()),
          len(full.lookups()))

    # Each turn is tracked over its own window, exactly as the driver does after a prompt.
    ack2 = _replay_tracker(_replay(rows[turn2_commit:turn2_ack_end + 1]))
    tracker2 = _replay_tracker(_replay(rows[turn2_commit:turn2_end + 1]))
    ack3 = _replay_tracker(_replay(rows[turn3_commit:turn3_ack_end + 1]))
    tracker3 = _replay_tracker(_replay(rows[turn3_commit:turn3_end + 1]))
    check("the real phase lines are matched, not fenced out",
          not tracker2.fence_rejected and not tracker3.fence_rejected
          and not tracker2.incomplete_markers and not tracker3.incomplete_markers,
          f"fence_rejected={tracker2.fence_rejected + tracker3.fence_rejected} "
          f"incomplete={tracker2.incomplete_markers + tracker3.incomplete_markers}")
    check("the real ACK (turn 2 gen 2) does not complete the turn",
          ack2.completion() is None
          and (ack2.generations.get(2) or {}).get("playback_reason") == "playback_completed",
          f"completion={ack2.completion()} gens={sorted(ack2.generations)}")
    done2 = tracker2.completion()
    check("turn 2 completes on the content generation 3",
          bool(done2) and done2["generation_id"] == 3
          and done2["acknowledgement_generations"] == [2],
          done2)
    check("turn 2 content started after the unscoped weather lookup",
          bool(done2) and done2["content_after_weather"] is True
          and done2["weather_correlation"] == "unscoped_provider_log", done2)

    check("the real ACK (turn 3 gen 4) does not complete the turn",
          ack3.completion() is None
          and (ack3.generations.get(4) or {}).get("playback_reason") == "playback_completed",
          f"completion={ack3.completion()} gens={sorted(ack3.generations)}")
    done3 = tracker3.completion()
    check("turn 3 completes on the content generation 5",
          bool(done3) and done3["generation_id"] == 5
          and done3["acknowledgement_generations"] == [4]
          and done3["content_after_weather"] is True, done3)
    check("both completed generations carry their full fence",
          bool(done2) and done2["session_epoch"] == 1 and done2["tool_epoch"] == 0
          and bool(done3) and done3["session_epoch"] == 1 and done3["tool_epoch"] == 0,
          [done2 and done2["tool_epoch"], done3 and done3["tool_epoch"]])


def probe_checks() -> None:
    print("deadline / probe fixtures")
    # A probe whose budget is gone must not start: a bogus binary would raise if executed.
    check("an expired probe is never started",
          driver.make_probe(driver.Deadline(0.0))(["definitely-not-a-real-binary-xyz"]) == (125, ""),
          "exit 125, no output")
    code, text = driver.make_probe(driver.Deadline(5.0))(["/bin/echo", "ok"])
    check("a live probe still runs", code == 0 and text.strip() == "ok", (code, text.strip()))
    # The budget is read per call, not frozen when the probe was created.
    shrinking = driver.Deadline(5.0)
    probe = driver.make_probe(shrinking)
    first = probe(["/bin/echo", "a"])[0]
    shrinking.end = 0.0
    check("the probe reads the remaining budget at each call",
          first == 0 and probe(["/bin/echo", "b"]) == (125, ""), (first, "then 125"))


def exit_code_checks() -> None:
    print("exit-code fixtures")
    check("a clean run exits zero", driver.abort_reason({}) is None, None)
    check("an incomplete turn exits non-zero",
          driver.abort_reason({"aborted_after_turn": 2, "abort_reason": "turn_incomplete"})
          == "turn_incomplete", None)
    check("a run that never woke exits non-zero",
          driver.abort_reason({"aborted_before_turn": 1}) == "wake_or_welcome_not_reached", None)
    check("a budget abort exits non-zero",
          driver.abort_reason({"aborted_by": "no budget left"}) == "no budget left", None)


def rebind_checks() -> None:
    print("re-wake / fence fixtures")
    session = driver.LogSession({})
    session.bind(OUR_SESSION, stream_epoch=1955, source="fixture")
    check("bind sets the stream epoch", session.stream_epoch == 1955, session.stream_epoch)
    session.unbind("re-wake")
    check("unbind drops the identity and the epoch",
          session.binding is None and session.stream_epoch is None,
          (session.binding, session.stream_epoch))
    # The greeting of a re-wake arrives before the fresh snapshot: held, then admitted on
    # bind.  It must not be judged against the previous session.
    feed(session, [serial_line(0.0, "idle", "listening"),
                   phase_line(1.0, "speaking", "listening", "media_playback_ack",
                              session=OUR_SESSION, turn=1, generation=1)])
    session.bind(OUR_SESSION, stream_epoch=None, source="fixture")
    check("bind overwrites the epoch even with None", session.stream_epoch is None,
          session.stream_epoch)
    check("the new greeting was admitted, not called foreign",
          not session.foreign and driver._welcome_done(session, 0) is not None,
          f"foreign={session.foreign}")

    tracker = driver.TurnTracker()
    tracker.observe(driver.LogLine("bridge", stamp(0.9), "interaction_phase",
                                   session_id=OUR_SESSION, turn_id=3, generation_id=5,
                                   fields={"src": "speaking", "dst": "speaking",
                                           "cause": "assistant_speaking"}))
    check("an auxiliary phase line is not incomplete evidence", not tracker.incomplete_markers,
          tracker.incomplete_markers)
    tracker.observe(driver.LogLine("bridge", stamp(0.95), "turn_committed",
                                   session_id=OUR_SESSION, session_epoch=1, turn_id=3,
                                   generation_id=4, tool_epoch=0))
    tracker.observe(driver.LogLine("bridge", stamp(1.0), "first_frame_sent",
                                   session_id=OUR_SESSION, session_epoch=1, turn_id=3,
                                   generation_id=5, tool_epoch=0))
    check("a complete delivery frame is accepted",
          (tracker.generations.get(5) or {}).get("first_frame") is True,
          tracker.generations.get(5))
    tracker.observe(driver.LogLine("bridge", stamp(1.1), "playback_ended",
                                   session_id=OUR_SESSION, session_epoch=None, turn_id=3,
                                   generation_id=5, tool_epoch=0,
                                   fields={"reason": "playback_completed"}))
    check("a delivery frame missing its epoch is refused",
          any(item["missing"] == ["session_epoch"] for item in tracker.incomplete_markers)
          and (tracker.generations.get(5) or {}).get("playback_reason") is None,
          tracker.incomplete_markers)


class FakeProc:
    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.signals: list[int] = []
        self.killed = False

    def poll(self):
        return None if self.running else 0

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)
        self.running = False

    def wait(self, timeout: float | None = None) -> int:
        self.running = False
        return 0

    def kill(self) -> None:
        self.killed = True
        self.running = False


class FakeRecorder:
    def __init__(self, name: str) -> None:
        self.proc: FakeProc | None = FakeProc()
        self.out_path = Path(name)
        self.ended_local = ""
        self.returncode = 0
        self.stop_calls = 0

    def stop(self, grace: float = 10.0) -> None:
        self.stop_calls += 1
        self.ended_local = stamp(99.0)


class ExplodingRecorder(FakeRecorder):
    def stop(self, grace: float = 10.0) -> None:
        raise RuntimeError("this recorder refuses to stop")


class FakeClock:
    """A monotonic clock the fixtures advance, so bounded waits run without real waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class LateBytesRecorder:
    """A recorder whose bytes only appear `first_bytes_at` seconds after it started."""

    def __init__(self, first_bytes_at: float) -> None:
        self.proc = FakeProc()
        self.first_bytes_at = first_bytes_at
        self.started = driver.time.monotonic()

    def bytes_written(self) -> int:
        elapsed = driver.time.monotonic() - self.started
        return driver.RECORDER_MIN_BYTES + 1 if elapsed >= self.first_bytes_at else 0

    def live(self) -> bool:
        return self.bytes_written() > driver.RECORDER_MIN_BYTES


def live_gate_checks(root: Path) -> None:
    print("live gate fixtures")
    case = root / "live_gate"
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    recorder = driver.Recorder(case / "recorder.log", 0, case / "recorder.wav", 30)
    # A bare 44-byte header is exactly what the WAV shows while ffmpeg still buffers.
    recorder.out_path.write_bytes(b"\0" * 44)
    recorder.log_path.write_text(
        "frame=0\nsize=0KiB\ntotal_size=0\nout_time_us=0\nprogress=continue\n"
        "frame=12\ntotal_size=13420\nout_time_us=509813\nprogress=continue\n",
        encoding="utf-8",
    )
    check("ffmpeg's own progress counter counts as live while the WAV is still a header",
          recorder.live() and recorder.file_bytes() < driver.RECORDER_MIN_BYTES,
          (recorder.file_bytes(), recorder.progress()))
    recorder.log_path.write_text("frame=0\ntotal_size=0\nout_time_us=0\n",
                                encoding="utf-8")
    check("no bytes and no progressed counter is not live", recorder.live() is False)

    clock = FakeClock()
    with patch.object(driver.time, "monotonic", clock.monotonic), \
            patch.object(driver.time, "sleep", clock.sleep):
        deadline = driver.Deadline(300.0)
        check("a flush that lands at 10.4 s is accepted inside the shipped bound",
              driver.wait_recorder_live(LateBytesRecorder(10.4),
                                        driver.FFMPEG_START_TIMEOUT_S, deadline) is True,
              driver.FFMPEG_START_TIMEOUT_S)
        check("the old 10 s default raced that same flush",
              driver.wait_recorder_live(LateBytesRecorder(10.4), 10.0, deadline) is False)
        check("a recorder that never writes is refused",
              driver.wait_recorder_live(LateBytesRecorder(1e9),
                                        driver.FFMPEG_START_TIMEOUT_S, deadline) is False)
        exited = LateBytesRecorder(0.0)
        exited.proc.running = False
        before = clock.now
        check("an ffmpeg that already exited is refused without waiting",
              driver.wait_recorder_live(exited, driver.FFMPEG_START_TIMEOUT_S, deadline) is False
              and clock.now - before < 1.0, clock.now - before)


def follow_up_checks() -> None:
    print("follow-up delay fixtures")
    check("the shipped default is the acceptance grid",
          driver.parse_follow_up_delays(driver.DEFAULT_FOLLOW_UP_DELAYS) == [3.0, 5.0, 8.0],
          driver.DEFAULT_FOLLOW_UP_DELAYS)
    for bad in ("", "abc", "-1", "61", " , "):
        try:
            driver.parse_follow_up_delays(bad)
            refused = False
        except ValueError:
            refused = True
        check(f"delay {bad!r} is refused before anything is played", refused)
    check("the first question has no reply to count from",
          driver.planned_follow_up_delay([3.0, 5.0, 8.0], 1) is None)
    check("later questions walk the grid and repeat its last value",
          [driver.planned_follow_up_delay([3.0, 5.0, 8.0], position) for position in (2, 3, 4)]
          == [3.0, 5.0, 8.0])
    partial = driver.follow_up_grid([3.0, 5.0, 8.0], [
        {"follow_up": {"planned_s": 3.0, "actual_s": 3.1, "late_by_s": 0.1}},
        {"follow_up": {"planned_s": 5.0, "actual_s": None}},
    ])
    check("a delay cell counts only when a turn really waited and played",
          partial["covered_delays_s"] == [3.0] and partial["uncovered_delays_s"] == [5.0, 8.0],
          partial)
    late_cell = driver.follow_up_grid([3.0, 5.0, 8.0], [
        {"follow_up": {"planned_s": 3.0, "actual_s": 12.4, "late_by_s": 9.4}},
    ])
    check("a delay a wake consumed is reported late, never as the planned cell",
          late_cell["covered_delays_s"] == [] and late_cell["late_delays_s"] == [3.0]
          and late_cell["uncovered_delays_s"] == [3.0, 5.0, 8.0]
          and late_cell["cells"][0]["disposition"] == "late", late_cell)
    early_cell = driver.follow_up_grid([3.0, 5.0, 8.0], [
        {"follow_up": {"planned_s": 3.0, "actual_s": 2.5, "late_by_s": -0.5}},
    ])
    check("an early play is a clock/anchor anomaly, never counted as covered",
          early_cell["covered_delays_s"] == [] and early_cell["early_delays_s"] == [3.0]
          and early_cell["uncovered_delays_s"] == [3.0, 5.0, 8.0]
          and early_cell["cells"][0]["disposition"] == "early", early_cell)
    check("a follow-up only counts as a continuation if the gate did not wake the device",
          driver.follow_up_gate_verdict(1, {}) is None
          and driver.follow_up_gate_verdict(2, {"already_listening": True}) is None
          and driver.follow_up_gate_verdict(2, {"wake_detected": True})
          == "follow_up_requires_continuation"
          and driver.follow_up_gate_verdict(3, {}) == "follow_up_requires_continuation")
    default_run = driver.follow_up_grid([3.0, 5.0, 8.0], [
        {"follow_up": {"planned_s": planned,
                       "actual_s": planned + 0.1}}
        for planned in (driver.planned_follow_up_delay([3.0, 5.0, 8.0], 2),
                        driver.planned_follow_up_delay([3.0, 5.0, 8.0], 3))
    ])
    check("the default three-question run covers only two cells and reports the gap",
          default_run["covered_delays_s"] == [3.0, 5.0]
          and default_run["uncovered_delays_s"] == [8.0], default_run)
    whole = driver.follow_up_grid([3.0, 5.0, 8.0], [
        {"follow_up": {"planned_s": value, "actual_s": value}} for value in (3.0, 5.0, 8.0)])
    check("a four-question run covers the whole 3/5/8 grid",
          whole["covered_delays_s"] == [3.0, 5.0, 8.0]
          and whole["uncovered_delays_s"] == [], whole)
    anchor = driver.follow_up_anchor_local({"completion": {"playback_ended_t": stamp(20.0)}})
    check("the anchor is the previous turn's playback end", anchor == stamp(20.0), anchor)
    check("a turn without a completed playback exposes no anchor",
          driver.follow_up_anchor_local({"completion": None}) is None
          and driver.follow_up_anchor_local(None) is None)
    check("the measured delay is the distance from that anchor",
          driver.seconds_between(stamp(20.0), stamp(25.2)) == 5.2
          and driver.seconds_between(None, stamp(25.2)) is None)
    started = datetime.fromisoformat(stamp(20.0))
    with patch.object(driver.time, "sleep") as sleeper:
        note = driver.wait_for_follow_up(stamp(20.0), 5.0, driver.Deadline(600.0), now=started)
    check("a +5 s delay sleeps the remaining 5 s and says so",
          sleeper.call_args.args[0] == 5.0 and note["reason"] == "waited"
          and note["cut_by_deadline"] is False, note)
    with patch.object(driver.time, "sleep") as sleeper:
        late = driver.wait_for_follow_up(stamp(20.0), 5.0, driver.Deadline(600.0),
                                         now=datetime.fromisoformat(stamp(30.0)))
    check("a moment that has already passed does not sleep",
          sleeper.call_count == 0 and late["reason"] == "already_past_target", late)
    with patch.object(driver.time, "sleep") as sleeper:
        cut = driver.wait_for_follow_up(stamp(20.0), 8.0, driver.Deadline(0.0), now=started)
    check("a delay the budget cannot cover is reported as cut, not served short",
          cut["cut_by_deadline"] is True and sleeper.call_count == 0, cut)
    with patch.object(driver.time, "sleep") as sleeper:
        broken = driver.wait_for_follow_up("not-a-stamp", 3.0, driver.Deadline(600.0), now=started)
    check("an unparsable anchor refuses the delay instead of guessing one",
          broken["reason"] == "anchor_unparsable" and sleeper.call_count == 0, broken)


def barge_source_checks() -> None:
    print("barge-source fixtures")
    configured = driver.barge_source_evidence({
        "allowed_barge_in": ["button", "keyword"], "allowed_barge_in_origin": "configured_row",
        "settings_version": 7,
    })
    check("a configured settings row is reported as the control plane's configured value",
          configured["configured_allowed_barge_in"] == ["button", "keyword"]
          and configured["configured_allowed_barge_in_source"]
          == "control_api_device_settings_row", configured)
    defaults = driver.barge_source_evidence({
        "allowed_barge_in": ["button", "keyword"], "allowed_barge_in_origin": "defaults",
        "settings_version": 0,
    })
    check("the authority's built-in defaults are never reported as a permission",
          defaults["configured_allowed_barge_in"] is None
          and defaults["configured_allowed_barge_in_source"] == "unknown"
          and defaults["configured_allowed_barge_in_defaults_unverified"]
          == ["button", "keyword"], defaults)
    unknown = driver.barge_source_evidence({})
    check("a snapshot without settings stays unknown instead of assuming a source",
          unknown["configured_allowed_barge_in"] is None
          and unknown["configured_allowed_barge_in_source"] == "unknown", unknown)
    empty = driver.barge_source_evidence({"allowed_barge_in": [],
                                          "allowed_barge_in_origin": "configured_row"})
    check("an empty configured list is refused as unknown too",
          empty["configured_allowed_barge_in"] is None, empty)
    failed = driver.barge_source_evidence({"barge_settings_error": "RuntimeError: no db"})
    check("a failed settings read keeps its reason",
          failed["barge_settings_error"] == "RuntimeError: no db", failed)


def cleanup_checks(root: Path) -> None:
    print("cleanup fixture")
    # A stop signal must unwind through the finally block, not kill the process outright.
    driver.install_stop_handlers()
    unwound = False
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except driver.Stopped:
        unwound = True
    finally:
        driver.ignore_stop_handlers()
    check("a stop signal unwinds so cleanup can run", unwound, "SIGTERM")

    case = root / "cleanup"
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    events = driver.EventLog(case / "events.jsonl")
    results: dict[str, object] = {}
    capture = FakeProc()
    recorders = [FakeRecorder("selftest.wav"), FakeRecorder("microphone.wav")]
    driver.cleanup(capture, recorders, events, results, case, budget_s=7.5)
    payload = json.loads((case / "results.json").read_text(encoding="utf-8"))
    emitted = [json.loads(line)["recording"]
               for line in (case / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
    check("cleanup terminated the capture", capture.signals == [driver.signal.SIGTERM],
          capture.signals)
    check("cleanup stopped every recorder including the self-test one",
          all(recorder.stop_calls == 1 for recorder in recorders),
          [recorder.stop_calls for recorder in recorders])
    check("cleanup reported both recorders",
          payload["cleanup"]["recorders_stopped"] == ["selftest.wav", "microphone.wav"],
          payload["cleanup"])
    check("cleanup keeps its own budget", payload["cleanup"]["budget_s"] == 7.5,
          payload["cleanup"]["budget_s"])
    check("cleanup closed the event log", emitted == ["selftest", "microphone"], emitted)

    # A cleanup whose own budget is already spent must still stop everything.
    results3: dict[str, object] = {}
    events3 = driver.EventLog(case / "events3.jsonl")
    capture3 = FakeProc()
    recorders3 = [FakeRecorder("late.wav")]
    driver.cleanup(capture3, recorders3, events3, results3, case, budget_s=0.0)
    check("cleanup still stops everything with no budget left",
          capture3.signals == [driver.signal.SIGTERM] and recorders3[0].stop_calls == 1,
          results3["cleanup"])

    # One recorder that raises must not skip the ones after it.
    results4: dict[str, object] = {}
    events4 = driver.EventLog(case / "events4.jsonl")
    healthy = FakeRecorder("healthy.wav")
    driver.cleanup(FakeProc(), [ExplodingRecorder("broken.wav"), healthy], events4, results4, case)
    check("cleanup continues past a failing recorder",
          healthy.stop_calls == 1 and bool(results4.get("cleanup_errors")),
          results4.get("cleanup_errors"))

    # The recorder has to give its log handle back when it stops.
    log_path = case / "recorder.log"
    recorder = driver.Recorder(log_path, 0, case / "rec.wav", 5)
    recorder._handle = log_path.open("ab")
    handle = recorder._handle
    recorder.proc = FakeProc()
    recorder.stop()
    check("Recorder.stop closes its log handle",
          handle.closed and recorder._handle is None, f"closed={handle.closed}")

    # A run that failed before it started anything must still clean up cleanly.
    results2: dict[str, object] = {}
    events2 = driver.EventLog(case / "events2.jsonl")
    driver.cleanup(None, [], events2, results2, case)
    check("cleanup tolerates a run that started nothing",
          results2["cleanup"]["capture_stopped"] is True and results2["cleanup"]["recorders_stopped"] == [],
          results2["cleanup"])


def main() -> int:
    root = HERE / "selfcheck"
    root.mkdir(exist_ok=True)
    audio_checks(root)
    negative_audio_checks(root)
    reference_window_checks(root)
    live_gate_checks(root)
    follow_up_checks()
    barge_source_checks()
    log_checks()
    welcome_checks()
    real_replay_checks()
    probe_checks()
    exit_code_checks()
    rebind_checks()
    cleanup_checks(root)
    if FAILURES:
        print("")
        for failure in FAILURES:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print("")
    print("self-check passed: audio verdicts, the acknowledgement rule, session binding and cleanup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
