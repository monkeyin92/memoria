"""Duplex orchestrator: atomic cancel, fence, heard-text, pipeline coordination."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import (
    CancellationContext,
    GenerationFence,
    new_session_id,
)
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.context_manager import (
    ChatMessage,
    ContextManager,
    SpeakerScope,
)
from services.agent.src.orchestration.generation_fence import FenceGate
from services.agent.src.orchestration.handlers import (
    LanguageModelHandler,
    LanguageModelRequest,
    SpeechSynthesisHandler,
    SpeechSynthesisRequest,
)
from services.agent.src.orchestration.heard_text_tracker import HeardTextTracker
from services.agent.src.orchestration.interruption_guard import (
    ChineseInterruptionGuard,
    InterruptDecision,
)
from services.agent.src.orchestration.phrase_segmenter import PhraseSegmenter
from services.agent.src.orchestration.state_machine import (
    ConversationState,
    DuplexStateMachine,
    TransitionEvent,
)
from services.agent.src.orchestration.task_manager import TaskManager
from services.agent.src.prompts import VOICE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


async def cancel_and_wait(task: asyncio.Task[Any] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@dataclass
class PlaybackController:
    """In-process stand-in for LiveKit speech_handle stop/flush."""

    playing: bool = False
    started_mono_ns: int | None = None
    stopped_mono_ns: int | None = None
    pcm_played: bytearray = field(default_factory=bytearray)
    stop_latency_ns: int = 5_000_000  # 5ms simulated stop

    async def start(self) -> None:
        self.playing = True
        self.started_mono_ns = time.monotonic_ns()
        self.stopped_mono_ns = None

    async def stop_and_flush(self) -> None:
        if self.playing:
            await asyncio.sleep(self.stop_latency_ns / 1e9)
        self.playing = False
        self.stopped_mono_ns = time.monotonic_ns()

    def push_pcm(self, data: bytes) -> None:
        if self.playing:
            self.pcm_played.extend(data)


@dataclass
class TTSPoolHandle:
    """Minimal provider-neutral interface used to cancel TTS on interrupt."""

    discarded: list[GenerationFence] = field(default_factory=list)

    async def discard_active_connection(self, fence: GenerationFence) -> None:
        self.discarded.append(fence)


@dataclass
class Orchestrator:
    session_id: str = field(default_factory=new_session_id)
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    state_machine: DuplexStateMachine | None = None
    fence_gate: FenceGate | None = None
    heard_tracker: HeardTextTracker = field(default_factory=HeardTextTracker)
    segmenter: PhraseSegmenter | None = None
    context: ContextManager = field(
        default_factory=lambda: ContextManager(system_prompt=VOICE_SYSTEM_PROMPT)
    )
    task_manager: TaskManager = field(default_factory=TaskManager)
    interruption_guard: ChineseInterruptionGuard = field(default_factory=ChineseInterruptionGuard)
    playback: PlaybackController = field(default_factory=PlaybackController)
    tts_pool: TTSPoolHandle = field(default_factory=TTSPoolHandle)
    mic_open: bool = True  # MUST remain true while assistant speaks
    vad_active: bool = True
    asr_active: bool = True
    _state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _active_llm_task: asyncio.Task[Any] | None = None
    _active_tts_task: asyncio.Task[Any] | None = None
    _tts_cancel: asyncio.Event = field(default_factory=asyncio.Event)
    stale_audio_outputs: int = 0
    published_audio_generations: list[int] = field(default_factory=list)
    _pending_interrupted_from: GenerationFence | None = None
    _pending_interrupted_to: GenerationFence | None = None
    _pending_interrupted_message: ChatMessage | None = None
    _speaker_scope_by_turn: dict[int, SpeakerScope] = field(default_factory=dict)

    def __post_init__(self) -> None:
        fence = GenerationFence(
            session_id=self.session_id,
            turn_id=0,
            generation_id=0,
            tool_epoch=0,
        )
        if self.state_machine is None:
            self.state_machine = DuplexStateMachine(
                session_id=self.session_id,
                fence=fence,
                metrics=self.metrics,
            )
        if self.fence_gate is None:
            self.fence_gate = FenceGate(current=fence, metrics=self.metrics)
        if self.segmenter is None:
            self.segmenter = PhraseSegmenter(fence=fence)

    @property
    def state(self) -> ConversationState:
        assert self.state_machine is not None
        return self.state_machine.state

    @property
    def fence(self) -> GenerationFence:
        assert self.state_machine is not None
        return self.state_machine.fence

    def speaker_scope_for_fence(self, fence: GenerationFence) -> SpeakerScope:
        """Resolve scope from the originating turn, never from the latest speaker."""
        return self._speaker_scope_by_turn.get(fence.turn_id, "public")

    async def ready(self) -> None:
        assert self.state_machine is not None
        async with self._state_lock:
            self.state_machine.apply(TransitionEvent.PREWARM_OK)

    async def on_vad_start(self) -> None:
        assert self.state_machine is not None
        async with self._state_lock:
            if self.state_machine.can_transition(TransitionEvent.VAD_START):
                self.state_machine.apply(TransitionEvent.VAD_START)
            elif self.state is ConversationState.THINKING:
                self.state_machine.apply(TransitionEvent.USER_SPEAKS_DURING_THINK)
            elif self.state is ConversationState.SPEAKING:
                self.state_machine.apply(TransitionEvent.USER_VOICE_WHILE_SPEAKING)

    async def commit_turn(
        self,
        user_text: str,
        *,
        speaker_scope: SpeakerScope = "public",
    ) -> GenerationFence:
        assert self.state_machine is not None
        assert self.fence_gate is not None
        assert self.segmenter is not None
        async with self._state_lock:
            # Normalize barge-in / interrupt paths into EOT_PENDING before TURN_END.
            # LiveKit may complete a user turn while we are still in
            # INTERRUPTION_PENDING (energy seen, not yet REAL_INTERRUPT).
            if self.state is ConversationState.INTERRUPTION_PENDING:
                if self.state_machine.can_transition(TransitionEvent.REAL_INTERRUPT):
                    self.state_machine.apply(
                        TransitionEvent.REAL_INTERRUPT,
                        cause="commit_turn_from_interruption_pending",
                    )
            elif self.state is ConversationState.SPEAKING:
                if self.state_machine.can_transition(TransitionEvent.USER_VOICE_WHILE_SPEAKING):
                    self.state_machine.apply(TransitionEvent.USER_VOICE_WHILE_SPEAKING)
                if self.state_machine.can_transition(TransitionEvent.REAL_INTERRUPT):
                    self.state_machine.apply(
                        TransitionEvent.REAL_INTERRUPT,
                        cause="commit_turn_from_speaking",
                    )
            elif self.state is ConversationState.THINKING:
                if self.state_machine.can_transition(TransitionEvent.USER_SPEAKS_DURING_THINK):
                    self.state_machine.apply(
                        TransitionEvent.USER_SPEAKS_DURING_THINK,
                        cause="commit_turn_from_thinking",
                    )
            elif self.state is ConversationState.LISTENING:
                if self.state_machine.can_transition(TransitionEvent.VAD_START):
                    self.state_machine.apply(TransitionEvent.VAD_START)

            if self.state is ConversationState.USER_SPEAKING:
                self.state_machine.apply(TransitionEvent.VAD_PAUSE_INCOMPLETE)

            if not self.state_machine.can_transition(TransitionEvent.TURN_END):
                # Last-resort recovery so a stuck session never blackholes replies.
                logger.warning(
                    "commit_turn forcing EOT_PENDING from state=%s",
                    self.state.value,
                )
                self.state_machine.state = ConversationState.EOT_PENDING

            new_fence = self.fence.bump_turn()
            self.state_machine.apply(TransitionEvent.TURN_END, new_fence=new_fence)
            self.fence_gate.update(new_fence)
            self.segmenter.reset(new_fence)
            self.heard_tracker.reset()
            self.context.add_user(user_text, speaker_scope=speaker_scope)
            self._speaker_scope_by_turn[new_fence.turn_id] = speaker_scope
            while len(self._speaker_scope_by_turn) > self.context.max_turns:
                self._speaker_scope_by_turn.pop(next(iter(self._speaker_scope_by_turn)))
            self._tts_cancel = asyncio.Event()
            return new_fence

    async def dismiss_pending_interruption(self, *, cause: str = "false_barge") -> bool:
        """Return INTERRUPTION_PENDING → SPEAKING when barge-in is not the owner."""
        assert self.state_machine is not None
        async with self._state_lock:
            if self.state is not ConversationState.INTERRUPTION_PENDING:
                return False
            if self.state_machine.can_transition(TransitionEvent.BACKCHANNEL_OR_NOISE):
                self.state_machine.apply(
                    TransitionEvent.BACKCHANNEL_OR_NOISE,
                    cause=cause,
                )
                return True
            return False

    def cancellation_context(
        self,
        fence: GenerationFence | None = None,
    ) -> CancellationContext:
        return CancellationContext.capture(fence or self.fence)

    async def accept_authoritative_fence(
        self,
        fence: GenerationFence,
        *,
        cause: str = "media_generation_control",
    ) -> bool:
        """Install a monotonic fence received from the media boundary.

        Media Edge is allowed to publish the cancellation generation. The
        Voice Core consumes that exact fence instead of deriving a local
        ``generation_id + 1``. Existing provider tasks are cancelled before
        the new fence becomes visible; stale callbacks are then rejected by
        ``FenceGate``.
        """

        assert self.state_machine is not None
        assert self.fence_gate is not None
        async with self._state_lock:
            current = self.fence
            if fence.session_id != self.session_id:
                return False
            if (
                fence.turn_id < current.turn_id
                or (
                    fence.turn_id == current.turn_id
                    and fence.generation_id < current.generation_id
                )
                or (
                    fence.turn_id == current.turn_id
                    and fence.generation_id == current.generation_id
                    and fence.tool_epoch < current.tool_epoch
                )
            ):
                return False
            if fence.matches(current):
                return True

            # Cancellation is a correctness fence, not a queue-clearing hint.
            self._tts_cancel.set()
            for task in (self._active_llm_task, self._active_tts_task):
                if task is not None and not task.done():
                    task.cancel()
            for record in self.task_manager.tasks.values():
                if record.finished or not record.fence.matches(current):
                    continue
                if record.cancellable:
                    record.cancel_event.set()
                    if not record.task.done():
                        record.task.cancel()

            self.state_machine.fence = fence
            self.fence_gate.update(fence)
            if self.segmenter is not None:
                self.segmenter.reset(fence)
            if self.state in {
                ConversationState.THINKING,
                ConversationState.SPEAKING,
                ConversationState.INTERRUPTION_PENDING,
                ConversationState.TOOL_WAITING,
            } and self.state_machine.can_transition(TransitionEvent.STOP_RESPONSE):
                self.state_machine.apply(
                    TransitionEvent.STOP_RESPONSE,
                    cause=cause,
                    new_fence=fence,
                )
            return True

    @staticmethod
    def _generation_fence(
        cancellation: GenerationFence | CancellationContext,
    ) -> GenerationFence:
        return cancellation.fence if isinstance(cancellation, CancellationContext) else cancellation

    def gate_llm_token(
        self,
        cancellation: GenerationFence | CancellationContext,
        token: str,
    ) -> str | None:
        assert self.fence_gate is not None
        return self.fence_gate.gate(
            self._generation_fence(cancellation),
            token,
            source="llm",
        )

    def gate_tts_audio(
        self,
        cancellation: GenerationFence | CancellationContext,
        pcm: bytes,
    ) -> bytes | None:
        assert self.fence_gate is not None
        fence = self._generation_fence(cancellation)
        out = self.fence_gate.gate(fence, pcm, source="tts")
        if out is None:
            self.stale_audio_outputs += 1
            return None
        self.published_audio_generations.append(fence.generation_id)
        return out

    def gate_tool_result(
        self,
        cancellation: GenerationFence | CancellationContext,
        payload: Any,
    ) -> Any | None:
        assert self.fence_gate is not None
        # Full fence including tool_epoch
        return self.fence_gate.gate(
            self._generation_fence(cancellation),
            payload,
            source="tool",
        )

    def set_active_llm_task(self, task: asyncio.Task[Any] | None) -> None:
        """Register the in-flight LLM generation task for atomic interrupt cancel."""
        self._active_llm_task = task

    def set_active_tts_task(self, task: asyncio.Task[Any] | None) -> None:
        """Register the in-flight TTS synthesis task for atomic interrupt cancel."""
        self._active_tts_task = task

    def clear_active_llm_task(self, task: asyncio.Task[Any] | None = None) -> None:
        if task is None or self._active_llm_task is task:
            self._active_llm_task = None

    def clear_active_tts_task(self, task: asyncio.Task[Any] | None = None) -> None:
        if task is None or self._active_tts_task is task:
            self._active_tts_task = None

    @property
    def active_llm_task(self) -> asyncio.Task[Any] | None:
        return self._active_llm_task

    @property
    def active_tts_task(self) -> asyncio.Task[Any] | None:
        return self._active_tts_task

    def tts_cancel_event(self) -> asyncio.Event:
        return self._tts_cancel

    async def begin_speaking(
        self, words: list[TimedWord] | tuple[TimedWord, ...], full_text: str
    ) -> None:
        assert self.state_machine is not None
        async with self._state_lock:
            if self.state_machine.can_transition(TransitionEvent.FIRST_PHRASE_READY):
                self.state_machine.apply(TransitionEvent.FIRST_PHRASE_READY)
        self.heard_tracker.set_full_text(full_text)
        self.heard_tracker.add_words(words)
        await self.playback.start()
        if self.playback.started_mono_ns is not None:
            self.heard_tracker.mark_playback_started(self.playback.started_mono_ns)
        # Invariant: mic/VAD/ASR stay open while speaking
        assert self.mic_open and self.vad_active and self.asr_active

    async def finish_speaking(self, *, tools_active: bool = False) -> None:
        await self.finish_livekit_playback(tools_active=tools_active)

    async def finish_livekit_playback(
        self,
        *,
        tools_active: bool = False,
        playback_position_s: float | None = None,
        synchronized_transcript: str | None = None,
        reply_fence: GenerationFence | None = None,
    ) -> str:
        """Commit a completed LiveKit playout, never TTS production completion."""
        assert self.state_machine is not None
        committed_fence = reply_fence or self.fence
        if playback_position_s is None:
            await self.playback.stop_and_flush()
            stopped_ns = self.playback.stopped_mono_ns
        else:
            self.playback.playing = False
            started_ns = self.heard_tracker.playback_started_mono_ns
            if started_ns is None:
                started_ns = time.monotonic_ns() - int(max(0.0, playback_position_s) * 1e9)
                self.heard_tracker.mark_playback_started(started_ns)
            stopped_ns = started_ns + int(max(0.0, playback_position_s) * 1e9)
            self.playback.stopped_mono_ns = stopped_ns
        if stopped_ns is not None:
            self.heard_tracker.mark_playback_stopped(stopped_ns)
        heard = (
            synchronized_transcript
            if synchronized_transcript is not None
            else self.heard_tracker.snapshot()
        )
        self.context.commit_assistant_heard(
            heard,
            speaker_scope=self.speaker_scope_for_fence(committed_fence),
        )
        async with self._state_lock:
            if self.state is ConversationState.SPEAKING:
                if tools_active:
                    self.state_machine.apply(TransitionEvent.PLAYBACK_DONE_TOOLS_ACTIVE)
                else:
                    self.state_machine.apply(TransitionEvent.PLAYBACK_DONE)
        return heard

    async def confirm_interruption(
        self,
        cause: str = "adaptive_interruption",
        *,
        stop_playback: Callable[[], Awaitable[str | None]] | None = None,
        precondition: Callable[[], bool] | None = None,
        create_user_turn: bool = True,
        synchronized_transcript: str | None = None,
        force_generation_bump: bool = False,
    ) -> GenerationFence:
        """Atomic cancel sequence from ch.17 — single critical section."""
        assert self.state_machine is not None
        assert self.fence_gate is not None
        async with self._state_lock:
            if precondition is not None and not precondition():
                return self.fence
            interruptible_states = {
                ConversationState.THINKING,
                ConversationState.SPEAKING,
                ConversationState.INTERRUPTION_PENDING,
                ConversationState.TOOL_WAITING,
            }
            if not force_generation_bump and (
                self.state not in interruptible_states
                and self._active_llm_task is None
                and self._active_tts_task is None
                and not self.playback.playing
                and self.task_manager.active_count() == 0
            ):
                return self.fence

            old = self.fence
            new_fence = old.bump_generation()
            self.state_machine.fence = new_fence
            self.fence_gate.update(new_fence)
            if cause != "rtc_recovered":
                self.metrics.inc_interruptions_confirmed()

            if self.playback.playing:
                # progress until stop
                if (
                    self.playback.started_mono_ns is not None
                    and self.heard_tracker.playback_started_mono_ns is None
                ):
                    self.heard_tracker.mark_playback_started(self.playback.started_mono_ns)

            livekit_heard = await stop_playback() if stop_playback is not None else None
            await self.playback.stop_and_flush()
            if self.playback.stopped_mono_ns is not None:
                self.heard_tracker.mark_playback_stopped(self.playback.stopped_mono_ns)

            heard_text = (
                synchronized_transcript
                if synchronized_transcript is not None
                else livekit_heard
                if livekit_heard is not None
                else self.heard_tracker.snapshot()
            )
            self._tts_cancel.set()
            await cancel_and_wait(self._active_llm_task)
            await cancel_and_wait(self._active_tts_task)
            await self.tts_pool.discard_active_connection(old)
            await self.task_manager.cancel_cancellable(old)
            interrupted_message = self.context.commit_interrupted_assistant_text(
                heard_text,
                speaker_scope=self.speaker_scope_for_fence(old),
            )
            self._pending_interrupted_from = old
            self._pending_interrupted_to = new_fence
            self._pending_interrupted_message = interrupted_message

            if not create_user_turn and self.state_machine.can_transition(
                TransitionEvent.STOP_RESPONSE
            ):
                self.state_machine.apply(
                    TransitionEvent.STOP_RESPONSE,
                    cause=cause,
                    new_fence=new_fence,
                )
            elif self.state is ConversationState.SPEAKING:
                self.state_machine.apply(TransitionEvent.USER_VOICE_WHILE_SPEAKING)
                self.state_machine.apply(
                    TransitionEvent.REAL_INTERRUPT,
                    cause=cause,
                    new_fence=new_fence,
                )
            elif self.state_machine.can_transition(TransitionEvent.REAL_INTERRUPT):
                self.state_machine.apply(
                    TransitionEvent.REAL_INTERRUPT,
                    cause=cause,
                    new_fence=new_fence,
                )
            elif self.state is ConversationState.THINKING:
                self.state_machine.apply(
                    TransitionEvent.USER_SPEAKS_DURING_THINK,
                    cause=cause,
                    new_fence=new_fence,
                )
            else:
                # Ensure fence advanced even if state path differs
                self.state_machine.fence = new_fence

            self._active_llm_task = None
            self._active_tts_task = None
            return new_fence

    async def finalize_interrupted_playback(
        self,
        *,
        interrupted_from: GenerationFence,
        synchronized_transcript: str | None,
    ) -> tuple[str, GenerationFence] | None:
        """Refine one confirmed interruption without bumping its generation again."""
        async with self._state_lock:
            if (
                self._pending_interrupted_from is None
                or not self._pending_interrupted_from.matches(interrupted_from)
                or self._pending_interrupted_to is None
            ):
                return None

            message = self._pending_interrupted_message
            if synchronized_transcript is not None:
                message = self.context.refine_assistant_heard(
                    message,
                    synchronized_transcript,
                    speaker_scope=self.speaker_scope_for_fence(interrupted_from),
                )
            heard = message.content if message is not None else ""
            fence = self._pending_interrupted_to
            self._pending_interrupted_from = None
            self._pending_interrupted_to = None
            self._pending_interrupted_message = None
            return heard, fence

    async def accept_background_result(
        self,
        fence: GenerationFence,
        payload: Any,
    ) -> Any | None:
        """Accept a background result only through the complete production fence."""
        assert self.state_machine is not None
        assert self.fence_gate is not None
        async with self._state_lock:
            accepted = self.fence_gate.gate(fence, payload, source="tool")
            if accepted is None:
                return None
            if self.state_machine.can_transition(TransitionEvent.TOOL_RESULT_VALID):
                self.state_machine.apply(TransitionEvent.TOOL_RESULT_VALID)
            return accepted

    async def evaluate_interruption(self, asr_text: str, elapsed_ms: int) -> InterruptDecision:
        decision = self.interruption_guard.evaluate(
            elapsed_ms=elapsed_ms,
            asr_text=asr_text,
            has_speech_energy=True,
        )
        if decision is InterruptDecision.CONFIRM_INTERRUPT:
            await self.confirm_interruption(cause="explicit_or_rule")
        elif decision is InterruptDecision.FALSE_INTERRUPTION:
            self.metrics.inc_false_interruptions()
            assert self.state_machine is not None
            async with self._state_lock:
                if self.state_machine.can_transition(TransitionEvent.BACKCHANNEL_OR_NOISE):
                    self.state_machine.apply(TransitionEvent.BACKCHANNEL_OR_NOISE)
        elif decision is InterruptDecision.RESUME:
            assert self.state_machine is not None
            async with self._state_lock:
                if self.state_machine.can_transition(TransitionEvent.BACKCHANNEL_OR_NOISE):
                    self.state_machine.apply(TransitionEvent.BACKCHANNEL_OR_NOISE)
        elif decision is InterruptDecision.UNCERTAIN:
            # Prefer yield
            await self.confirm_interruption(cause="uncertain_yield")
            return InterruptDecision.CONFIRM_INTERRUPT
        return decision

    async def bump_tool_epoch_on_condition_change(self) -> GenerationFence:
        assert self.state_machine is not None
        assert self.fence_gate is not None
        async with self._state_lock:
            new_fence = self.fence.bump_tool_epoch()
            self.state_machine.fence = new_fence
            self.fence_gate.update(new_fence)
            if self.state_machine.can_transition(TransitionEvent.USER_CHANGED_TOOL_CONDITIONS):
                self.state_machine.apply(
                    TransitionEvent.USER_CHANGED_TOOL_CONDITIONS,
                    new_fence=new_fence,
                )
            return new_fence

    def publish_audio_if_current(self, fence: GenerationFence, pcm: bytes) -> bool:
        """Returns True if audio was published (not stale)."""
        gated = self.gate_tts_audio(fence, pcm)
        if gated is None:
            return False
        self.playback.push_pcm(gated)
        return True

    async def close(self) -> None:
        assert self.state_machine is not None
        async with self._state_lock:
            self._tts_cancel.set()
            await cancel_and_wait(self._active_llm_task)
            await cancel_and_wait(self._active_tts_task)
            await self.task_manager.cancel_cancellable(self.fence)
            self.state_machine.apply(TransitionEvent.SESSION_END)


@dataclass
class OfflinePipeline:
    """ASR -> LLM -> TTS offline path using injectable providers."""

    orchestrator: Orchestrator
    asr_final_text: str = ""
    llm_handler: LanguageModelHandler | None = None
    tts_handler: SpeechSynthesisHandler | None = None

    async def run_turn(self, user_text: str) -> dict[str, Any]:
        orch = self.orchestrator
        if orch.state is ConversationState.CONNECTING:
            await orch.ready()
        await orch.on_vad_start()
        fence = await orch.commit_turn(user_text)
        cancellation = orch.cancellation_context(fence)

        full_content = ""
        assert orch.segmenter is not None
        phrases: list[str] = []

        if self.llm_handler is not None:
            request = LanguageModelRequest(
                user_text=user_text,
                cancellation=cancellation,
            )
            async for token in self.llm_handler.stream(request):
                gated = orch.gate_llm_token(cancellation, token)
                if gated is None:
                    continue
                full_content += gated
                segs = orch.segmenter.push_token(gated)
                phrases.extend(s.text for s in segs)
            segs = orch.segmenter.flush(end_of_stream=True)
            phrases.extend(s.text for s in segs)
        else:
            full_content = f"收到，关于「{user_text}」我这边记下了。"
            phrases = [full_content]

        words = _approx_words(full_content)
        await orch.begin_speaking(words, full_content)

        pcm_total = bytearray()
        if self.tts_handler is not None:
            result = await self.tts_handler.synthesize(
                SpeechSynthesisRequest(
                    phrases=tuple(phrases),
                    cancellation=cancellation,
                    cancel_event=orch.tts_cancel_event(),
                )
            )
            pcm = getattr(result, "pcm", b"") or b""
            if orch.publish_audio_if_current(cancellation.fence, pcm):
                pcm_total.extend(pcm)
            w = getattr(result, "words", ())
            if w:
                orch.heard_tracker.words = list(w)
        else:
            # synthetic silence proportional to text
            pcm = b"\x00\x00" * max(100, len(full_content) * 240)
            if orch.publish_audio_if_current(fence, pcm):
                pcm_total.extend(pcm)

        await orch.finish_speaking()
        return {
            "user_text": user_text,
            "assistant_text": orch.context.turns[-1].content if orch.context.turns else "",
            "full_generated": full_content,
            "pcm_bytes": len(pcm_total),
            "fence": fence,
            "phrases": phrases,
        }


def _approx_words(text: str, ms_per_char: int = 80) -> list[TimedWord]:
    words: list[TimedWord] = []
    t = 0
    for ch in text:
        begin = t
        end = t + ms_per_char
        words.append(TimedWord(text=ch, begin_ms=begin, end_ms=end))
        t = end
    return words
