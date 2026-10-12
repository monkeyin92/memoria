"""Offline diagnosis for the 2026-10-11 stop-word PCM capture.

This tool intentionally does not contact an ASR provider, a device, or a
service.  It combines four independent pieces of local evidence:

* energy in each interrupt window of the admitted-uplink WAV (acoustic signal,
  not a semantic assertion);
* a direct SenseVoice decode when a local sherpa-onnx model is supplied;
* stop/ASR event lines already present in the captured bridge log; and
* normalized cross-correlation between each played stimulus WAV named by the
  receipts and the capture (waveform presence at a lag, not a transcript).

The bridge log stores ASR text lengths rather than transcripts.  Consequently,
the log analysis never claims that an utterance was "停" or "别说了".  The
same fail-closed rule is used when timestamps cannot be aligned to the PCM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
import wave
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
MIN_RMS = 150.0
FIELD_RE = re.compile(r"\b(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>[^\s]*)")
ISO_PREFIX_RE = re.compile(r"^(?P<timestamp>\S+)")
TAP_OPEN_RE = re.compile(r"media PCM tap opened\b")
ASR_FINAL_RE = re.compile(
    r"FunASR realtime final .*?text_len=(?P<text_len>\d+)"
    r" .*?begin_ms=(?P<begin_ms>\d+) .*?end_ms=(?P<end_ms>\d+)"
)
ASR_RESCUE_RE = re.compile(r"funasr segment rescue (?P<kind>[^ ]+).*?pcm_ms=(?P<pcm_ms>\d+)")
STOP_ROUTE_RE = re.compile(r"media spoken stop interrupted reply\b")
STOP_REJECT_RE = re.compile(r"media playback-stop not taken\b")
BOUNDARY_RE = re.compile(r"media_asr_boundary (\{.*\})")
STIMULUS_METHOD = "fft_normalized_cross_correlation_linear_resampled_to_16k"
INTERRUPT_SEARCH_RADIUS_S = 2.0
STIMULUS_LIMIT = (
    "waveform-similarity evidence only: correlation does not prove intelligibility "
    "or word recognition; a low "
    "peak does not prove the stimulus is absent because gain changes, clipping, "
    "nonlinear echo suppression and masking all lower the peak"
)


@dataclass(frozen=True)
class LogEvent:
    """A timestamped, parsed line from the captured bridge log."""

    timestamp: float
    kind: str
    fields: dict[str, Any]
    line_number: int


@dataclass(frozen=True)
class AcousticWindow:
    """Energy measurements for one timeline interval."""

    start_s: float
    end_s: float
    available: bool
    start_frame: int | None
    end_frame: int | None
    rms: float | None
    peak_abs: int | None
    voiced_frames: int
    total_frames: int
    clipped_samples: int
    noise_floor_rms: float | None
    speech_like_energy: bool | None
    method: str


@dataclass(frozen=True)
class OfflineAsrResult:
    """Result of the optional local-only SenseVoice decode."""

    status: str
    model_dir: str
    model_file: str | None
    transcript: str | None
    decode_ms: int | None
    error: str | None
    text_chars: int | None = None


@dataclass(frozen=True)
class Trial:
    """One interrupted reply and its evidence classifications."""

    tag: str
    interrupt_text_declared: str
    expected_stop: bool
    interrupt_start_ts: float | None
    interrupt_end_ts: float | None
    interrupt_deadline_ts: float | None
    acoustic_window: AcousticWindow
    offline_asr: OfflineAsrResult
    log_evidence: dict[str, Any]


@dataclass(frozen=True)
class StimulusMatch:
    """Correlation evidence for one played asset inside the admitted PCM."""

    asset_id: str
    role: str
    file: str
    status: str
    sha256_verified: bool | None
    asset_duration_s: float | None
    expected_lag_s: float | None
    search_center_s: float | None
    search_radius_s: float | None
    match_lag_s: float | None
    offset_s: float | None
    offset_from_search_center_s: float | None
    peak_abs_ncc: float | None
    window_rms_dbfs: float | None
    projected_component_dbfs: float | None
    error: str | None
    limit: str


def parse_timestamp(value: str) -> float | None:
    """Parse an ISO-8601 timestamp, accepting the log's trailing ``Z``."""

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def parse_log_timestamp(line: str) -> float | None:
    """Extract the ISO timestamp at the beginning of a bridge log line."""

    match = ISO_PREFIX_RE.match(line)
    return parse_timestamp(match.group("timestamp")) if match else None


