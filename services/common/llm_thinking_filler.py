from __future__ import annotations

import enum
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
_UPLINK_SAMPLE_RATE: Final = 16_000


def speech_end_monotonic(
    *,
    received_at: float,
    event_sample: int,
    voiced_end_sample: int | None,
) -> float:
    """When the child's last voiced sample was spoken, on ``received_at``'s monotonic clock.

    A device reports VAD end at ``event_sample``, a hangover after ``voiced_end_sample``.
    Uplink latency is not subtracted, so the result is never earlier than the speech end.
    """

    if voiced_end_sample is None or voiced_end_sample >= event_sample:
        return received_at
    return received_at - (event_sample - voiced_end_sample) / _UPLINK_SAMPLE_RATE


class FillerState(enum.Enum):
    IDLE = "idle"
    ARMED = "armed"
    PLAYED = "played"
    REPLY_STARTED = "reply_started"


@dataclass(slots=True)
class LlmThinkingFiller:
    """Tracks only the latest armed turn; signals for older turns are ignored."""

    delay_s: float = LLM_THINKING_DELAY_S
    turn_id: int | None = None
    armed_at: float = 0.0
    state: FillerState = FillerState.IDLE

    def arm(self, *, turn_id: int, now: float) -> None:
        self.turn_id = turn_id
        self.armed_at = now
        self.state = FillerState.ARMED

    def note_reply_spoken(self, *, turn_id: int) -> None:
        if self._armed_for(turn_id):
            self.state = FillerState.REPLY_STARTED

    def mark_played(self, *, turn_id: int) -> None:
        if self._armed_for(turn_id):
            self.state = FillerState.PLAYED

    def seconds_until_due(self, *, turn_id: int, now: float) -> float | None:
        """Wait left before the filler may play; None once this turn never can."""

        if not self._armed_for(turn_id):
            return None
        return max(0.0, self.armed_at + self.delay_s - now)

    def should_play(self, *, turn_id: int, now: float) -> bool:
        return self.seconds_until_due(turn_id=turn_id, now=now) == 0.0

    def _armed_for(self, turn_id: int) -> bool:
        return self.turn_id == turn_id and self.state is FillerState.ARMED


__all__ = [
    "LLM_THINKING_AIGC",
    "LLM_THINKING_DELAY_S",
    "LLM_THINKING_LINE_ID",
    "LLM_THINKING_TEXT",
    "FillerState",
    "LlmThinkingFiller",
    "speech_end_monotonic",
]
