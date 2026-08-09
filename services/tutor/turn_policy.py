"""Deterministic, fence-aware tutor turn progression."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Literal

from services.tutor.domain import TutorFocus

TutorUtteranceIntent = Literal[
    "chat",
    "request_hint",
    "request_repeat",
    "pace_control",
    "give_up",
    "interrupt_then_chat",
    "resume",
]


@dataclass(frozen=True, slots=True)
class TutorTurnDirective:
    stuck_count: int
    minimal_hint_allowed: bool
    instruction: str | None
    reason: str


@dataclass(slots=True)
class _SessionState:
    focus: TutorFocus
    last_turn_id: int = -1
    stuck_count: int = 0
    decisions: OrderedDict[int, TutorTurnDirective] = field(default_factory=OrderedDict)


class TutorTurnPolicy:
    """Count only Router-owned tutor intents, once per committed turn."""

    def __init__(self, *, max_sessions: int = 512, max_turns_per_session: int = 16) -> None:
        if max_sessions < 1 or max_turns_per_session < 1:
            raise ValueError("tutor turn policy bounds must be positive")
        self._max_sessions = max_sessions
        self._max_turns = max_turns_per_session
        self._sessions: OrderedDict[str, _SessionState] = OrderedDict()
        self._lock = Lock()

    def observe(
        self,
        *,
        session_id: str,
        turn_id: int,
        focus: TutorFocus,
        intent: TutorUtteranceIntent,
    ) -> TutorTurnDirective:
        if not session_id.strip() or turn_id < 0:
            raise ValueError("tutor turn fence is invalid")
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None or state.focus != focus:
                state = _SessionState(focus=focus)
                self._sessions[session_id] = state
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
            existing = state.decisions.get(turn_id)
            if existing is not None:
                return existing
            if turn_id <= state.last_turn_id:
                return TutorTurnDirective(
                    stuck_count=state.stuck_count,
                    minimal_hint_allowed=False,
                    instruction="导师话轮状态不确定；本轮不要给答案或具体提示，只请学生重述思路。",
                    reason="stale_tutor_turn_fail_closed",
                )
            directive = self._advance(state, intent)
            state.last_turn_id = turn_id
            state.decisions[turn_id] = directive
            while len(state.decisions) > self._max_turns:
                state.decisions.popitem(last=False)
            return directive

    @staticmethod
    def _advance(
        state: _SessionState,
        intent: TutorUtteranceIntent,
    ) -> TutorTurnDirective:
        if intent == "request_hint":
            state.stuck_count = min(2, state.stuck_count + 1)
            if state.stuck_count == 1:
                return TutorTurnDirective(
                    stuck_count=1,
                    minimal_hint_allowed=False,
                    instruction=(
                        "学生这是连续卡住的第一次。本轮不要给具体提示或答案；"
                        "换一个更短的问题，请学生说出已知条件或先尝试一步。"
                    ),
                    reason="first_stuck_reframe",
                )
            return TutorTurnDirective(
                stuck_count=2,
                minimal_hint_allowed=True,
                instruction=(
                    "学生已连续两次明确卡住。本轮只允许给一个足以继续思考的最小提示；"
                    "不得给最终答案、完整解法或可直接提交的内容。"
                ),
                reason="second_stuck_minimal_hint",
            )
        if intent not in {"request_repeat", "pace_control"}:
            state.stuck_count = 0
        instruction = {
            "request_repeat": "只用更短、更具体的一句话重讲上一步，不提前给答案。",
            "pace_control": "放慢表达，每轮只说一个步骤并等待学生确认。",
            "give_up": "先承接挫败感，再把任务降低一步或建议短暂休息，不施压。",
        }.get(intent)
        return TutorTurnDirective(
            stuck_count=state.stuck_count,
            minimal_hint_allowed=False,
            instruction=instruction,
            reason=(f"tutor_{intent}" if instruction is not None else "tutor_progress_reset"),
        )


__all__ = ["TutorTurnDirective", "TutorTurnPolicy", "TutorUtteranceIntent"]
