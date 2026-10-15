from __future__ import annotations

from dataclasses import dataclass
from typing import Final

LLM_THINKING_LINE_ID: Final = "llm_thinking"
LLM_THINKING_TEXT: Final = "嗯，我想一想。"
LLM_THINKING_DELAY_S: Final = 3.0
LLM_THINKING_AIGC: Final = {
    "content_id": LLM_THINKING_LINE_ID,
    "service_provider": "Memoria",
    "ai_generated": True,
}


@dataclass(slots=True)
class LlmThinkingFiller:
    delay_s: float = LLM_THINKING_DELAY_S
    _armed_turn_id: int | None = None
    _armed_mono: float | None = None
    _played_turn_id: int | None = None
    _reply_spoken_turn_id: int | None = None

    def arm(self, *, turn_id: int, now: float) -> None:
        self._armed_turn_id = turn_id
        self._armed_mono = now
        if self._played_turn_id != turn_id:
            self._played_turn_id = None
        if self._reply_spoken_turn_id != turn_id:
            self._reply_spoken_turn_id = None

    def note_reply_spoken(self, *, turn_id: int) -> None:
        self._reply_spoken_turn_id = turn_id

    def should_play(self, *, turn_id: int, now: float) -> bool:
        if self._armed_turn_id != turn_id or self._armed_mono is None:
            return False
        if self._played_turn_id == turn_id:
            return False
        if self._reply_spoken_turn_id == turn_id:
            return False
        return (now - self._armed_mono) >= self.delay_s

    def mark_played(self, *, turn_id: int) -> None:
        self._played_turn_id = turn_id


__all__ = [
    "LLM_THINKING_AIGC",
    "LLM_THINKING_DELAY_S",
    "LLM_THINKING_LINE_ID",
    "LLM_THINKING_TEXT",
    "LlmThinkingFiller",
]
