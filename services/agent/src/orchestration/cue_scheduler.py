"""Bounded listener backchannel policy, isolated from assistant conversation history."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass

_SENSITIVE_HINTS = (
    "验证码",
    "手机号",
    "身份证",
    "银行卡",
    "密码",
    "转账",
    "金额",
    "报警",
    "自杀",
    "呼吸困难",
    "律师",
    "法院",
)
_LONG_NUMBER = re.compile(r"\d(?:[\s-]*\d){3,}")


@dataclass(frozen=True)
class ListenerCue:
    cue_id: str
    cue_epoch: int
    user_turn_id: int
    text: str


@dataclass
class CueScheduler:
    """Creates safe fixed cues; it never writes a ChatMessage or invokes an LLM."""

    enabled: bool = True
    min_speech_ms: int = 1_800
    pause_ms: int = 250
    cooldown_ms: int = 5_000
    max_per_turn: int = 2
    cues: tuple[str, ...] = ("嗯", "我在听", "你继续")
    _cue_epoch: int = 0
    _active_turn_id: int | None = None
    _turn_started_ns: int | None = None
    _last_cue_ns: int | None = None
    _count: int = 0
    _next_cue: int = 0

    def start_turn(self, *, user_turn_id: int, now_ns: int | None = None) -> None:
        if self._active_turn_id == user_turn_id:
            return
        self._cue_epoch += 1
        self._active_turn_id = user_turn_id
        self._turn_started_ns = now_ns if now_ns is not None else time.monotonic_ns()
        self._count = 0

    def observe_partial(
        self,
        text: str,
        *,
        now_ns: int | None = None,
        aec_healthy: bool = True,
        main_response_active: bool = False,
    ) -> ListenerCue | None:
        now = now_ns if now_ns is not None else time.monotonic_ns()
        if (
            not self.enabled
            or self._active_turn_id is None
            or self._turn_started_ns is None
            or not aec_healthy
            or main_response_active
            or self._count >= self.max_per_turn
            or self._unsafe(text)
        ):
            return None
        elapsed_ms = (now - self._turn_started_ns) // 1_000_000
        if elapsed_ms < self.min_speech_ms:
            return None
        if (
            self._last_cue_ns is not None
            and (now - self._last_cue_ns) // 1_000_000 < self.cooldown_ms
        ):
            return None

        cue = ListenerCue(
            cue_id=str(uuid.uuid4()),
            cue_epoch=self._cue_epoch,
            user_turn_id=self._active_turn_id,
            text=self.cues[self._next_cue % len(self.cues)],
        )
        self._next_cue += 1
        self._count += 1
        self._last_cue_ns = now
        return cue

    def cancel_turn(self) -> int:
        self._cue_epoch += 1
        self._active_turn_id = None
        self._turn_started_ns = None
        self._count = 0
        return self._cue_epoch

    def is_current(self, cue: ListenerCue) -> bool:
        return (
            self._active_turn_id == cue.user_turn_id
            and self._cue_epoch == cue.cue_epoch
        )

    @staticmethod
    def _unsafe(text: str) -> bool:
        compact = text.strip()
        return (
            not compact
            or _LONG_NUMBER.search(compact) is not None
            or any(hint in compact for hint in _SENSITIVE_HINTS)
        )
