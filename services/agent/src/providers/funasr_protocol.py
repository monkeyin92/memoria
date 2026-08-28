"""FunASR Realtime protocol parse/map (pure, no network)."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from services.agent.src.contracts.events import TimedWord
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    ASRWordTiming,
)
from services.common.redaction import redact_pii

FunASREventType = Literal[
    "task-started",
    "result-generated",
    "task-finished",
    "task-failed",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class FunASRSentence:
    sentence_id: int
    text: str
    begin_ms: int
    end_ms: int | None
    sentence_end: bool
    heartbeat: bool
    words: tuple[TimedWord, ...]
    word_timing_valid: bool = True


def sentence_to_asr_result(
    sentence: FunASRSentence,
    *,
    task_epoch: int,
    sample_rate: int = 16_000,
    revision: int = 1,
    confidence: float | None = None,
    stream_epoch: int = 1,
    sample_offset: int = 0,
) -> ASRResult:
    """Map FunASR millisecond timestamps to a deterministic sample range."""

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if sample_offset < 0:
        raise ValueError("sample_offset must be non-negative")
    provider_begin_ms = max(0, sentence.begin_ms)
    start = sample_offset + round(provider_begin_ms * sample_rate / 1000)
    word_end = max((word.end_ms for word in sentence.words), default=sentence.begin_ms)
    provider_end = (
        sentence.end_ms if sentence.end_ms is not None else max(sentence.begin_ms, word_end)
    )
    provider_end_ms = max(provider_begin_ms, provider_end, 0)
    end = max(
        start + 1,
        sample_offset + round(provider_end_ms * sample_rate / 1000),
    )
    projected_words: list[ASRWordTiming] = []
    invalid_word = not sentence.word_timing_valid
    for word in sentence.words:
        try:
            projected_words.append(
                ASRWordTiming(
                    text=word.text + (word.punctuation or ""),
                    capture_start_sample=sample_offset + round(word.begin_ms * sample_rate / 1000),
                    capture_end_sample=sample_offset + round(word.end_ms * sample_rate / 1000),
                )
            )
        except ValueError:
            # Malformed provider timing is evidence failure, not an ASR
            # session failure. Preserve the transcript and fail closed later.
            invalid_word = True
    word_timings = tuple(projected_words) if not invalid_word else ()
    return ASRResult(
        task_epoch=task_epoch,
        sentence_id=str(sentence.sentence_id),
        revision=revision,
        capture_start_sample=start,
        capture_end_sample=end,
        text=sentence.text,
        is_final=sentence.sentence_end,
        confidence=confidence,
        provider_begin_ms=provider_begin_ms,
        provider_end_ms=provider_end_ms,
        stream_epoch=stream_epoch,
        word_timings=word_timings,
    )


@dataclass(frozen=True, slots=True)
class FunASRServerEvent:
    event: FunASREventType
    task_id: str
    sentence: FunASRSentence | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] | None = None


_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?i)\b(authorization|api[-_ ]?key|access[-_ ]?token|token|secret|password)"
    r"\s*[:=]\s*[^\s,;]+"
)


def sanitize_error_code(value: Any) -> str | None:
    """Return a bounded provider error code safe for logs and exceptions."""

    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    return sanitize_diagnostic_text(text, fallback="unknown", limit=96)


def sanitize_error_message(value: Any, *, fallback: str = "task-failed") -> str:
    """Bound and redact provider error text before it reaches diagnostics."""

    if value is None:
        text = ""
    elif isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    return sanitize_diagnostic_text(text, fallback=fallback, limit=400)


def sanitize_diagnostic_text(value: str, *, fallback: str, limit: int) -> str:
    text = value.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    text = "".join(char if char.isprintable() else " " for char in text)
    text = _DIAGNOSTIC_SECRET_RE.sub(r"\1=[REDACTED]", text)
    text = redact_pii(text)
    text = text[:limit].strip()
    return text or fallback


def _first_error_field(*mappings: dict[str, Any]) -> Any:
    for mapping in mappings:
        for key in ("error_code", "code"):
            if key in mapping and mapping[key] is not None:
                return mapping[key]
    return None


def _first_error_message(*mappings: dict[str, Any]) -> Any:
    for mapping in mappings:
        for key in ("error_message", "message", "error", "detail"):
            if key in mapping and mapping[key] is not None:
                return mapping[key]
    return None


def build_run_task(
    *,
    task_id: str | None = None,
    model: str = "fun-asr-realtime",
    sample_rate: int = 16000,
    language_hints: list[str] | None = None,
    semantic_punctuation_enabled: bool = False,
    max_sentence_silence_ms: int = 650,
    heartbeat: bool = True,
    context: list[dict[str, object]] | None = None,
    vocabulary_id: str | None = None,
    speech_noise_threshold: float | None = None,
) -> dict[str, Any]:
    tid = task_id or str(uuid.uuid4())
    parameters: dict[str, object] = {
        "format": "pcm",
        "sample_rate": sample_rate,
        "language_hints": language_hints or ["zh"],
        "semantic_punctuation_enabled": semantic_punctuation_enabled,
        "max_sentence_silence": max_sentence_silence_ms,
        "heartbeat": heartbeat,
    }
    if vocabulary_id:
        parameters["vocabulary_id"] = vocabulary_id
    if speech_noise_threshold is not None:
        parameters["speech_noise_threshold"] = speech_noise_threshold
    return {
        "header": {
            "action": "run-task",
            "task_id": tid,
            "streaming": "duplex",
        },
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": model,
            "parameters": parameters,
            "input": {"context": context} if context else {},
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


def build_continue_task_context(task_id: str, context: list[dict[str, object]]) -> dict[str, Any]:
    return {
        "header": {
            "action": "continue-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {
            "input": {
                "context": context,
            },
        },
    }


def parse_server_message(data: str | bytes | dict[str, Any]) -> FunASRServerEvent:
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    if isinstance(data, str):
        obj: dict[str, Any] = json.loads(data)
    else:
        obj = data

    header = obj.get("header") or {}
    event_name = str(header.get("event") or header.get("action") or "unknown")
    task_id = str(header.get("task_id") or "")

    if event_name == "task-started":
        return FunASRServerEvent(event="task-started", task_id=task_id, raw=obj)
    if event_name == "task-finished":
        return FunASRServerEvent(event="task-finished", task_id=task_id, raw=obj)
    if event_name == "task-failed":
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        output = payload.get("output") or {}
        if not isinstance(output, dict):
            output = {}
        return FunASRServerEvent(
            event="task-failed",
            task_id=task_id,
            error_code=sanitize_error_code(
                _first_error_field(header, payload, output)
            ),
            error_message=sanitize_error_message(
                _first_error_message(header, payload, output),
                fallback="task-failed",
            ),
            raw=obj,
        )
    if event_name in ("result-generated", "result"):
        sentence = _parse_sentence(obj)
        return FunASRServerEvent(
            event="result-generated",
            task_id=task_id,
            sentence=sentence,
            raw=obj,
        )
    return FunASRServerEvent(event="unknown", task_id=task_id, raw=obj)


def _parse_sentence(obj: dict[str, Any]) -> FunASRSentence | None:
    payload = obj.get("payload") or {}
    output = payload.get("output") or {}
    sent = output.get("sentence") or payload.get("sentence")
    if not isinstance(sent, dict):
        return None
    words_raw = sent.get("words") or []
    words: list[TimedWord] = []
    word_timing_valid = True
    for w in words_raw:
        if not isinstance(w, dict):
            word_timing_valid = False
            continue
        try:
            begin = int(w.get("begin_time") or w.get("begin_ms") or 0)
            end = int(w.get("end_time") or w.get("end_ms") or begin)
            text = str(w.get("text") or "")
            punct = str(w.get("punctuation") or "")
            words.append(TimedWord(text=text, begin_ms=begin, end_ms=end, punctuation=punct))
        except (TypeError, ValueError):
            word_timing_valid = False

    end_time = sent.get("end_time")
    return FunASRSentence(
        sentence_id=int(sent.get("sentence_id") or 0),
        text=str(sent.get("text") or ""),
        begin_ms=int(sent.get("begin_time") or 0),
        end_ms=int(end_time) if end_time is not None else None,
        sentence_end=bool(sent.get("sentence_end")),
        heartbeat=bool(sent.get("heartbeat")),
        words=tuple(words),
        word_timing_valid=word_timing_valid,
    )


def words_to_seconds(words: tuple[TimedWord, ...]) -> list[tuple[str, float, float]]:
    """Convert ms timestamps to seconds for LiveKit TimedString."""
    out: list[tuple[str, float, float]] = []
    for w in words:
        text = w.text + (w.punctuation or "")
        out.append((text, w.begin_ms / 1000.0, w.end_ms / 1000.0))
    return out


def result_trace_metrics(
    sentence: FunASRSentence,
    *,
    task_epoch: int,
) -> dict[str, int]:
    """Return bounded numeric timing facts; transcript and provider IDs stay private."""

    end_ms = sentence.end_ms if sentence.end_ms is not None else sentence.begin_ms
    return {
        "task_epoch": max(1, task_epoch),
        "sentence_id": max(0, sentence.sentence_id),
        "begin_ms": max(0, sentence.begin_ms),
        "end_ms": max(0, end_ms),
        "duration_ms": max(0, end_ms - sentence.begin_ms),
        "text_len": len((sentence.text or "").strip()),
    }


def conversation_item_to_funasr_context(item: dict[str, Any]) -> dict[str, object] | None:
    """Map conversation item to FunASR context entry with redaction/truncation."""
    role = str(item.get("role") or "")
    text = str(item.get("text") or item.get("content") or "")
    if role not in ("user", "assistant") or not text.strip():
        return None
    text = redact_pii(text)[:400]
    content_type = "input_text" if role == "user" else "text"
    return {
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }


def timestamps_monotonic(words: tuple[TimedWord, ...]) -> bool:
    prev = -1
    for w in words:
        if w.begin_ms < prev or w.end_ms < w.begin_ms:
            return False
        prev = w.begin_ms
    return True
