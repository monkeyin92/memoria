#!/usr/bin/env python3
"""Fail-closed, machine-only energy analysis for automated audio acceptance.

Reads the recording(s) and `events.jsonl` produced by `auto_audio_session.py`,
measures every marked window, and refines each turn's verdict with the audio
evidence.  It never touches the device, the serial port or the network, so it can
be re-run as often as needed on an existing run directory.

    python3 auto_audio_analyze.py --run-dir outputs/acceptance/run-<stamp>-auto-audio

The result is explicitly machine-side evidence: this script cannot hear, so it
records `machine_analyzed_only=true` and `human_listened=false` and never claims
`Actual Heard`.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import wave
from array import array
from datetime import datetime
from pathlib import Path

# Defaults are deliberately loose: they must separate "silent" from "audible",
# not judge audio quality.  -50 dBFS is a room-noise floor on a MacBook mic.
FRAME_MS = 20
ACTIVE_DBFS = -50.0
SILENCE_DBFS = -55.0
ROBOT_MIN_ACTIVE_MS = 600.0
SELFTEST_MIN_RMS_DBFS = -40.0
SELFTEST_MIN_ABOVE_FLOOR_DB = 12.0
SELFTEST_MIN_RECORDING_S = 8.5
SELFTEST_MIN_ACTIVE_MS = 250.0
SELFTEST_MIN_ACTIVE_RATIO = 0.90
SELFTEST_MIN_LEVEL_RANGE_DB = 8.0
SELFTEST_BASELINE_MAX_S = 3.0
SELFTEST_BASELINE_MIN_S = 1.5
SELFTEST_SEARCH_GUARD_S = 0.25
SELFTEST_SCAN_WINDOW_S = 0.5
ALIGNMENT_TOLERANCE_MS = 150.0
REFERENCE_MIN_NCC = 0.20
# How far one chunk's chosen peak may sit from the lattice the first chunk proposed before
# the three of them stop counting as one coherent occurrence.  This keeps the value the
# 2026-09-15 design chose: a real playback is a rigid waveform, so chunk peaks that disagree
# by more than this are refused rather than fitted.  A 2026-09-20 field run reported chunk
# peaks 0.148 s and 0.170 s off that lattice, which this bound refuses; whether that reading
# was a genuine capture distortion or a peak that locked onto neighbouring speech is NOT
# established, so the bound stays where it is and the refusal is reported with the readout
# below instead of being loosened to make the run pass.
REFERENCE_MAX_SKEW_S = 0.15
# Search bound for a single chunk's peak.  It used to be the same 0.15 s as the lattice
# check, so a peak that sat just outside that window was never found: the same field run's
# third chunk read 0.144 inside the window while its strongest peak anywhere in the take
# read 0.267 at -0.17 s.  Widening the search only lets the peak be *found*; the lattice
# bound above still decides whether the chunks agree, and the negative fixtures below still
# refuse a take that is missing a chunk or holds unrelated sound.
REFERENCE_CHUNK_SEARCH_S = 0.60
FENCE_KEYS = (
    "session_id", "session_epoch", "stream_epoch", "turn_id", "generation_id", "tool_epoch",
)


def match_reference(samples: array, rate: int, path: Path) -> dict[str, object]:
    """Locate a known local playback using three independent waveform chunks.

    Level/envelope alone cannot identify a prompt. Require every third of the known
    waveform to correlate above threshold at one coherent location. This proves neither
    robot audibility nor semantic correctness. Failure is deliberately inconclusive.
    NumPy is optional at import time; unavailable decoding/matching fails closed.
    """
    result: dict[str, object] = {
        "matched": False, "reference_path": str(path), "matches": [],
        "numpy_available": None,
        "method": "three_chunk_normalized_waveform_correlation",
        "min_chunk_ncc": REFERENCE_MIN_NCC, "max_chunk_skew_s": REFERENCE_MAX_SKEW_S,
        "chunk_search_s": REFERENCE_CHUNK_SEARCH_S,
    }
    if not path.is_file():
        return {**result, "reason": "reference_missing"}
    try:
        import numpy as np
        result["numpy_available"] = True
        if path.suffix.lower() == ".wav":
            reference, reference_rate = read_wav_mono(path)
            if reference_rate != rate:
                return {**result, "reason": "reference_sample_rate_mismatch"}
            x = np.asarray(reference, dtype=float)
        else:
            raw = subprocess.check_output(
                ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                 "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
                timeout=10, stderr=subprocess.DEVNULL,
            )
            x = np.frombuffer(raw, dtype="<i2").astype(float)
    except (ImportError, OSError, subprocess.SubprocessError, wave.Error, EOFError, ValueError, SystemExit) as error:
        if isinstance(error, ImportError):
            result["numpy_available"] = False
        return {**result, "reason": "reference_decode_unavailable", "error": str(error)}
    y = np.asarray(samples, dtype=float)
    duration = len(x) / rate
    result["reference_duration_s"] = duration
    if not 0.3 <= duration <= 30 or len(y) < len(x):
        return {**result, "reason": "reference_or_recording_length_invalid"}
    scores = []
    boundaries = [len(x) * i // 3 for i in range(4)]
    for first, last in zip(boundaries, boundaries[1:], strict=False):
        chunk = x[first:last] - x[first:last].mean()
        energy = float(np.sum(chunk * chunk))
        if energy <= len(chunk):
            return {**result, "reason": "reference_chunk_has_no_signal"}
        nfft = 1 << (len(y) + len(chunk) - 1).bit_length()
        correlation = np.fft.irfft(
            np.fft.rfft(y, nfft) * np.fft.rfft(chunk[::-1], nfft), nfft
        )[len(chunk) - 1:len(y)]
        sums = np.r_[0.0, np.cumsum(y)]
        squared = np.r_[0.0, np.cumsum(y * y)]
        variance = squared[len(chunk):] - squared[:-len(chunk)]
        variance -= (sums[len(chunk):] - sums[:-len(chunk)]) ** 2 / len(chunk)
        scores.append(np.abs(correlation) / np.sqrt(np.maximum(variance, 1.0) * energy))
    # Each first-chunk peak proposes an occurrence. Subsequent chunks must agree with
    # its position; a long recording/periodic tone with many hits is ambiguous, not green.
    candidates = scores[0].copy()
    matches = []
    guard = max(int(duration * rate), 1)
    search = max(1, int(REFERENCE_CHUNK_SEARCH_S * rate))
    exhausted = False
    # The strongest candidate's numbers are kept whether or not it is accepted: a refused
    # prompt then reports which chunk was weak and how far the chunks disagree instead of
    # only "reference_not_detected".  Nothing here decides the match.
    readout: dict[str, object] | None = None
    for _ in range(32):
        at = int(np.argmax(candidates))
        if candidates[at] < REFERENCE_MIN_NCC:
            exhausted = True
            break
        candidates[max(0, at - guard):at + guard + 1] = 0
        origins, levels = [at / rate], [float(scores[0][at])]
        peaks = [{"ncc": round(float(scores[0][at]), 5), "offset_s": 0.0}]
        for number in (1, 2):
            expected = at + boundaries[number]
            first, last = max(0, expected - search), min(len(scores[number]), expected + search + 1)
            if last <= first:
                break
            position = first + int(np.argmax(scores[number][first:last]))
            origins.append((position - boundaries[number]) / rate)
            levels.append(float(scores[number][position]))
            # The strongest peak this chunk has anywhere in the take, reported next to the
            # one the lattice chose: a peak that only just fell outside the search window
            # then reads as "clipped", not as "the waveform is missing".
            strongest = int(np.argmax(scores[number]))
            peaks.append({
                "ncc": round(float(scores[number][strongest]), 5),
                "offset_s": round((strongest - boundaries[number]) / rate - at / rate, 6),
            })
        residuals = [round(origin - at / rate, 6) for origin in origins]
        is_first = readout is None
        if is_first:
            readout = {
                "start_s": round(at / rate, 6),
                "chunk_ncc": [round(value, 5) for value in levels],
                "chunk_residual_s": residuals,
                "chunk_global_peak": peaks,
            }
        if len(levels) != 3:
            if is_first:
                readout["rejection"] = "chunk_search_exhausted"
            continue
        if min(levels) < REFERENCE_MIN_NCC:
            if is_first:
                readout["rejection"] = "chunk_ncc_below_min"
            continue
        low, high = min(origins), max(origins)
        if is_first:
            readout["spread_s"] = round(high - low, 6)
        if high - low > REFERENCE_MAX_SKEW_S:
            if is_first:
                readout["rejection"] = "lattice_spread_exceeds_bound"
            continue
        if low < 0 or high + duration > len(y) / rate:
            if is_first:
                readout["rejection"] = "occurrence_outside_recording"
            continue
        matches.append({
            "start_s": round(sum(origins) / 3, 6),
            "start_bounds_s": [round(low, 6), round(high, 6)],
            "end_s": round(high + duration, 6),
            "chunk_ncc": [round(value, 5) for value in levels],
            "chunk_residual_s": residuals,
            "chunk_global_peak": peaks,
        })
    if not exhausted:
        return {**result, "reason": "too_many_reference_candidates", "candidate": readout}
    matches.sort(key=lambda item: item["start_s"])
    return {**result, "matched": bool(matches), "matches": matches, "candidate": readout,
            "reason": "reference_located" if matches else "reference_not_detected"}


def dbfs(amplitude: float) -> float:
    if amplitude <= 0.0:
        return -120.0
    return 20.0 * math.log10(min(amplitude, 1.0))


def read_wav_mono(path: Path) -> tuple[array, int]:
    """Read a 16-bit WAV as mono samples plus its sample rate."""

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    if width != 2:
        raise SystemExit(f"{path}: expected 16-bit PCM, got {width * 8}-bit")
    samples = array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if channels > 1:
        mixed = array("h")
        for index in range(0, len(samples) - channels + 1, channels):
            mixed.append(int(sum(samples[index : index + channels]) / channels))
        samples = mixed
    return samples, rate


def window_metrics(
    samples: array,
    rate: int,
    start_s: float,
    end_s: float,
    *,
    frame_ms: int = FRAME_MS,
    active_dbfs: float = ACTIVE_DBFS,
) -> dict[str, float]:
    """RMS/peak/active-time for one [start_s, end_s) slice of the recording."""

    first = max(0, int(round(start_s * rate)))
    last = min(len(samples), int(round(end_s * rate)))
    duration_s = max(0.0, (last - first) / float(rate))
    if last <= first:
        return {
            "duration_s": round(duration_s, 3),
            "rms_dbfs": -120.0,
            "peak_dbfs": -120.0,
            "active_ms": 0.0,
            "active_ratio": 0.0,
            "floor_dbfs": -120.0,
        }
    window = samples[first:last]
    total = len(window)
    energy = 0.0
    peak = 0
    for value in window:
        energy += float(value) * float(value)
        magnitude = abs(int(value))
        if magnitude > peak:
            peak = magnitude
    rms = math.sqrt(energy / total) / 32768.0
    frame = max(1, int(rate * frame_ms / 1000))
    active_frames = 0
    frames = 0
    threshold = 32768.0 * (10.0 ** (active_dbfs / 20.0))
    frame_rms: list[float] = []
    for offset in range(0, total - frame + 1, frame):
        frames += 1
        chunk_energy = 0.0
        for value in window[offset : offset + frame]:
            chunk_energy += float(value) * float(value)
        level = math.sqrt(chunk_energy / frame)
        frame_rms.append(level)
        if level >= threshold:
            active_frames += 1
    # A quiet-window floor from the low percentile of frame level: robust against a
    # neighbour window bleeding a few frames into this one.
    floor = -120.0
    level_range = 0.0
    core_level_range = 0.0
    if frame_rms:
        level_dbfs = [dbfs(level / 32768.0) for level in frame_rms]
        level_range = max(level_dbfs) - min(level_dbfs)
        margin = max(1, int(len(level_dbfs) * 0.2))
        core = level_dbfs[margin:-margin] if len(level_dbfs) > 2 * margin else level_dbfs
        core_level_range = max(core) - min(core)
        frame_rms.sort()
        floor = dbfs(frame_rms[int(0.2 * (len(frame_rms) - 1))] / 32768.0)
    return {
        "duration_s": round(duration_s, 3),
        "rms_dbfs": round(dbfs(rms), 2),
        "peak_dbfs": round(dbfs(peak / 32768.0), 2),
        "active_ms": round(active_frames * frame_ms, 1),
        "active_ratio": round(active_frames / frames, 4) if frames else 0.0,
        "floor_dbfs": round(floor, 2),
        "level_range_db": round(level_range, 2),
        "core_level_range_db": round(core_level_range, 2),
    }


def _parse_local(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_events(run_dir: Path) -> list[dict[str, object]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        raise SystemExit(f"{path} does not exist; nothing to analyse")
    events: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise SystemExit(f"{path}:{number} is not valid JSON ({error})") from error
        events.append(payload)
    return events


def calibrate_recording(samples, rate, wav, origin, windows):
    """Bound one sample/wall offset with known prompt occurrences, never with spawn.

    afplay start/end bracket playback, not sample zero. Intersect those conservative
    brackets from independent prompts; inconsistent clocks/dropouts refuse alignment.
    No affine stretching, endpoint-duration correction or guessed startup delay is used.
    """
    groups: dict[Path, list[dict[str, object]]] = {}
    for window in windows:
        if window.get("kind") != "prompt" or not window.get("end_local"):
            continue
        name = str(window.get("name", ""))
        reference = window.get("reference_path")
        if reference:
            path = Path(str(reference))
        elif name.startswith("wake_"):
            path = wav.parent / "wake.aiff"
        elif name.startswith("question_"):
            path = wav.parent / f"question-{window.get('turn_index')}.aiff"
        else:
            continue
        groups.setdefault(path, []).append(window)
    anchors = []
    diagnostics = []
    low, high = -math.inf, math.inf
    pad = ALIGNMENT_TOLERANCE_MS / 1000
    for path, prompts in groups.items():
        match = match_reference(samples, rate, path)
        diagnostics.append(match)
        occurrences = match["matches"]
        if len(occurrences) != len(prompts):
            return {"verified": False, "reason": "prompt_match_count_mismatch",
                    "reference_matches": diagnostics, "anchors": anchors}
        for prompt, occurrence in zip(
            sorted(prompts, key=lambda item: item["start_local"]), occurrences, strict=True
        ):
            start = (_parse_local(str(prompt["start_local"])) - origin).total_seconds()
            end = (_parse_local(str(prompt["end_local"])) - origin).total_seconds()
            sample_low, sample_high = occurrence["start_bounds_s"]
            offset_low = start - sample_high - pad
            offset_high = end - float(match["reference_duration_s"]) - sample_low + pad
            anchors.append({
                "name": prompt.get("name"), "start_local": prompt["start_local"],
                "end_local": prompt["end_local"], **occurrence,
                "offset_bounds_s": [offset_low, offset_high],
            })
            low, high = max(low, offset_low), min(high, offset_high)
    if len(anchors) < 2:
        reason = "at_least_two_reference_anchors_required"
    elif low > high:
        reason = "inconsistent_sample_wall_clock"
    elif high - low > 1.5:
        reason = "playback_timing_bracket_too_wide"
    else:
        reason = "bounded_reference_alignment"
    return {
        "verified": reason == "bounded_reference_alignment", "reason": reason,
        "offset_bounds_s": [low, high] if anchors else None, "anchors": anchors,
        "reference_matches": diagnostics, "timing_tolerance_ms": ALIGNMENT_TOLERANCE_MS,
        "scope": "conservative_inner_windows_bracketed_by_prompts_not_precise_gap_measurement",
    }


def analyse_recording(
    wav: Path,
    rec_start_local: str,
    windows: list[dict[str, object]],
    *,
    active_dbfs: float,
) -> dict[str, object]:
    samples, rate = read_wav_mono(wav)
    origin = _parse_local(rec_start_local)
    duration = len(samples) / rate
    alignment = calibrate_recording(samples, rate, wav, origin, windows)
    measured: list[dict[str, object]] = []
    for window in windows:
        named = {**window, "name": window.get("name") or window.get("kind"),
                 "alignment_verified": False}
        if not alignment["verified"]:
            measured.append({**named, "unverified_reason": alignment["reason"]})
            continue
        if not window.get("end_local"):
            measured.append({**named, "unverified_reason": "window_not_closed"})
            continue
        start = (_parse_local(str(window["start_local"])) - origin).total_seconds()
        end = (_parse_local(str(window["end_local"])) - origin).total_seconds()
        low, high = alignment["offset_bounds_s"]
        # Only this intersection is inside the logged window for EVERY possible offset.
        # Verify the enclosing interval is recorded, never silently clip a truncated take.
        outer_start, outer_end = start - high, end - low
        inner_start, inner_end = start - low, end - high
        anchors = alignment["anchors"]
        bracketed = (
            any(_parse_local(a["end_local"]) <= _parse_local(str(window["start_local"])) for a in anchors)
            and any(_parse_local(a["start_local"]) >= _parse_local(str(window["end_local"])) for a in anchors)
        )
        if not bracketed:
            reason = "window_not_bracketed_by_reference_anchors"
        elif outer_start < 0 or outer_end > duration or inner_end <= inner_start:
            reason = "recording_does_not_cover_window"
        elif any(
            prompt.get("kind") == "prompt"
            and _parse_local(str(prompt["start_local"])) < _parse_local(str(window["end_local"]))
            and _parse_local(str(prompt["end_local"])) > _parse_local(str(window["start_local"]))
            for prompt in windows if prompt.get("end_local")
        ):
            reason = "computer_playback_overlaps_window"
        else:
            reason = None
        if reason:
            measured.append({**named, "unverified_reason": reason})
            continue
        stats = window_metrics(
            samples, rate, inner_start, inner_end, active_dbfs=active_dbfs
        )
        measured.append({**named, **stats, "alignment_verified": True,
                         "sample_core_s": [inner_start, inner_end],
                         "sample_enclosing_s": [outer_start, outer_end]})
    if alignment["verified"]:
        quiet_end = min(1.5, min(a["start_bounds_s"][0] for a in alignment["anchors"]) - 0.25)
        if quiet_end >= 1:
            stats = window_metrics(samples, rate, 0, quiet_end, active_dbfs=active_dbfs)
            measured.append({**stats, "name": "recording_floor", "alignment_verified": True,
                             "floor_dbfs": max(stats["floor_dbfs"], stats["rms_dbfs"]),
                             "sample_core_s": [0, quiet_end]})
    return {
        "path": str(wav),
        "rec_start_local": rec_start_local,
        "sample_rate": rate,
        "duration_s": round(len(samples) / float(rate), 3) if rate else 0.0,
        "windows": measured,
        "alignment": alignment,
    }


def _best_selftest_window(
    samples: array,
    rate: int,
    start_s: float,
    end_s: float,
    *,
    floor_dbfs: float,
    active_dbfs: float,
    min_rms_dbfs: float,
    min_above_floor_db: float,
) -> dict[str, object] | None:
    """Find the strongest sample-relative playback slice after the quiet prefix.

    The recording process and the audio device do not share a trustworthy start clock.
    Scanning the WAV itself avoids treating that unmeasured skew as an exact timestamp.
    The candidate is still judged by absolute level, distance from the measured floor and
    active-time ratio; this is not a blanket relaxation of the self-test.
    """

    span = max(0.0, end_s - start_s)
    if rate <= 0 or span <= 0.0:
        return None
    window_s = min(SELFTEST_SCAN_WINDOW_S, span)
    step_s = FRAME_MS / 1000.0
    last_start = max(start_s, end_s - window_s)
    threshold_dbfs = max(min_rms_dbfs, floor_dbfs + min_above_floor_db)
    best: dict[str, object] | None = None
    best_score: tuple[int, float, float, float] | None = None
    candidate = start_s
    while candidate <= last_start + 1e-9:
        stats = window_metrics(
            samples, rate, candidate, candidate + window_s, active_dbfs=active_dbfs
        )
        qualifies = (
            float(stats["rms_dbfs"]) >= threshold_dbfs
            and float(stats["active_ms"]) >= SELFTEST_MIN_ACTIVE_MS
            and float(stats["active_ratio"]) >= SELFTEST_MIN_ACTIVE_RATIO
            and float(stats["core_level_range_db"]) >= SELFTEST_MIN_LEVEL_RANGE_DB
        )
        # Prefer a qualifying window, then the loudest/most active one. Keeping the best
        # non-qualifying window gives the verdict useful evidence instead of hiding silence.
        score = (
            1 if qualifies else 0,
            float(stats["rms_dbfs"]),
            float(stats["active_ms"]),
            float(stats["peak_dbfs"]),
        )
        if best_score is None or score > best_score:
            best_score = score
            best = {
                **stats,
                "start_s": round(candidate, 3),
                "end_s": round(candidate + window_s, 3),
                "qualifies": qualifies,
                "threshold_dbfs": round(threshold_dbfs, 2),
            }
        candidate += step_s
    return best


def analyse_selftest(
    wav: Path,
    rec_start_local: str,
    *,
    active_dbfs: float,
    min_rms_dbfs: float = SELFTEST_MIN_RMS_DBFS,
    min_above_floor_db: float = SELFTEST_MIN_ABOVE_FLOOR_DB,
    reference_path: Path | None = None,
) -> dict[str, object]:
    """Analyse the self-test on the WAV timeline, not the recorder spawn timeline.

    The first quiet prefix is deliberately independent from the driver's event timestamps.
    Playback must uniquely match three chunks of the known local reference waveform.
    Only then can a sustained, above-floor window within that match pass. Sample positions
    are never represented as measured wall-clock timestamps.
    """

    samples, rate = read_wav_mono(wav)
    reference = match_reference(
        samples, rate, reference_path or wav.with_name("selftest-prompt.aiff")
    )
    occurrences = reference["matches"]
    reference_verified = reference["matched"] and len(occurrences) == 1
    duration_s = len(samples) / float(rate) if rate else 0.0
    baseline_end = min(
        duration_s,
        max(SELFTEST_BASELINE_MIN_S, min(SELFTEST_BASELINE_MAX_S, duration_s * 0.2)),
    )
    baseline = window_metrics(samples, rate, 0.0, baseline_end, active_dbfs=active_dbfs)
    baseline_window: dict[str, object] = {
        **baseline,
        "name": "baseline",
        "start_s": 0.0,
        "end_s": round(baseline_end, 3),
        "alignment_method": "sample_relative_quiet_prefix",
    }
    floor_dbfs = float(baseline["floor_dbfs"])
    # The driver intentionally waits three seconds before rendering/playing the prompt.
    # Start just after that known quiet-to-play boundary so a loud environmental sound that
    # begins exactly at the baseline edge cannot win merely by straddling the edge.
    search_start = min(
        duration_s,
        max(SELFTEST_BASELINE_MAX_S + SELFTEST_SEARCH_GUARD_S,
            baseline_end + SELFTEST_SEARCH_GUARD_S),
    )
    search_end = duration_s
    if reference_verified:
        search_start = max(search_start, occurrences[0]["start_bounds_s"][0])
        search_end = min(duration_s, occurrences[0]["end_s"])
    candidate = _best_selftest_window(
        samples,
        rate,
        search_start,
        search_end,
        floor_dbfs=floor_dbfs,
        active_dbfs=active_dbfs,
        min_rms_dbfs=min_rms_dbfs,
        min_above_floor_db=min_above_floor_db,
    )
    windows: list[dict[str, object]] = [baseline_window]
    if candidate is not None:
        play_window = {
            **candidate,
            "name": "selftest_play",
            "search_start_s": round(search_start, 3),
            "search_end_s": round(search_end, 3),
            "alignment_method": "reference_match_then_sample_relative_energy",
            "reference_match_verified": bool(reference_verified),
            "reference_evidence": reference,
        }
        windows.append(play_window)
    return {
        "path": str(wav),
        "rec_start_local": rec_start_local,
        "sample_rate": rate,
        "duration_s": round(duration_s, 3),
        "windows": windows,
        "alignment_method": "reference_match_then_sample_relative_energy",
        "alignment_uncertainty": "sample_relative_only_no_wall_clock_claim",
        "reference_evidence": reference,
        "baseline_search_s": round(baseline_end, 3),
        "playback_search_s": {
            "start": round(search_start, 3),
            "end": round(search_end, 3),
        },
    }


def _pick(measured: list[dict[str, object]], name: str) -> dict[str, object] | None:
    for window in measured:
        if window.get("name") == name:
            return window
    return None


def selftest_verdict(
    recording_duration_s: float,
    measured: list[dict[str, object]],
    *,
    min_recording_s: float,
    min_rms_dbfs: float,
    min_above_floor_db: float,
    min_active_ms: float = SELFTEST_MIN_ACTIVE_MS,
    min_active_ratio: float = SELFTEST_MIN_ACTIVE_RATIO,
    min_level_range_db: float = SELFTEST_MIN_LEVEL_RANGE_DB,
) -> tuple[str, dict[str, object]]:
    """Judge the audio path: a file that is short or quiet is never a pass."""

    baseline = _pick(measured, "baseline")
    play = _pick(measured, "selftest_play")
    floor = float(baseline["floor_dbfs"]) if baseline else SILENCE_DBFS
    evidence: dict[str, object] = {
        "recording_s": round(recording_duration_s, 3),
        "min_recording_s": min_recording_s,
        "baseline_floor_dbfs": floor,
        "baseline_rms_dbfs": baseline["rms_dbfs"] if baseline else None,
        "play_rms_dbfs": play["rms_dbfs"] if play else None,
        "play_peak_dbfs": play["peak_dbfs"] if play else None,
        "play_active_ms": play["active_ms"] if play else None,
        "play_active_ratio": play["active_ratio"] if play else None,
        "play_level_range_db": play["level_range_db"] if play else None,
        "play_core_level_range_db": play["core_level_range_db"] if play else None,
        "margin_db": round(float(play["rms_dbfs"]) - floor, 2) if play else None,
        "min_active_ms": min_active_ms,
        "min_active_ratio": min_active_ratio,
        "min_level_range_db": min_level_range_db,
        "reference_match_verified": play.get("reference_match_verified", False) if play else False,
        "reference_evidence": play.get("reference_evidence") if play else None,
    }
    # A truncated recording means the capture never really ran (denied permission, device
    # grabbed by another process), which no amount of level margin may be allowed to hide.
    if recording_duration_s < min_recording_s:
        return "blocked_microphone", evidence
    if play is None:
        return "selftest_window_missing", evidence
    reference_reason = (play.get("reference_evidence") or {}).get("reason")
    if reference_reason in {
        "reference_missing", "reference_decode_unavailable",
        "reference_sample_rate_mismatch", "reference_or_recording_length_invalid",
        "reference_chunk_has_no_signal",
    }:
        return "blocked_reference_unavailable", evidence
    if float(play["rms_dbfs"]) < min_rms_dbfs:
        return "blocked_output", evidence
    if float(play["rms_dbfs"]) - floor < min_above_floor_db:
        return "blocked_output", evidence
    if float(play["active_ms"]) < min_active_ms or float(play["active_ratio"]) < min_active_ratio:
        return "blocked_output", evidence
    if float(play["core_level_range_db"]) < min_level_range_db:
        return "blocked_output", evidence
    if play.get("reference_match_verified") is not True:
        return "blocked_reference_match", evidence
    return "selftest_passed", evidence


def refine_turns(
    results: dict[str, object],
    measured: list[dict[str, object]],
    *,
    min_active_ms: float,
) -> None:
    """Attach machine-only energy evidence without rewriting the log verdict."""

    floor = None
    baseline = _pick(measured, "recording_floor")
    if baseline is not None and baseline.get("alignment_verified"):
        floor = float(baseline["floor_dbfs"])
    for turn in results.get("turns", []) or []:
        index = turn.get("index")
        windows = [
            window
            for window in measured
            if window.get("name") == "device_speaking" and window.get("turn_index") == index
        ]
        # Old logs/ACKs do not contain this binding and deliberately remain unverified.
        expected = (turn.get("content_window") or {}).get("fence") or {}
        completion = turn.get("completion") or {}
        matches = [w for w in windows if w.get("content_matched") is True
                   and all(expected.get(key) is not None and w.get(key) == expected[key]
                           for key in FENCE_KEYS)
                   and all(w.get(key) == completion.get(key) for key in
                           ("turn_id", "generation_id", "session_epoch", "tool_epoch"))
                   and not completion.get("acknowledgement_marker")]
        answer = matches[0] if len(matches) == 1 else None
        turn.setdefault("verdict_log", turn.get("verdict"))
        turn["audio"] = {
            "speaking_windows": len(windows),
            "answer_window": answer,
            "recording_floor_dbfs": floor,
            "scope": "window_energy_only_not_speech_content_continuity_or_actual_heard",
        }
        if answer is None:
            turn["verdict_audio"] = "unverified_content_window"
            continue
        if not answer.get("alignment_verified"):
            turn["verdict_audio"] = "unverified_alignment"
            continue
        if floor is None or float(answer["duration_s"]) * 1000 < min_active_ms:
            turn["verdict_audio"] = "unverified_recording_coverage"
            continue
        audible = (
            float(answer["active_ms"]) >= min_active_ms
            and float(answer["rms_dbfs"]) > floor + 6.0
        )
        turn["verdict_audio"] = (
            "energy_detected_in_fenced_reply_window" if audible
            else "no_sustained_energy_in_fenced_reply_window"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="New report path; raw results.json is never overwritten")
    parser.add_argument("--active-dbfs", type=float, default=ACTIVE_DBFS)
    parser.add_argument("--robot-min-active-ms", type=float, default=ROBOT_MIN_ACTIVE_MS)
    parser.add_argument("--selftest-min-rms-dbfs", type=float, default=SELFTEST_MIN_RMS_DBFS)
    parser.add_argument(
        "--selftest-min-recording-s", type=float, default=SELFTEST_MIN_RECORDING_S
    )
    parser.add_argument(
        "--selftest-min-above-floor-db", type=float, default=SELFTEST_MIN_ABOVE_FLOOR_DB
    )
    parser.add_argument("--selftest-min-active-ms", type=float, default=SELFTEST_MIN_ACTIVE_MS)
    parser.add_argument(
        "--selftest-min-active-ratio", type=float, default=SELFTEST_MIN_ACTIVE_RATIO
    )
    parser.add_argument(
        "--selftest-min-level-range-db", type=float, default=SELFTEST_MIN_LEVEL_RANGE_DB
    )
    args = parser.parse_args(argv)

    run_dir = args.run_dir
    results_path = run_dir / "results.json"
    results: dict[str, object] = {}
    if results_path.exists():
        results = json.loads(results_path.read_text(encoding="utf-8"))
    events = load_events(run_dir)

    recordings = results.setdefault("recordings", {})
    if not isinstance(recordings, dict):
        raise SystemExit("results.json: recordings must be an object")
    for name in ("selftest", "microphone"):
        wav = run_dir / f"{name}.wav"
        if not wav.exists():
            continue
        rec_start = None
        windows: list[dict[str, object]] = []
        for event in events:
            if event.get("recording") != name:
                continue
            kind = event.get("kind")
            if kind == "recording_start":
                rec_start = str(event.get("start_local"))
                continue
            if kind in ("baseline", "selftest_play", "prompt", "device_speaking"):
                windows.append(dict(event))
        if rec_start is None:
            continue
        if name == "selftest":
            recordings[name] = analyse_selftest(
                wav,
                rec_start,
                active_dbfs=args.active_dbfs,
                min_rms_dbfs=args.selftest_min_rms_dbfs,
                min_above_floor_db=args.selftest_min_above_floor_db,
                reference_path=(run_dir / "selftest-prompt.wav")
                if (run_dir / "selftest-prompt.wav").is_file() else None,
            )
        else:
            recordings[name] = analyse_recording(
                wav, rec_start, windows, active_dbfs=args.active_dbfs
            )

    selftest = recordings.get("selftest")
    if isinstance(selftest, dict):
        verdict, evidence = selftest_verdict(
            float(selftest.get("duration_s") or 0.0),
            selftest.get("windows", []),
            min_recording_s=args.selftest_min_recording_s,
            min_rms_dbfs=args.selftest_min_rms_dbfs,
            min_above_floor_db=args.selftest_min_above_floor_db,
            min_active_ms=args.selftest_min_active_ms,
            min_active_ratio=args.selftest_min_active_ratio,
            min_level_range_db=args.selftest_min_level_range_db,
        )
        results["selftest"] = {"verdict": verdict, "evidence": evidence}

    microphone = recordings.get("microphone")
    if isinstance(microphone, dict):
        refine_turns(results, microphone.get("windows", []), min_active_ms=args.robot_min_active_ms)

    results["source_verification_flags"] = {
        key: results[key] for key in (
            "machine_analyzed_only", "human_listened", "direct_real_device_verified",
            "full_duplex_verified",
        ) if key in results
    }
    results["machine_analyzed_only"] = True
    results["human_listened"] = False
    results["direct_real_device_verified"] = False
    results["full_duplex_verified"] = False
    results.setdefault(
        "notes",
        [
            "Machine analysis only: energy/duration on the Mac microphone.",
            "Actual Heard, direct_real_device_verified and full_duplex_verified stay false.",
        ],
    )
    output = args.output or run_dir / "audio-analysis.json"
    if output.resolve() == results_path.resolve():
        raise SystemExit("refusing to overwrite raw results.json")
    output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
