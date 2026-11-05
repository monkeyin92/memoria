"""Post-playback input guard runtime mixin: the reply's echo and backchannels right after it ends.

``DuplexRuntimePostPlaybackGuardMixin`` is composed into ``DuplexRuntime``; the voice floor, the input guard and
the fixed-safety echo reference stay owned by the runtime dataclass and are only declared here for type checking.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from services.common.fixed_safety_scripts import fixed_safety_echo_reference

if TYPE_CHECKING:
    from services.agent.src.orchestration.interruption_guard import PlaybackInputGuard
    from services.agent.src.voice_floor import VoiceFloorState

POST_PLAYBACK_BACKCHANNEL_GUARD_MS = 800
POST_PLAYBACK_ECHO_GUARD_MS = 10_000


class DuplexRuntimePostPlaybackGuardMixin:
    if TYPE_CHECKING:
        input_guard: PlaybackInputGuard
        fixed_safety_heard: bool
        fixed_safety_echo_text: str
        _voice_floor: VoiceFloorState

    def note_fixed_safety_spoken(self, text: str) -> None:
        echo = fixed_safety_echo_reference(text)
        if echo is None:
            return
        self.fixed_safety_heard = True
        self.fixed_safety_echo_text = echo

    def post_playback_guard_reason(
        self, text: str, *, now_ns: int | None = None, echo_possible: bool = True
    ) -> str | None:
        """Why ``text`` committed just after a reply is not a turn, or None.

        ``echo_possible`` is False when the utterance was recorded only after the reply's echo tail ended:
        the reply cannot be heard back in it, so repeating the reply's words is the child speaking.
        """

        script = self.fixed_safety_echo_text
        if (
            echo_possible
            and script
            and self.input_guard.guarded_reason(
                text,
                duration_ms=POST_PLAYBACK_ECHO_GUARD_MS,
                assistant_text=script,
            )
            == "assistant_echo"
        ):
            return "assistant_echo"
        completed_ns = self._voice_floor.last_playback_completed_ns
        if completed_ns is None:
            return None
        elapsed_ms = ((now_ns or time.monotonic_ns()) - completed_ns) // 1_000_000
        if not 0 <= elapsed_ms <= POST_PLAYBACK_ECHO_GUARD_MS:
            return None
        reason = self.input_guard.guarded_reason(
            text,
            duration_ms=int(elapsed_ms),
            assistant_text=self._voice_floor.played_assistant_text if echo_possible else "",
        )
        if reason == "assistant_echo":
            return reason
        if elapsed_ms <= POST_PLAYBACK_BACKCHANNEL_GUARD_MS and (
            reason == "backchannel" or self.input_guard.enabled
        ):
            return reason
        return None
