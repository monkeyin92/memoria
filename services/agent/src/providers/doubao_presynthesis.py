"""A Doubao session opened before its turn is committed, kept until the turn says it is its own.

2026-11-01 device soaks: the first phrase reaches Doubao only after the commit, and its first PCM comes
0.3-0.45 s later.  When the speculative reply's first phrase is known while the commit still runs,
``DoubaoTTS.presynthesize`` opens the session that turn is predicted to open, sends the phrase, and the audio
waits in the stream.  The committed turn's speech stream takes that session instead of opening one only when
the speculative reply was adopted and the turn, the session config and the first phrase are exactly what it
would send itself.  Anything else cancels the session; a cancelled session closes its connection, so none of its
audio can reach this turn or a later one.

Each pre-synthesis ends in exactly one log line, ``speculative speech ... outcome=...``, which never names
the child's words or the reply.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from services.agent.src.contracts.ids import GenerationFence

if TYPE_CHECKING:
    from services.agent.src.providers.doubao_tts import DoubaoSynthesizeStream, DoubaoTTSConfig

logger = logging.getLogger(__name__)

Mismatch = Literal[
    "not_adopted",  # the speculative reply was not adopted, or not yet
    "other_fence",  # the provider would now open a session for another generation
    "other_text",  # the turn's first phrase differs
    "other_config",  # voice, resource, rate, pitch, volume or style context differ
    "failed",  # the session ended on its own before the turn asked for it
    "stale",  # open so long that its idle limit could end it before the turn's next phrase is heard
]


class SessionReports(Protocol):
    """Where a Doubao session reports its audio stages and its subtitle alignment."""

    def trace(
        self, name: str, status: str = "ok", detail: dict[str, Any] | None = None
    ) -> None: ...

    def _report_alignment(
        self, fence: GenerationFence | None, utterance_id: str, status: str
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SpeechInputs:
    """Everything that decides a Doubao session's opening audio: its turn, its session config, its first phrase."""

    fence: GenerationFence | None
    config: DoubaoTTSConfig
    text: str


class HeldReports:
    """A pre-synthesized session's reports, held until its turn takes the session and dropped otherwise.

    The reports name that turn's audio stages (``doubao_first_pcm`` is the turn's first-audio mark) and start
    its subtitle alignment, so a turn that never plays the session must not see them.
    """

    def __init__(self, reports: SessionReports) -> None:
        self._reports = reports
        self._held: list[Callable[[], None]] | None = []
        self._dropped = False
        self.first_pcm_at: float | None = None

    def trace(self, name: str, status: str = "ok", detail: dict[str, Any] | None = None) -> None:
        if name == "doubao_first_pcm" and self.first_pcm_at is None:
            self.first_pcm_at = time.monotonic()
        self._report(lambda: self._reports.trace(name, status, detail))

    def _report_alignment(
        self, fence: GenerationFence | None, utterance_id: str, status: str
    ) -> None:
        self._report(lambda: self._reports._report_alignment(fence, utterance_id, status))

    def release(self) -> None:
        held, self._held = self._held, None
        for report in held or ():
            report()

    def drop(self) -> None:
        self._held = None
        self._dropped = True

    def _report(self, report: Callable[[], None]) -> None:
        if self._dropped:
            return
        if self._held is not None:
            self._held.append(report)
            return
        report()


class Presynthesis:
    """One session opened ahead of its turn: what it was opened with, whether its reply was adopted, how it ended.

    Only ``take`` hands the stream out, once, and only to the exact turn it was opened for after ``adopt``;
    ``discard`` cancels it.  Either ends it, and an ended pre-synthesis never changes again.
    """

    def __init__(
        self, inputs: SpeechInputs, stream: DoubaoSynthesizeStream, reports: HeldReports
    ) -> None:
        self.inputs = inputs
        self._stream = stream
        self._reports = reports
        self._started_at = time.monotonic()
        self.adopted = False
        self.ended = False

    def adopt(self) -> None:
        """The committed turn adopted the speculative reply whose first phrase this session speaks."""

        if not self.ended:
            self.adopted = True

    def take(self, wanted: SpeechInputs) -> DoubaoSynthesizeStream | None:
        """The session, when ``wanted`` is exactly what it was opened with; otherwise it is discarded."""

        if self.ended:
            return None
        mismatch = self._mismatch(wanted)
        if mismatch is not None:
            self.discard(mismatch)
            return None
        self._end("used")
        self._reports.release()
        return self._stream

    def discard(self, reason: str) -> None:
        if self.ended:
            return
        self._end(reason)
        self._reports.drop()
        self._stream.cancel()

    def _mismatch(self, wanted: SpeechInputs) -> Mismatch | None:
        if not self.adopted:
            return "not_adopted"
        if wanted.fence != self.inputs.fence:
            return "other_fence"
        if wanted.text != self.inputs.text:
            return "other_text"
        if wanted.config != self.inputs.config:
            return "other_config"
        if self._stream.done:
            return "failed"
        # The session ends itself after ``total_timeout_s`` without audio, counted from its last audio; half of
        # it leaves the turn's next phrase that much time to arrive.
        if time.monotonic() - self._started_at > self.inputs.config.total_timeout_s / 2:
            return "stale"
        return None

    def _end(self, outcome: str) -> None:
        self.ended = True
        now = time.monotonic()
        first_pcm_at = self._reports.first_pcm_at
        fence = self.inputs.fence
        logger.info(
            "speculative speech session_id=%s turn_id=%s outcome=%s started_ms=%s pcm_lead_ms=%s",
            fence.session_id if fence is not None else "-",
            fence.turn_id if fence is not None else "-",
            outcome,
            int((now - self._started_at) * 1_000),
            int((now - first_pcm_at) * 1_000) if first_pcm_at is not None else "-",
        )
