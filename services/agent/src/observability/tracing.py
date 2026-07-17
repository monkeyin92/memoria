"""Lightweight latency stage markers (ch.21)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class LatencyTrace:
    marks: dict[str, int] = field(default_factory=dict)

    def mark(self, name: str, mono_ns: int | None = None) -> None:
        self.marks[name] = mono_ns if mono_ns is not None else time.monotonic_ns()

    def delta_s(self, start: str, end: str) -> float | None:
        if start not in self.marks or end not in self.marks:
            return None
        return (self.marks[end] - self.marks[start]) / 1e9

    def derived(self) -> dict[str, float | None]:
        return {
            "endpointing_latency": self.delta_s("last_user_audio", "turn_committed"),
            "llm_ttft": self.delta_s("llm_request_started", "llm_first_content_token"),
            "phrase_wait": self.delta_s("llm_first_content_token", "first_phrase_ready"),
            "tts_ttfb": self.delta_s("tts_task_started", "tts_first_audio_received"),
            "speech_to_speech": self.delta_s("last_user_audio", "client_first_playback"),
            "barge_in_stop": self.delta_s("barge_in_confirmed", "playback_stopped"),
        }