def parse_fields(line: str) -> dict[str, str]:
    """Parse the simple key/value fields used by structured bridge logs."""

    return {match.group("key"): match.group("value") for match in FIELD_RE.finditer(line)}


def parse_bridge_events(text: str) -> tuple[float | None, list[LogEvent]]:
    """Return tap-open time and relevant ASR/stop events from a bridge log."""

    tap_start: float | None = None
    events: list[LogEvent] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        timestamp = parse_log_timestamp(line)
        if timestamp is None:
            continue
        if tap_start is None and TAP_OPEN_RE.search(line):
            tap_start = timestamp
        kind: str | None = None
        fields: dict[str, Any] = parse_fields(line)
        if TAP_OPEN_RE.search(line):
            kind = "tap_open"
        elif STOP_ROUTE_RE.search(line):
            kind = "stop_route"
        elif STOP_REJECT_RE.search(line):
            kind = "stop_rejected"
        else:
            asr_match = ASR_FINAL_RE.search(line)
            if asr_match:
                kind = "asr_final"
                fields.update(
                    text_len=int(asr_match.group("text_len")),
                    begin_ms=int(asr_match.group("begin_ms")),
                    end_ms=int(asr_match.group("end_ms")),
                    transcript_available=False,
                )
            else:
                rescue_match = ASR_RESCUE_RE.search(line)
                if rescue_match:
                    kind = "asr_rescue"
                    fields.update(
                        rescue_kind=rescue_match.group("kind"),
                        pcm_ms=int(rescue_match.group("pcm_ms")),
                        transcript_available=False,
                    )
                else:
                    boundary_match = BOUNDARY_RE.search(line)
                    if boundary_match:
                        try:
                            boundary = json.loads(boundary_match.group(1))
                        except json.JSONDecodeError:
                            boundary = {"parse_error": True}
                        if isinstance(boundary, dict):
                            fields.update(boundary)
                        kind = "asr_boundary"
        if kind is not None:
            events.append(LogEvent(timestamp, kind, fields, line_number))
    return tap_start, events


def scope_events(
    events: Sequence[LogEvent], session: str, epoch: str
) -> tuple[float | None, list[LogEvent], dict[str, Any]]:
    """Isolate a session/epoch; retain session-only lines only for a unique epoch."""

    taps = [e for e in events if e.kind == "tap_open" and e.fields.get("session") == session]
    epochs = {str(e.fields.get("stream_epoch")) for e in taps}
    target_taps = [e for e in taps if str(e.fields.get("stream_epoch")) == epoch]
    tap_start = target_taps[0].timestamp if len(target_taps) == 1 else None
    unique_epoch = epochs == {epoch} and tap_start is not None
    selected: list[LogEvent] = []
    unassigned = 0
    for event in events:
        if event.kind == "tap_open":
            continue
        event_session = event.fields.get("session", event.fields.get("session_id"))
        if event_session != session:
            if event_session is None:
                unassigned += 1
            continue
        event_epoch = event.fields.get("stream_epoch")
        if event_epoch is not None:
            belongs = str(event_epoch) == epoch
        else:
            belongs = bool(unique_epoch and tap_start is not None and event.timestamp >= tap_start)
        if belongs:
            selected.append(event)
        else:
            unassigned += 1
    return tap_start, selected, {
        "session_id": session,
        "stream_epoch": epoch,
        "session_tap_epochs": sorted(epochs),
        "session_only_attribution": "unique_tap_epoch" if unique_epoch else "unverified",
        "unattributable_events": unassigned,
        "stop_rejections_total": sum(e.kind == "stop_rejected" for e in selected),
        "stop_execution_total": sum(e.kind == "stop_route" for e in selected),
        "limit": "ASR lines without session identity are not assigned by temporal proximity",
    }


