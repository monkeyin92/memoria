"""Per-round internal AIGC generation log.

One record is written when a device conversation round ends: content id,
start/end time, and the TTS provider/engine actually configured for that round.
Child utterances and assistant reply text are never accepted by this module.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

logger = logging.getLogger("memoria.aigc.generation")

AIGC_ROUND_EVENT: Final = "aigc_round"
_FORBIDDEN_RECORD_KEYS: Final = frozenset(
    {
        "text",
        "user_text",
        "assistant_text",
        "reply",
        "transcript",
        "utterance",
        "prompt",
        "content",
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AigcTtsDescriptor:
    """Vendor/engine facts safe to log for one round."""

    provider: str
    engine: str
    voice: str = ""
    fallback_provider: str = ""
    fallback_engine: str = ""
    fallback_voice: str = ""


@dataclass(slots=True)
class AigcRoundRecord:
    content_id: str
    started_at: str
    descriptor: AigcTtsDescriptor
    ended_at: str | None = None
    close_reason: str | None = None


class AigcRoundLogger:
    """Emit one JSON record per round to the internal log and optional JSONL file."""

    def __init__(
        self,
        descriptor: AigcTtsDescriptor,
        *,
        path: str = "",
        content_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._path = Path(path) if path.strip() else None
        self._content_id_factory = content_id_factory or (
            lambda: f"aigc-round-{uuid.uuid4().hex}"
        )
        self._clock = clock or _utc_now

    @classmethod
    def from_settings(cls, settings: object) -> AigcRoundLogger | None:
        if not bool(getattr(settings, "aigc_generation_log_enabled", True)):
            return None
        from services.agent.src.providers.tts_factory import tts_runtime_descriptor

        return cls(
            tts_runtime_descriptor(settings),
            path=str(getattr(settings, "aigc_generation_log_path", "")),
        )

    def start(self) -> AigcRoundRecord:
        return AigcRoundRecord(
            content_id=self._content_id_factory(),
            started_at=self._clock().isoformat(),
            descriptor=self._descriptor,
        )

    def finish(self, record: AigcRoundRecord, *, close_reason: str) -> dict[str, object]:
        if record.ended_at is not None:
            raise ValueError("AIGC round already finished")
        record.ended_at = self._clock().isoformat()
        record.close_reason = close_reason[:128]
        payload = self._payload(record)
        logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        if self._path is not None:
            self._append(payload)
        return payload

    def _append(self, payload: dict[str, object]) -> None:
        assert self._path is not None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            logger.warning(
                "AIGC round log file write failed path=%s", self._path, exc_info=True
            )

    def _payload(self, record: AigcRoundRecord) -> dict[str, object]:
        descriptor = record.descriptor
        payload: dict[str, object] = {
            "event": AIGC_ROUND_EVENT,
            "content_id": record.content_id,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "provider": descriptor.provider,
            "engine": descriptor.engine,
            "voice": descriptor.voice,
            "fallback_provider": descriptor.fallback_provider,
            "fallback_engine": descriptor.fallback_engine,
            "fallback_voice": descriptor.fallback_voice,
            "close_reason": record.close_reason,
        }
        if _FORBIDDEN_RECORD_KEYS.intersection(payload):
            raise ValueError("AIGC round log must not carry child or reply text")
        return payload


__all__ = [
    "AIGC_ROUND_EVENT",
    "AigcRoundLogger",
    "AigcRoundRecord",
    "AigcTtsDescriptor",
]
