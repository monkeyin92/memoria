"""FunASR Realtime protocol parse/map (pure, no network)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from services.agent.src.contracts.events import TimedWord
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


@dataclass(frozen=True, slots=True)
class FunASRServerEvent:
    event: FunASREventType
    task_id: str
    sentence: FunASRSentence | None = None
    error_message: str | None = None
    raw: dict[str, Any] | None = None


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
        return FunASRServerEvent(
            event="task-failed",
            task_id=task_id,
            error_message=str(
                header.get("error_message")
                or header.get("message")
                or payload.get("message")
                or payload.get("error")
                or "task-failed"
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
    for w in words_raw:
        if not isinstance(w, dict):
            continue
        begin = int(w.get("begin_time") or w.get("begin_ms") or 0)
        end = int(w.get("end_time") or w.get("end_ms") or begin)
        text = str(w.get("text") or "")
        punct = str(w.get("punctuation") or "")
        words.append(TimedWord(text=text, begin_ms=begin, end_ms=end, punctuation=punct))

    end_time = sent.get("end_time")
    return FunASRSentence(
        sentence_id=int(sent.get("sentence_id") or 0),
        text=str(sent.get("text") or ""),
        begin_ms=int(sent.get("begin_time") or 0),
        end_ms=int(end_time) if end_time is not None else None,
        sentence_end=bool(sent.get("sentence_end")),
        heartbeat=bool(sent.get("heartbeat")),
        words=tuple(words),
    )


def words_to_seconds(words: tuple[TimedWord, ...]) -> list[tuple[str, float, float]]:
    """Convert ms timestamps to seconds for LiveKit TimedString."""
    out: list[tuple[str, float, float]] = []
    for w in words:
        text = w.text + (w.punctuation or "")
        out.append((text, w.begin_ms / 1000.0, w.end_ms / 1000.0))
    return out


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