def read_wav(path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    """Read and validate the admitted PCM WAV without accepting resampling."""

    with wave.open(str(path), "rb") as audio:
        info: dict[str, Any] = {
            "sample_rate_hz": audio.getframerate(),
            "channels": audio.getnchannels(),
            "sample_width_bytes": audio.getsampwidth(),
            "frames": audio.getnframes(),
        }
        if (
            info["sample_rate_hz"],
            info["channels"],
            info["sample_width_bytes"],
        ) != (SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH):
            raise ValueError(
                f"{path}: expected 16 kHz mono s16le, got "
                f"{info['sample_rate_hz']} Hz/{info['channels']} ch/{info['sample_width_bytes']} bytes"
            )
        raw = audio.readframes(audio.getnframes())
        expected_bytes = info["frames"] * info["channels"] * info["sample_width_bytes"]
        if len(raw) != expected_bytes:
            actual_frames = len(raw) // (info["channels"] * info["sample_width_bytes"])
            raise ValueError(
                f"{path}: truncated WAV file, expected {info['frames']} frames ({expected_bytes} bytes), "
                f"read {actual_frames} frames ({len(raw)} bytes)"
            )
    samples = np.frombuffer(raw, dtype="<i2").astype(np.int16)
    info["duration_s"] = round(len(samples) / SAMPLE_RATE, 3)
    info["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return samples, info


def _frame_rms(samples: np.ndarray) -> np.ndarray:
    """Return RMS for complete 30 ms frames."""

    count = len(samples) // FRAME_SAMPLES
    if count == 0:
        return np.empty(0, dtype=np.float64)
    frames = samples[: count * FRAME_SAMPLES].reshape(count, FRAME_SAMPLES).astype(np.float64)
    return np.sqrt(np.mean(frames * frames, axis=1))


def resample_linear(
    samples: np.ndarray, source_rate: int, target_rate: int = SAMPLE_RATE
) -> np.ndarray:
    """Resample by linear interpolation; deterministic, approximate at ratios above 1."""

    source = np.asarray(samples, dtype=np.float64)
    if len(source) == 0:
        return source
    if source_rate == target_rate:
        return source
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError(f"invalid resample rates {source_rate} -> {target_rate}")
    target_count = int(round(len(source) * target_rate / source_rate))
    positions = np.arange(target_count, dtype=np.float64) * (source_rate / target_rate)
    return np.interp(positions, np.arange(len(source), dtype=np.float64), source)


def read_stimulus_wav(path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    """Read a played-asset WAV and return 16 kHz mono samples plus its metadata."""

    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        width = audio.getsampwidth()
        rate = audio.getframerate()
        frames = audio.getnframes()
        raw = audio.readframes(frames)
    if channels != CHANNELS or width != SAMPLE_WIDTH:
        raise ValueError(f"{path}: expected mono s16 WAV, got {channels} ch/{width} bytes")
    if rate <= 0:
        raise ValueError(f"{path}: invalid sample rate {rate}")
    expected_bytes = frames * channels * width
    if len(raw) != expected_bytes:
        raise ValueError(
            f"{path}: truncated WAV file, expected {frames} frames ({expected_bytes} bytes), "
            f"read {len(raw) // (channels * width)} frames ({len(raw)} bytes)"
        )
    info: dict[str, Any] = {
        "path": str(path),
        "source_sample_rate_hz": rate,
        "frames": frames,
        "duration_s": round(frames / rate, 3) if rate else None,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    source = np.frombuffer(raw, dtype="<i2")
    return resample_linear(source, rate, SAMPLE_RATE), info


def normalized_cross_correlation(samples: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Return the absolute zero-mean NCC at every valid template lag.

    Per-window mean removal rejects DC bias. Normalization makes the peak
    invariant to constant capture-side gain changes.
    """

    x = np.asarray(samples, dtype=np.float64)
    t = np.asarray(template, dtype=np.float64)
    n = len(t)
    if n == 0 or len(x) < n:
        return np.empty(0, dtype=np.float64)
    size = 1
    while size < len(x) + n - 1:
        size <<= 1
    centered_template = t - float(np.mean(t))
    corr = np.fft.irfft(
        np.fft.rfft(x, size) * np.fft.rfft(centered_template[::-1], size), size
    )[n - 1 : len(x)]
    cumulative = np.concatenate(([0.0], np.cumsum(x * x)))
    cumulative_sum = np.concatenate(([0.0], np.cumsum(x)))
    window_energy = cumulative[n:] - cumulative[:-n]
    window_sum = cumulative_sum[n:] - cumulative_sum[:-n]
    centered_window_energy = np.maximum(window_energy - (window_sum * window_sum / n), 0.0)
    denominator = np.sqrt(centered_window_energy * float(np.dot(centered_template, centered_template)))
    ncc = np.zeros_like(corr)
    np.divide(corr, denominator, out=ncc, where=denominator > 0)
    return np.abs(ncc)


def _dbfs(rms_value: float) -> float | None:
    """Convert an int16-scale RMS to dBFS, or None for digital silence."""

    if not rms_value > 0.0:
        return None
    return round(20.0 * math.log10(rms_value / 32768.0), 2)


def _stimulus_asset(assets: Sequence[Any], tag: str) -> Mapping[str, Any] | None:
    """Return the longest receipt asset id that names this step's question file."""

    candidates = [
        asset
        for asset in assets
        if isinstance(asset, Mapping)
        and isinstance(asset.get("id"), str)
        and (tag == asset["id"] or tag.endswith(f"-{asset['id']}"))
    ]
    return max(candidates, key=lambda asset: len(str(asset["id"])), default=None)


def _interrupt_asset(
    assets: Sequence[Any], tag: str, question: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """Return the receipt asset for the file the driver played to interrupt."""

    question_id = str(question["id"]) if question is not None else None
    for asset in assets:
        if not isinstance(asset, Mapping) or not isinstance(asset.get("id"), str):
            continue
        asset_id = str(asset["id"])
        if question_id is not None and asset_id == f"{question_id}-stop":
            return asset
    return None


def _hms_seconds(value: Any) -> float | None:
    """Parse the driver's ``HH:MM:SS.mmm`` clock strings."""

    if not isinstance(value, str):
        return None
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def _expected_lag(row: Mapping[str, Any], *, role: str, tap_start: float) -> float | None:
    """Driver wall-clock stimulus start, mapped onto the concatenated PCM timeline."""

    if role == "interrupt":
        value = row.get("interrupt_start_ts")
        return round(float(value) - tap_start, 3) if isinstance(value, (int, float)) else None
    end = _hms_seconds(row.get("end"))
    start = _hms_seconds(row.get("start"))
    reference = row.get("ts")
    if end is None or start is None or not isinstance(reference, (int, float)):
        return None
    return round(float(reference) - (end - start) - tap_start, 3)


def _match_stimulus(
    samples: np.ndarray,
    asset: Mapping[str, Any],
    *,
    role: str,
    expected_lag_s: float | None,
    search_center_s: float | None,
    search_radius_s: float | None,
    tap_start: float | None,
) -> StimulusMatch:
    """Correlate one receipt asset against the full capture, fail-closed."""

    asset_id = str(asset.get("id", ""))
    file_value = str(asset.get("file", ""))

    def record(
        status: str,
        *,
        sha256_verified: bool | None = None,
        asset_duration_s: float | None = None,
        match_lag_s: float | None = None,
        offset_s: float | None = None,
        peak_abs_ncc: float | None = None,
        window_rms_dbfs: float | None = None,
        projected_component_dbfs: float | None = None,
        error: str | None = None,
    ) -> StimulusMatch:
        return StimulusMatch(
            asset_id,
            role,
            file_value,
            status,
            sha256_verified,
            asset_duration_s,
            expected_lag_s,
            search_center_s,
            search_radius_s,
            match_lag_s,
            offset_s,
            (
                round(match_lag_s - search_center_s, 3)
                if match_lag_s is not None and search_center_s is not None
                else None
            ),
            peak_abs_ncc,
            window_rms_dbfs,
            projected_component_dbfs,
            error,
            STIMULUS_LIMIT,
        )

    if not file_value:
        return record("missing_file", error="receipt asset has no file path")
    path = Path(file_value)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        return record("missing_file", error=f"not found: {path}")
    try:
        stimulus, info = read_stimulus_wav(path)
    except (OSError, ValueError, wave.Error) as exc:
        return record("read_error", error=f"{type(exc).__name__}: {exc}")
    wanted = asset.get("sha256")
    verified = None
    if isinstance(wanted, str):
        verified = wanted == info["sha256"]
        if not verified:
            return record(
                "hash_mismatch",
                sha256_verified=False,
                asset_duration_s=info["duration_s"],
                error=f"expected {wanted}, got {info['sha256']}",
            )
    if tap_start is None:
        return record(
            "unaligned_pcm",
            sha256_verified=verified,
            asset_duration_s=info["duration_s"],
            error="tap-open timestamp missing; offsets cannot be mapped",
        )
    if len(stimulus) == 0 or len(stimulus) > len(samples):
        return record(
            "length_mismatch",
            sha256_verified=verified,
            asset_duration_s=info["duration_s"],
            error=f"asset {len(stimulus)} samples vs capture {len(samples)}",
        )
    search_start = 0
    search_end = len(samples)
    if search_center_s is not None and search_radius_s is not None:
        search_start = max(0, int(round((search_center_s - search_radius_s) * SAMPLE_RATE)))
        search_end = min(
            len(samples),
            int(round((search_center_s + search_radius_s) * SAMPLE_RATE)) + len(stimulus),
        )
    search_samples = samples[search_start:search_end]
    ncc = normalized_cross_correlation(search_samples, stimulus)
    if len(ncc) == 0:
        return record(
            "search_window_too_short",
            sha256_verified=verified,
            asset_duration_s=info["duration_s"],
            error=f"search window has {len(search_samples)} samples; asset has {len(stimulus)}",
        )
    index = int(np.argmax(ncc))
    absolute_index = search_start + index
    peak = float(ncc[index])
    match_lag_s = round(absolute_index / SAMPLE_RATE, 3)
    window = samples[absolute_index : absolute_index + len(stimulus)].astype(np.float64)
    template = np.asarray(stimulus, dtype=np.float64)
    centered_template = template - float(np.mean(template))
    centered_window = window - float(np.mean(window))
    template_energy = float(np.dot(centered_template, centered_template))
    gain = (
        float(np.dot(centered_window, centered_template) / template_energy)
        if template_energy > 0
        else 0.0
    )
    component_rms = abs(gain) * math.sqrt(template_energy / len(template))
    window_rms = float(np.sqrt(np.mean(window * window)))
    offset = round(match_lag_s - expected_lag_s, 3) if expected_lag_s is not None else None
    return record(
        "measured",
        sha256_verified=verified,
        asset_duration_s=info["duration_s"],
        match_lag_s=match_lag_s,
        offset_s=offset,
        peak_abs_ncc=round(peak, 3),
        window_rms_dbfs=_dbfs(window_rms),
        projected_component_dbfs=_dbfs(component_rms),
    )


def stimulus_alignment(
    rows: Sequence[Mapping[str, Any]],
    samples: np.ndarray,
    tap_start: float | None,
    receipt_data: Mapping[str, Any],
) -> dict[str, Any]:
    """Locate every played asset inside the capture by waveform correlation."""

    stimuli = receipt_data.get("stimuli")
    assets = stimuli.get("assets") if isinstance(stimuli, Mapping) else None
    if not isinstance(assets, list) or not assets:
        return {
            "status": "unavailable_no_stimulus_receipt",
            "method": STIMULUS_METHOD,
            "search": "full_capture_lag_0_to_len_minus_template",
            "question_offsets_s": [],
            "mean_offset_s": None,
            "matches": [],
            "limit": STIMULUS_LIMIT,
        }
    said_rows = {str(row.get("tag")): row for row in rows if row.get("kind") == "said"}
    matches: list[StimulusMatch] = []
    for row in _trial_rows(rows):
        tag = str(row.get("tag", ""))
        question = _stimulus_asset(assets, tag)
        if question is not None:
            said = said_rows.get(tag)
            expected = (
                _expected_lag(said, role="question", tap_start=tap_start)
                if said is not None and tap_start is not None
                else None
            )
            matches.append(
                _match_stimulus(
                    samples,
                    question,
                    role="question",
                    expected_lag_s=expected,
                    search_center_s=None,
                    search_radius_s=None,
                    tap_start=tap_start,
                )
            )
    offsets = [
        match.offset_s
        for match in matches
        if match.role == "question" and match.offset_s is not None
    ]
    calibrated_offset = float(np.median(offsets)) if offsets else None
    for row in _trial_rows(rows):
        tag = str(row.get("tag", ""))
        question = _stimulus_asset(assets, tag)
        interrupt = _interrupt_asset(assets, tag, question)
        if interrupt is not None:
            expected = _expected_lag(row, role="interrupt", tap_start=tap_start) if tap_start is not None else None
            center = (
                expected + calibrated_offset
                if expected is not None and calibrated_offset is not None
                else expected
            )
            matches.append(
                _match_stimulus(
                    samples,
                    interrupt,
                    role="interrupt",
                    expected_lag_s=expected,
                    search_center_s=center,
                    search_radius_s=INTERRUPT_SEARCH_RADIUS_S if center is not None else None,
                    tap_start=tap_start,
                )
            )
    return {
        "status": "available",
        "method": STIMULUS_METHOD,
        "search": {
            "question": "full_capture_lag_0_to_len_minus_template",
            "interrupt": "question_median_offset_centered_window",
            "interrupt_radius_s": INTERRUPT_SEARCH_RADIUS_S,
        },
        "question_offsets_s": offsets,
        "mean_offset_s": round(sum(offsets) / len(offsets), 3) if offsets else None,
        "median_offset_s": round(calibrated_offset, 3) if calibrated_offset is not None else None,
        "matches": [asdict(match) for match in matches],
        "limit": STIMULUS_LIMIT,
    }


def acoustic_window(
    samples: np.ndarray,
    start_s: float,
    end_s: float,
    *,
    global_frame_rms: np.ndarray | None = None,
) -> AcousticWindow:
    """Measure energy, clipping, and speech-like frame count in an interval.

    ``speech_like_energy`` only means that the interval exceeds an adaptive
    energy floor.  It does not identify words or prove that audio is speech.
    """

    start_frame = int(round(start_s * SAMPLE_RATE))
    end_frame = int(round(end_s * SAMPLE_RATE))
    if start_frame < 0 or end_frame > len(samples) or end_frame <= start_frame:
        return AcousticWindow(
            start_s,
            end_s,
            False,
            None,
            None,
            None,
            None,
            0,
            0,
            0,
            None,
            None,
            "30ms_rms_above_max(3*p20_global_rms,150)",
        )
    window = samples[start_frame:end_frame]
    frame_rms = _frame_rms(window)
    baseline = global_frame_rms if global_frame_rms is not None else _frame_rms(samples)
    noise_floor = float(np.percentile(baseline, 20)) if len(baseline) else None
    threshold = max(3 * noise_floor, MIN_RMS) if noise_floor is not None else MIN_RMS
    voiced_frames = int(np.count_nonzero(frame_rms > threshold))
    rms = float(np.sqrt(np.mean(window.astype(np.float64) ** 2)))
    absolute_samples = np.abs(window.astype(np.int32))
    peak = int(np.max(absolute_samples))
    clipped = int(np.count_nonzero(absolute_samples >= 32767))
    return AcousticWindow(
        start_s,
        end_s,
        True,
        start_frame,
        end_frame,
        round(rms, 3),
        peak,
        voiced_frames,
        len(frame_rms),
        clipped,
        round(noise_floor, 3) if noise_floor is not None else None,
        voiced_frames > 0,
        "30ms_rms_above_max(3*p20_global_rms,150)",
    )


def _model_files(model_dir: Path) -> tuple[Path | None, Path | None]:
    """Find the supported SenseVoice model and its token file."""

    model = next((model_dir / name for name in ("model.int8.onnx", "model.onnx") if (model_dir / name).is_file()), None)
    tokens = model_dir / "tokens.txt"
    return model, tokens if tokens.is_file() else None


def decode_sensevoice(
    samples: np.ndarray,
    model_dir: Path,
    *,
    show_text: bool,
) -> OfflineAsrResult:
    """Decode with a local SenseVoice model, never making network calls."""

    model, tokens = _model_files(model_dir)
    if model is None or tokens is None:
        missing = []
        if model is None:
            missing.append("model.(int8.)onnx")
        if tokens is None:
            missing.append("tokens.txt")
        return OfflineAsrResult(
            "unavailable_missing_model",
            str(model_dir),
            model.name if model else None,
            None,
            None,
            "missing " + ", ".join(missing),
        )
    try:
        import sherpa_onnx  # type: ignore[import-not-found]
    except ImportError as exc:
        return OfflineAsrResult(
            "unavailable_missing_dependency",
            str(model_dir),
            model.name,
            None,
            None,
            f"sherpa_onnx is not installed: {exc}",
        )
    try:
        recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens), language="zh", use_itn=True
        )
        stream = recognizer.create_stream()
        audio = samples.astype(np.float32) / 32768.0
        stream.accept_waveform(SAMPLE_RATE, audio)
        started = time.monotonic()
        recognizer.decode_stream(stream)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        raw_text = str(stream.result.text or "").strip()
        # Keep text out of the default report; a non-empty marker still answers
        # whether this local recognizer produced any output.
        return OfflineAsrResult(
            "decoded",
            str(model_dir),
            model.name,
            raw_text if show_text else None,
            elapsed_ms,
            None,
            len(raw_text),
        )
    except Exception as exc:  # model/runtime errors are evidence, not crashes
        return OfflineAsrResult(
            "decode_error",
            str(model_dir),
            model.name,
            None,
            None,
            f"{type(exc).__name__}: {exc}",
        )


def _load_timeline(path: Path) -> list[dict[str, Any]]:
    """Load JSONL timeline rows and retain only well-formed objects."""

    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _trial_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Return interrupted replies with explicit interrupt timestamps."""

    return [
        row
        for row in rows
        if row.get("kind") == "reply"
        and row.get("interrupted") is True
        and isinstance(row.get("interrupt_start_ts"), (int, float))
        and isinstance(row.get("interrupt_end_ts"), (int, float))
    ]


def events_in_window(events: Sequence[LogEvent], start: float, end: float) -> list[LogEvent]:
    """Select events in a closed timestamp window."""

    return [event for event in events if start <= event.timestamp <= end]


def _asr_summary(events: Sequence[LogEvent], start: float, end: float) -> dict[str, Any]:
    """Summarize ASR evidence without inventing missing transcript text."""

    scoped = events_in_window(events, start, end)
    finals = [event for event in scoped if event.kind == "asr_final"]
    rescues = [event for event in scoped if event.kind == "asr_rescue"]
    boundaries = [event for event in scoped if event.kind == "asr_boundary"]
    return {
        "asr_result_events": len(finals),
        "asr_result_text_available": False,
        "asr_final_text_lengths": [event.fields.get("text_len") for event in finals],
        "asr_rescue_events": len(rescues),
        "asr_boundary_events": len(boundaries),
        "boundary_clipping_true": sum(
            event.fields.get("provider_pcm_clipping_detected") is True for event in boundaries
        ),
        "evidence_limit": "bridge.log stores text_len only; no transcript semantics can be verified",
    }


def _log_summary(events: Sequence[LogEvent], start: float, end: float) -> dict[str, Any]:
    """Classify stop route invocation, rejection, and execution in a trial."""

    scoped = events_in_window(events, start, end)
    route = [event for event in scoped if event.kind == "stop_route"]
    rejected = [event for event in scoped if event.kind == "stop_rejected"]
    return {
        "stop_route_invocations": len(route),
        "stop_route_executed": bool(route),
        "stop_route_rejections": len(rejected),
        "rejection_stop_word_values": [event.fields.get("stop_word") for event in rejected],
        "rejection_reasons": [event.fields.get("reason") for event in rejected],
        "evidence_limit": (
            "a stop route is counted only from 'media spoken stop interrupted reply'; "
            "'playback-stop not taken' is a rejection, not execution"
        ),
    }


def _receipt_check(run: Path, receipts: Path | None, wav_info: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the capture against the optional immutable receipt."""

    result: dict[str, Any] = {"receipt_available": bool(receipts and receipts.is_file())}
    if receipts is None or not receipts.is_file():
        result["status"] = "receipt_unavailable"
        return result
    try:
        data = json.loads(receipts.read_text(encoding="utf-8"))
        files = data.get("files", []) if isinstance(data, dict) else []
        capture_name = str((run / "admitted-uplink.wav").relative_to(Path.cwd())) if (run / "admitted-uplink.wav").is_relative_to(Path.cwd()) else str(run / "admitted-uplink.wav")
        match = next((item for item in files if isinstance(item, dict) and item.get("file", "").endswith("/admitted-uplink.wav")), None)
        result["capture_receipt"] = match
        result["status"] = "match" if match and match.get("sha256") == wav_info.get("sha256") and match.get("size_bytes") == (run / "admitted-uplink.wav").stat().st_size else "mismatch_or_missing"
        result["capture_path"] = capture_name
    except (OSError, json.JSONDecodeError) as exc:
        result["status"] = "receipt_error"
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def diagnose(
    run: Path,
    *,
    receipts: Path | None = None,
    model_dir: Path = Path("/models/sensevoice"),
    show_text: bool = False,
) -> dict[str, Any]:
    """Build a deterministic, offline diagnosis report for an acceptance run."""

    wav_path = run / "admitted-uplink.wav"
    timeline_path = run / "timeline.jsonl"
    bridge_path = run / "bridge.log"
    for path in (wav_path, timeline_path, bridge_path):
        if not path.is_file():
            raise FileNotFoundError(f"required evidence file missing: {path}")
    samples, wav_info = read_wav(wav_path)
    timeline = _load_timeline(timeline_path)
    _, events = parse_bridge_events(bridge_path.read_text(encoding="utf-8", errors="replace"))
    receipt_data = json.loads(receipts.read_text(encoding="utf-8")) if receipts and receipts.is_file() else {}
    sessions = {str(row["reply_delivery_id"]).split("/")[0] for row in timeline if row.get("reply_delivery_id")}
    if len(sessions) > 1:
        raise ValueError(f"mixed timeline: multiple session IDs: {sorted(sessions)}")
    timeline_session = next(iter(sessions)) if len(sessions) == 1 else None
    receipt_session = str(receipt_data["session_id"]) if "session_id" in receipt_data else None
    if receipt_session is not None and timeline_session is not None and receipt_session != timeline_session:
        raise ValueError(
            f"session conflict: receipt session {receipt_session} does not match timeline session {timeline_session}"
        )
    session = receipt_session or timeline_session or ""
    tap_epochs = {str(e.fields.get("stream_epoch")) for e in events if e.kind == "tap_open" and (not session or e.fields.get("session") == session)}
    receipt_epoch = str(receipt_data["stream_epoch"]) if "stream_epoch" in receipt_data else None
    if receipt_epoch is not None and tap_epochs and receipt_epoch not in tap_epochs:
        raise ValueError(
            f"epoch conflict: receipt stream_epoch {receipt_epoch} does not match tap epochs {sorted(tap_epochs)}"
        )
    epoch = receipt_epoch or (next(iter(tap_epochs)) if len(tap_epochs) == 1 else "")
    tap_start, events, identity = scope_events(events, session, epoch)
    global_rms = _frame_rms(samples)
    alignment = stimulus_alignment(timeline, samples, tap_start, receipt_data)
    trial_reports: list[Trial] = []
    for row in _trial_rows(timeline):
        start = float(row["interrupt_start_ts"])
        end = float(row["interrupt_end_ts"])
        deadline_value = row.get("interrupt_deadline_ts")
        deadline = float(deadline_value) if isinstance(deadline_value, (int, float)) else end
        if tap_start is None:
            acoustic = acoustic_window(samples, 1.0, 0.0, global_frame_rms=global_rms)
        else:
            acoustic = acoustic_window(
                samples,
                start - tap_start,
                end - tap_start,
                global_frame_rms=global_rms,
            )
        decode_samples = (
            samples[acoustic.start_frame : acoustic.end_frame]
            if acoustic.available and acoustic.start_frame is not None and acoustic.end_frame is not None
            else np.empty(0, dtype=np.int16)
        )
        asr_result = (
            decode_sensevoice(decode_samples, model_dir, show_text=show_text)
            if len(decode_samples)
            else OfflineAsrResult(
                "unavailable_unaligned_pcm",
                str(model_dir),
                None,
                None,
                None,
                "tap-open timestamp missing or interval outside capture",
            )
        )
        log_evidence = _log_summary(events, start, deadline)
        log_evidence.update(_asr_summary(events, start, deadline))
        trial_reports.append(
            Trial(
                tag=str(row.get("tag", "unknown")),
                interrupt_text_declared=str(row.get("interrupt_text", "")),
                expected_stop=bool(row.get("tag", "").endswith(("stop-a", "stop-b"))),
                interrupt_start_ts=start,
                interrupt_end_ts=end,
                interrupt_deadline_ts=deadline,
                acoustic_window=acoustic,
                offline_asr=asr_result,
                log_evidence=log_evidence,
            )
        )
    report: dict[str, Any] = {
        "schema_version": "stop-pcm-diagnosis-v2",
        "offline_only": True,
        "run": str(run),
        "capture": wav_info,
        "tap_open_ts": tap_start,
        "log_identity": identity,
        "pcm_wall_clock_alignment": {
            "status": "approximate_only",
            "method": "interrupt_wall_timestamp_minus_first_tap_open_timestamp",
            "verified": False,
            "limit": (
                "admitted PCM is concatenated, not a wall-clock recording; "
                "packet delays, rejected frames or ingress gaps can shift windows. "
                "No sample-to-wall-clock mapping is retained; these windows are "
                "review candidates, not verified stimulus alignment."
            ),
        },
        "trial_count": len(trial_reports),
        "trials": [asdict(trial) for trial in trial_reports],
        "stimulus_alignment": alignment,
        "receipt": _receipt_check(run, receipts, wav_info),
        "global_evidence_boundary": {
            "acoustic": "energy threshold detects signal-like audio only; it cannot identify words",
            "offline_asr": "semantic recognition is reported only when a local SenseVoice model is available and decoded",
            "service_logs": "captured logs expose ASR text_len, not transcript text; stop semantics are therefore not inferable from text",
            "cloud_access": "not attempted",
        },
    }
    return report


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line options."""

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("run", type=Path, help="acceptance evidence directory")
    parser.add_argument("--receipts", type=Path, help="optional docs acceptance receipts.json")
    parser.add_argument("--model-dir", type=Path, default=Path("/models/sensevoice"))
    parser.add_argument("--output", type=Path, help="write JSON report here")
    parser.add_argument("--show-text", action="store_true", help="include local ASR text (private speech)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline diagnosis and print JSON."""

    args = parse_args(argv)
    receipts = args.receipts
    if receipts is None:
        candidate = Path("docs/acceptance") / args.run.name / "receipts.json"
        receipts = candidate if candidate.is_file() else None
    report = diagnose(args.run, receipts=receipts, model_dir=args.model_dir, show_text=args.show_text)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
