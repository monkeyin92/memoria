"""The first phrase of a speculative reply, sent to the TTS while its turn's commit still runs.

2026-11-01 device soaks: on an adopted turn the speech segment, from the first phrase to Doubao's first PCM,
is 0.3-0.45 s, and it starts only after the commit.  On the turns where the speculative reply's first phrase
is complete before the commit is, ``SpeechHeadStart`` predicts that phrase and the speech plan the commit
will choose, and asks the TTS to pre-synthesize it (``DoubaoTTS.presynthesize``).  The speech stream of the
committed turn decides whether it uses that session, so a wrong prediction costs only the head start.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.llm_types import chunk_text
from services.agent.src.orchestration.assistant_emotion import AssistantEmotion
from services.agent.src.orchestration.context_manager import SpeakerScope
from services.agent.src.orchestration.phrase_segmenter import PhraseSegment, PhraseSegmenter
from services.agent.src.runtime_emotion import plan_with_assistant_emotion
from services.common.labelled_lines import labelled_catalogue

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.src.speculative_reply import SpeculationKey

logger = logging.getLogger(__name__)


class PendingSpeech(Protocol):
    """A pre-synthesis as the TTS hands it back: the turn may adopt it, or it is discarded."""

    def adopt(self) -> None: ...

    def discard(self, reason: str) -> None: ...


class FirstPhrase:
    """The first text the committed turn's speech stream will receive, for one model stream.

    The reply pipeline cuts the model's tokens into phrases and the media adapter cuts each of those again; this
    runs both cuts on the same tokens.  Both segmenters also cut on elapsed time, which a buffer replayed at the
    commit never reaches, so both run on a fixed clock here and cut on punctuation and length alone.
    """

    def __init__(self, fence: GenerationFence) -> None:
        self._reply = PhraseSegmenter(fence=fence)
        self._adapter = PhraseSegmenter(fence=fence)

    @property
    def emotion(self) -> AssistantEmotion | None:
        """The emotion the reply declared ahead of its words; it shapes the turn's speech plan."""

        return self._reply.assistant_emotion

    def push(self, text: str) -> str | None:
        return self._spoken(self._reply.push_token(text, now_ns=0))

    def end(self) -> str | None:
        """The first phrase once the model stream is over, when no punctuation cut one before."""

        phrase = self._spoken(self._reply.flush(end_of_stream=True))
        if phrase is not None:
            return phrase
        return next((segment.text for segment in self._adapter.flush(end_of_stream=True)), None)

    def _spoken(self, segments: list[PhraseSegment]) -> str | None:
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            for phrase in self._adapter.push_token(text, now_ns=0):
                return phrase.text
        return None


class SpeechHeadStart:
    """Watches one speculative reply's stream for its first phrase and starts its pre-synthesis once.

    ``adopt`` and ``discard`` follow the speculative reply: an adopted reply lets the committed turn take the
    session, and anything else cancels it.  Neither starts anything, and nothing here may break the stream.
    """

    def __init__(
        self,
        fence: GenerationFence,
        start: Callable[[str, AssistantEmotion | None], PendingSpeech | None],
    ) -> None:
        self._first = FirstPhrase(fence)
        self._start = start
        self._watching = True
        self._pending: PendingSpeech | None = None

    def feed(self, chunk: object) -> None:
        if self._watching:
            self._begin(lambda: self._first.push(chunk_text(chunk)))

    def end(self) -> None:
        if self._watching:
            self._begin(self._first.end)

    def adopt(self) -> None:
        self._watching = False
        if self._pending is not None:
            self._pending.adopt()

    def discard(self, reason: str) -> None:
        self._watching = False
        if self._pending is not None:
            self._pending.discard(reason)

    def _begin(self, phrase_now: Callable[[], str | None]) -> None:
        try:
            phrase = phrase_now()
            if phrase is None:
                return
            self._watching = False
            self._pending = self._start(phrase, self._first.emotion)
        except Exception:
            self._watching = False
            logger.warning("speculative speech not started", exc_info=True)


def speech_head_start(
    runtime: DuplexRuntime, key: SpeculationKey, *, speaker_class: str
) -> SpeechHeadStart | None:
    """The head start for ``key``'s speculative reply, or None when the TTS cannot pre-synthesize.

    The phrase goes out with the plan the commit is predicted to choose, ``_apply_speech_plan`` in the runtime:
    the previewed speech plan, the emotion the reply declares, and the heard context with the sentence added.
    Once the commit has begun choosing them the prediction stops, and so does the head start.
    """

    presynthesize = getattr(runtime.tts, "presynthesize", None)
    if not callable(presynthesize):
        return None
    scope: SpeakerScope = "owner" if speaker_class == "owner" else "public"

    def start(phrase: str, emotion: AssistantEmotion | None) -> Any:
        committed = runtime.fence.bump_turn() != key.fence
        if committed or labelled_catalogue().prefix_candidate(phrase):
            return None
        plan = plan_with_assistant_emotion(
            runtime.preview_speech_plan(key.text, turn_id=key.fence.turn_id), emotion
        )
        references = runtime.orchestrator.context.after_user(
            key.text, speaker_scope=scope
        ).tts_reference_context(current_user_final=key.text, speaker_scope=scope)
        return presynthesize(
            key.fence,
            phrase,
            rate=plan.rate,
            instruction=plan.tts_instruction,
            pitch=plan.pitch,
            reference_contexts=references,
        )

    return SpeechHeadStart(key.fence, start)
