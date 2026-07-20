"""CosyVoice Realtime protocol parse/map and timestamp scaling (ch.15)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from services.agent.src.contracts.events import TimedWord

CosyEventType = Literal[
    "task-started",
    "sentence-begin",
    "sentence-synthesis",
    "sentence-end",
    "task-finished",
    "task-failed",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class CosyWord:
    text: str
    begin_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class CosyServerEvent:
    event: CosyEventType
    task_id: str
    sentence_index: int | None = None
    text: str | None = None
    words: tuple[TimedWord, ...] = ()
    error_message: str | None = None
    raw: dict[str, Any] | None = None


def build_run_task(
    *,
    task_id: str | None = None,
    model: str = "cosyvoice-v3-flash",
    voice: str = "longanyang",
    sample_rate: int = 24000,
    rate: float = 1.0,
    pitch: float = 1.0,
    volume: int = 50,
    word_timestamp_enabled: bool = True,
    instruction: str | None = None,
) -> dict[str, Any]:
    tid = task_id or str(uuid.uuid4())
    request: dict[str, Any] = {
        "header": {
            "action": "run-task",
            "task_id": tid,
            "streaming": "duplex",
        },
        "payload": {
            "task_group": "audio",
            "task": "tts",
            "function": "SpeechSynthesizer",
            "model": model,
            "parameters": {
                "text_type": "PlainText",
                "voice": voice,
                "format": "pcm",
                "sample_rate": sample_rate,
                "volume": volume,
                "rate": rate,
                "pitch": pitch,
                "enable_ssml": False,
                "word_timestamp_enabled": word_timestamp_enabled,
                "language_hints": ["zh"],
            },
            "input": {},
        },
    }
    if instruction:
        request["payload"]["parameters"]["instruction"] = instruction
    return request


def build_continue_text(task_id: str, text: str) -> dict[str, Any]:
    return {
        "header": {
            "action": "continue-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {
            "input": {
                "text": text,
            },
        },
    }


def build_finish_task(task_id: str) -> dict[str, Any]:
    return {
        "header": {
            "action": "finish-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {"input": {}},
    }


def parse_server_message(data: str | bytes | dict[str, Any]) -> CosyServerEvent:
    if isinstance(data, bytes):
        # Binary frames are PCM, not JSON events.
        raise TypeError("binary PCM is not a JSON server event")
    if isinstance(data, str):
        obj: dict[str, Any] = json.loads(data)
    else:
        obj = data

    header = obj.get("header") or {}
    wire_event_name = str(header.get("event") or header.get("action") or "unknown")
    task_id = str(header.get("task_id") or "")
    payload = obj.get("payload") or {}
    output = payload.get("output") or payload
    sentence = output.get("sentence") or output
    event_name = (
        str(output.get("type") or "unknown")
        if wire_event_name == "result-generated"
        else wire_event_name
    )

    if event_name == "task-started":
        return CosyServerEvent(event="task-started", task_id=task_id, raw=obj)
    if event_name == "task-finished":
        return CosyServerEvent(event="task-finished", task_id=task_id, raw=obj)
    if event_name == "task-failed":
        return CosyServerEvent(
            event="task-failed",
            task_id=task_id,
            error_message=str(
                header.get("error_message")
                or header.get("message")
                or payload.get("error_message")
                or payload.get("message")
                or "task-failed"
            ),
            raw=obj,
        )
    if event_name == "sentence-begin":
        return CosyServerEvent(
            event="sentence-begin",
            task_id=task_id,
            sentence_index=int(
                sentence.get("index")
                or sentence.get("sentence_index")
                or output.get("index")
                or 0
            ),
            text=str(output.get("original_text") or sentence.get("text") or ""),
            raw=obj,
        )
    if event_name == "sentence-synthesis":
        return CosyServerEvent(
            event="sentence-synthesis",
            task_id=task_id,
            sentence_index=int(sentence.get("index") or output.get("index") or 0),
            raw=obj,
        )
    if event_name == "sentence-end":
        words = _parse_words(
            sentence.get("words") or output.get("words") or payload.get("words") or []
        )
        return CosyServerEvent(
            event="sentence-end",
            task_id=task_id,
            sentence_index=int(sentence.get("index") or output.get("index") or 0),
            text=str(output.get("original_text") or sentence.get("text") or ""),
            words=words,
            raw=obj,
        )
    return CosyServerEvent(event="unknown", task_id=task_id, raw=obj)


def _parse_words(raw: list[Any]) -> tuple[TimedWord, ...]:
    words: list[TimedWord] = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        begin = int(w.get("begin_time") or w.get("begin_ms") or 0)
        end = int(w.get("end_time") or w.get("end_ms") or begin)
        words.append(
            TimedWord(
                text=str(w.get("text") or ""),
                begin_ms=begin,
                end_ms=end,
                punctuation=str(w.get("punctuation") or ""),
            )
        )
    return tuple(words)


def pcm_duration_ms(pcm_bytes: bytes, *, sample_rate: int = 24000, num_channels: int = 1) -> int:
    # 16-bit PCM
    samples = len(pcm_bytes) // 2 // max(1, num_channels)
    return int(samples * 1000 / sample_rate)


def normalize_pcm16_peak(
    pcm_bytes: bytes,
    *,
    soft_peak: float = 0.35,
    soft_target: float = 0.58,
    loud_peak: float = 0.93,
    loud_target: float = 0.85,
    max_gain: float = 2.2,
    min_peak: float = 0.02,
) -> bytes:
    """Gentle loudness guard for int16 LE PCM (boost soft / limit hot CosyVoice chunks).

    Avoids hard peak-normalize-to-target on every frame (that pumps consonants).
    Only adjusts clearly soft or clearly hot chunks.
    """
    if not pcm_bytes or len(pcm_bytes) < 2:
        return pcm_bytes
    if len(pcm_bytes) % 2:
        pcm_bytes = pcm_bytes[:-1]
    import array

    samples = array.array("h")
    samples.frombytes(pcm_bytes)
    if not samples:
        return pcm_bytes
    peak = max(abs(s) for s in samples)
    if peak < int(min_peak * 32767):
        return pcm_bytes
    peak_f = peak / 32767.0
    if peak_f < soft_peak:
        gain = min(max_gain, soft_target / peak_f)
    elif peak_f > loud_peak:
        gain = loud_target / peak_f
    else:
        return pcm_bytes
    for i, s in enumerate(samples):
        v = int(s * gain)
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        samples[i] = v
    return samples.tobytes()


def scale_word_timestamps(
    words: tuple[TimedWord, ...],
    *,
    pcm_duration_ms_value: int,
    offset_ms: int = 0,
) -> tuple[tuple[TimedWord, ...], str]:
    """
    Align late timestamps with actual PCM duration.
    Returns (scaled_words, status) where status is ok|scaled|degraded.
    """
    if not words:
        return (), "degraded"
    last_end = words[-1].end_ms
    if last_end <= 0:
        return (), "degraded"
    err = abs(last_end - pcm_duration_ms_value)
    if err <= 120:
        scaled = tuple(
            TimedWord(
                text=w.text,
                begin_ms=w.begin_ms + offset_ms,
                end_ms=w.end_ms + offset_ms,
                punctuation=w.punctuation,
            )
            for w in words
        )
        return scaled, "ok"
    # Always linear-stretch to PCM when off by more than 120ms so Adaptive
    # Interruption / HeardTextTracker stay aligned with real playback. Designed
    # v3.5 voices often trail ~0.5s; still usable after scale.
    factor = pcm_duration_ms_value / last_end
    scaled = tuple(
        TimedWord(
            text=w.text,
            begin_ms=int(w.begin_ms * factor) + offset_ms,
            end_ms=int(w.end_ms * factor) + offset_ms,
            punctuation=w.punctuation,
        )
        for w in words
    )
    if err <= 300:
        return scaled, "scaled"
    # Large mismatch: timestamps were scaled to PCM but quality is degraded.
    return scaled, "degraded"
