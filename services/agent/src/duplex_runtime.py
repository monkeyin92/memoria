"""Wire Duplex Orchestrator invariants into the LiveKit AgentSession path (ch.8–18)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import GenerationFence, new_session_id
from services.agent.src.observability.tracing import LatencyTrace
from services.agent.src.orchestration.cue_scheduler import CueScheduler, ListenerCue
from services.agent.src.orchestration.emotion import (
    EmotionObservation,
    EmotionSmoother,
    neutral_observation,
)
from services.agent.src.orchestration.heard_text_tracker import HeardTextTracker
from services.agent.src.orchestration.interruption_guard import (
    PlaybackInputDecision,
    PlaybackInputGuard,
    interrupt_ack_phrase,
)
from services.agent.src.orchestration.orchestrator import CosyPoolHandle, Orchestrator
from services.agent.src.orchestration.phrase_segmenter import PhraseSegmenter
from services.agent.src.orchestration.prosody import (
    SpeechPlan,
    speech_plan_for_emotion,
    speech_plan_for_turn,
)
from services.agent.src.orchestration.speaker_verify import SpeakerGateState, SpeakerVerifier
from services.agent.src.orchestration.state_machine import ConversationState, InteractionPhase
from services.agent.src.orchestration.task_manager import ToolSpec, spoken_result_summarizer
from services.agent.src.orchestration.utterance_router import (
    UtteranceIntent,
    UtteranceRoute,
    route_utterance,
)
from services.agent.src.providers.cosyvoice_tts import CosyVoicePool, CosyVoiceTTS

logger = logging.getLogger(__name__)

POST_PLAYBACK_ECHO_GUARD_MS = 800
PLAYBACK_INPUT_BLOCK_MIN_WORDS = 1000
CLIENT_AUDIO_TRACE_NAMES = frozenset(
    {
        "audio_unlock",
        "session_created",
        "room_connected",
        "agent_ready",
        "track_subscribed",
        "audio_attached",
        "play_resolved",
        "play_rejected",
        "playing",
        "first_playback",
        "media_error",
        "webrtc_inbound_audio",
    }
)
CLIENT_AUDIO_METRIC_NAMES = frozenset(
    {
        "jitter",
        "packets_lost",
        "packets_received",
        "packets_discarded",
        "bytes_received",
        "nack_count",
        "concealed_samples",
        "silent_concealed_samples",
        "total_samples_received",
        "concealment_events",
        "concealment_ratio",
        "non_silent_concealment_ratio",
        "jitter_buffer_delay",
        "jitter_buffer_target_delay",
        "jitter_buffer_minimum_delay",
        "jitter_buffer_emitted_count",
        "total_samples_duration",
        "average_jitter_buffer_delay_ms",
        "average_jitter_buffer_target_delay_ms",
        "encoded_audio_bitrate_kbps",
        "inserted_samples_for_deceleration",
        "removed_samples_for_acceleration",
    }
)


@dataclass
class LiveKitCosyPoolAdapter(CosyPoolHandle):
    """Adapt CosyVoicePool.discard_active_connection for Orchestrator."""

    pool: CosyVoicePool | None = None
    discarded: list[GenerationFence] = field(default_factory=list)

    async def discard_active_connection(self, fence: GenerationFence) -> None:
        self.discarded.append(fence)
        if self.pool is not None:
            await self.pool.discard_active_connection(fence)


@dataclass
class DuplexRuntime:
    """Session-scoped orchestration bound to LiveKit agent lifecycle."""

    orchestrator: Orchestrator
    tts: CosyVoiceTTS | None = None
    session_id: str = field(default_factory=new_session_id)
    input_guard: PlaybackInputGuard = field(default_factory=PlaybackInputGuard)
    latency_trace: LatencyTrace = field(default_factory=LatencyTrace)
    cue_scheduler: CueScheduler = field(default_factory=CueScheduler)
    emotion_smoother: EmotionSmoother = field(default_factory=EmotionSmoother)
    speech_plan: SpeechPlan = field(default_factory=lambda: speech_plan_for_emotion("neutral"))
    interaction_phase: InteractionPhase = InteractionPhase.CONNECTING
    use_paralinguistic_tags: bool = False
    speaker_verifier: SpeakerVerifier = field(default_factory=SpeakerVerifier)
    _unsubscribers: list[Callable[[], None]] = field(default_factory=list)
    _was_speaking: bool = False
    _pending_assistant_text: str = ""
    _played_assistant_text: str = ""
    _fresh_user_speech: bool = False
    _playback_fence: GenerationFence | None = None
    _last_playback_completed_ns: int | None = None
    _set_interruption_min_words: Callable[[int], None] | None = None
    _base_interruption_min_words: int = 0
    _event_publisher: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    _result_speaker: Callable[[str], Any] | None = None
    _interrupt_yield: Callable[[str], Awaitable[None]] | None = None
    _false_interrupt_recover: Callable[[], Awaitable[None]] | None = None
    _last_interrupt_yield_ns: int | None = None
    _last_false_recover_ns: int | None = None
    _last_listen_restore_ns: int | None = None
    _playback_started_ns: int | None = None
    # After control yield, accept chat turns even if LiveKit omits speech anchors.
    CONTROL_RESTORE_SPEECH_EPOCH_GRACE_MS: int = 20_000
    _deep_client: Any | None = None
    _background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    # Friendly yield when we stop mid-reply so silence does not feel like a crash.
    INTERRUPT_YIELD_COOLDOWN_MS: int = 4_000
    FALSE_INTERRUPT_RECOVER_COOLDOWN_MS: int = 3_000
    _pending_tool_results: int = 0
    _listener_cue_player: Callable[[str], Any] | None = None
    _active_listener_cue: ListenerCue | None = None
    _active_listener_cue_handle: Any | None = None
    _listener_cue_candidate_task: asyncio.Task[Any] | None = None
    _listener_cue_aec_healthy: bool = False
    _emotion_turn_observer: Callable[[int], None] | None = None
    _emotion_by_turn: dict[int, EmotionObservation] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        session_id: str | None = None,
        tts: CosyVoiceTTS | None = None,
        input_guard_enabled: bool = False,
        listener_cues_enabled: bool = False,
        use_paralinguistic_tags: bool = False,
        speaker_verifier: SpeakerVerifier | None = None,
    ) -> DuplexRuntime:
        sid = session_id or new_session_id()
        orch = Orchestrator(session_id=sid)
        if tts is not None:
            orch.cosyvoice_pool = LiveKitCosyPoolAdapter(pool=tts.pool)
        return cls(
            orchestrator=orch,
            tts=tts,
            session_id=sid,
            input_guard=PlaybackInputGuard(enabled=input_guard_enabled),
            cue_scheduler=CueScheduler(enabled=listener_cues_enabled),
            use_paralinguistic_tags=use_paralinguistic_tags,
            speaker_verifier=speaker_verifier
            if speaker_verifier is not None
            else SpeakerVerifier(enabled=False),
        )

    @property
    def fence(self) -> GenerationFence:
        return self.orchestrator.fence

    @property
    def heard_tracker(self) -> HeardTextTracker:
        return self.orchestrator.heard_tracker

    @property
    def segmenter(self) -> PhraseSegmenter | None:
        return self.orchestrator.segmenter

    def gate_llm_token(self, fence: GenerationFence, token: str) -> str | None:
        return self.orchestrator.gate_llm_token(fence, token)

    def gate_tts_audio(self, fence: GenerationFence, pcm: bytes) -> bytes | None:
        return self.orchestrator.gate_tts_audio(fence, pcm)

    def gate_tool_result(self, fence: GenerationFence, payload: Any) -> Any | None:
        return self.orchestrator.gate_tool_result(fence, payload)

    def set_event_publisher(
        self,
        publisher: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._event_publisher = publisher

    def set_result_speaker(self, speaker: Callable[[str], Any]) -> None:
        self._result_speaker = speaker

    def set_listener_cue_player(self, player: Callable[[str], Any]) -> None:
        self._listener_cue_player = player

    def set_listener_cue_aec_healthy(self, healthy: bool) -> None:
        self._listener_cue_aec_healthy = healthy

    def set_emotion_turn_observer(self, observer: Callable[[int], None]) -> None:
        self._emotion_turn_observer = observer

    def feed_speaker_pcm(self, pcm: bytes) -> None:
        # Never enroll assistant TTS that leaks into the mic during playback.
        # After begin_speaker_enrollment we force _was_speaking=False so user
        # enroll speech is always collected.
        if (
            self.speaker_verifier.state is SpeakerGateState.PENDING
            and self._was_speaking
            and not getattr(self, "_enroll_collecting", False)
        ):
            return
        self.speaker_verifier.feed_pcm(pcm)

    def begin_speaker_enrollment(self) -> None:
        # Fixed session.say may leave _was_speaking stuck True (playback_finished
        # non-interrupt path used to not clear it). Clear so enroll PCM is fed.
        self._was_speaking = False
        self._enroll_collecting = True
        self._enroll_started_mono = time.monotonic()
        self.speaker_verifier.begin_enrollment()
        self.publish_assistant_state("speaker_enroll")
        self.mark_audio_event("speaker_enroll_started")

    def poll_speaker_enrollment(self, *, force: bool = False) -> dict[str, object] | None:
        wall_ms: int | None = None
        started = getattr(self, "_enroll_started_mono", None)
        if started is not None:
            wall_ms = int((time.monotonic() - started) * 1000)
        result = self.speaker_verifier.try_finalize_enrollment(
            force=force,
            wall_elapsed_ms=wall_ms,
        )
        if result is None:
            # Avoid spamming LiveKit data channel every poll tick.
            progress = self.speaker_verifier.enrollment_progress()
            speech_ms = int(progress["speech_ms"])
            last = getattr(self, "_last_enroll_progress_speech_ms", -1)
            if speech_ms - last >= 500 or speech_ms == 0 and last < 0:
                self._last_enroll_progress_speech_ms = speech_ms
                self._publish(
                    {
                        "type": "speaker_enroll_progress",
                        "session_id": self.session_id,
                        "state": progress["state"],
                        "speech_ms": progress["speech_ms"],
                        "target_ms": progress["target_ms"],
                        "elapsed_ms": progress["elapsed_ms"],
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
            return None
        payload = {
            "type": "speaker_enroll_result",
            "session_id": self.session_id,
            "accepted": result.accepted,
            "reason": result.reason,
            "score": round(result.score, 4),
            "speech_ms": result.speech_ms,
            "state": self.speaker_verifier.state.value,
            "at": datetime.now(UTC).isoformat(),
        }
        self._publish(payload)
        self._enroll_collecting = False
        self.mark_audio_event(
            "speaker_enrolled" if result.reason == "enrolled" else "speaker_enroll_open",
            detail={"reason": result.reason, "speech_ms": result.speech_ms},
        )
        logger.info(
            "speaker_enroll result=%s state=%s speech_ms=%s session_id=%s",
            result.reason,
            self.speaker_verifier.state.value,
            result.speech_ms,
            self.session_id,
        )
        return payload

    def _interrupt_candidate_text(self) -> str:
        return (getattr(self.input_guard, "candidate_text", None) or "").strip()

    def _route_candidate(self, text: str | None = None) -> UtteranceRoute:
        """Classify utterance via the shared control-plane router."""
        return route_utterance(
            text if text is not None else self._interrupt_candidate_text(),
            speaker_state=self.speaker_verifier.state,
        )

    def _is_explicit_owner_interrupt_cmd(self) -> bool:
        """True when ASR heard stop/yield phrases like「停一下」「等等」."""
        return self._route_candidate().speaker_gate_override

    def _speaker_allows_user_input(self, *, context: str) -> bool:
        if not self.speaker_verifier.enabled:
            return True
        if self.speaker_verifier.state in {
            SpeakerGateState.DISABLED,
            SpeakerGateState.OPEN,
        }:
            return True
        # Enrollment speech must not become a normal user turn / interrupt.
        if self.speaker_verifier.state is SpeakerGateState.PENDING:
            if context in {"turn_commit", "interrupt", "barge_in_start"}:
                return False
            return True
        # Explicit stop phrases from the mic: always treat as real interrupt.
        # Short「停一下」often scores too_short and used to wrongly trigger「我继续」.
        if context in {"interrupt", "barge_in_start"} and self._is_explicit_owner_interrupt_cmd():
            return True
        score = self.speaker_verifier.score_latest_utterance()
        if score.reason == "too_short":
            # Prefer rolling window over "allow any short blip" — nearby noise was
            # cancelling TTS while owner was enrolled.
            score = self.speaker_verifier.score_pcm()
            if score.reason == "too_short":
                # barge_in_start / interrupt: need enough speech before cancel.
                # turn_commit: fail open only after a full utterance window.
                if context in {"barge_in_start", "interrupt"}:
                    return False
                return context != "interrupt"
        if not score.accepted:
            # Soft accept for full turn commits: lightweight log-mel embedding is
            # noisy. Prod 20260717-200500: owner「介绍南京」scored 0.57–0.58 vs
            # threshold 0.62 → permanent silence with no LLM turn.
            thr = float(self.speaker_verifier.accept_threshold)
            soft_floor = max(0.45, thr - 0.15)
            soft_ok = (
                context == "turn_commit"
                and score.reason == "mismatch"
                and score.speech_ms >= int(self.speaker_verifier.min_verify_speech_ms)
                and score.score >= soft_floor
            )
            if soft_ok:
                logger.info(
                    "speaker_soft_accept context=%s score=%.3f thr=%.3f "
                    "soft_floor=%.3f speech_ms=%s session_id=%s",
                    context,
                    score.score,
                    thr,
                    soft_floor,
                    score.speech_ms,
                    self.session_id,
                )
                self.mark_audio_event(
                    "speaker_soft_accept",
                    detail={
                        "context": context,
                        "score": round(score.score, 4),
                        "threshold": thr,
                        "soft_floor": soft_floor,
                        "speech_ms": score.speech_ms,
                    },
                )
                return True
            self.orchestrator.metrics.inc_guarded_user_input(f"speaker_{score.reason}")
            logger.info(
                "speaker_reject context=%s reason=%s score=%.3f speech_ms=%s session_id=%s",
                context,
                score.reason,
                score.score,
                score.speech_ms,
                self.session_id,
            )
            self.mark_audio_event(
                "speaker_rejected",
                status="ignored",
                detail={
                    "context": context,
                    "reason": score.reason,
                    "score": round(score.score, 4),
                    "speech_ms": score.speech_ms,
                },
            )
            self._publish(
                {
                    "type": "speaker_reject",
                    "session_id": self.session_id,
                    "context": context,
                    "reason": score.reason,
                    "score": round(score.score, 4),
                    "speech_ms": score.speech_ms,
                    "at": datetime.now(UTC).isoformat(),
                }
            )
            return False
        return True

    def _spawn(self, awaitable: Awaitable[Any], *, name: str) -> asyncio.Task[Any]:
        async def _run() -> Any:
            return await awaitable

        task: asyncio.Task[Any] = asyncio.create_task(_run(), name=name)
        self._background_tasks.add(task)

        def _done(completed: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(completed)
            if not completed.cancelled() and (error := completed.exception()) is not None:
                logger.error(
                    "duplex background task failed: %s: %s",
                    completed.get_name(),
                    error,
                )

        task.add_done_callback(_done)
        return task

    def _publish(self, event: dict[str, Any]) -> asyncio.Task[Any] | None:
        if self._event_publisher is not None:
            return self._spawn(
                self._event_publisher(event),
                name=f"duplex-ui-{event['type']}",
            )
        return None

    def set_interaction_phase(
        self,
        phase: InteractionPhase,
        *,
        cause: str = "",
        publish: bool = True,
    ) -> asyncio.Task[Any] | None:
        if phase is self.interaction_phase:
            return None
        previous = self.interaction_phase
        self.interaction_phase = phase
        cause_s = cause or "unspecified"
        logger.info(
            "interaction_phase from=%s to=%s cause=%s session_id=%s turn_id=%s generation_id=%s",
            previous.value,
            phase.value,
            cause_s,
            self.session_id,
            self.fence.turn_id,
            self.fence.generation_id,
        )
        self.orchestrator.metrics.inc_interaction_phase(
            previous.value,
            phase.value,
            cause_s,
        )
        # Audio-trace seam so production logs can grep phase without UI events.
        self.mark_audio_event(
            "interaction_phase",
            detail={
                "from": previous.value,
                "to": phase.value,
                "cause": cause_s[:80],
            },
        )
        if not publish:
            return None
        return self.publish_assistant_state(phase.value, phase=phase)

    def publish_assistant_state(
        self,
        state: str,
        *,
        phase: InteractionPhase | None = None,
    ) -> asyncio.Task[Any] | None:
        fence = self.fence
        mapped_phase = phase or self._phase_for_published_state(state)
        if mapped_phase is not None and mapped_phase is not self.interaction_phase:
            previous = self.interaction_phase
            self.interaction_phase = mapped_phase
            logger.info(
                "interaction_phase from=%s to=%s cause=publish:%s session_id=%s "
                "turn_id=%s generation_id=%s",
                previous.value,
                mapped_phase.value,
                state,
                self.session_id,
                fence.turn_id,
                fence.generation_id,
            )
        return self._publish(
            {
                "type": "assistant_state",
                "session_id": self.session_id,
                "state": state,
                "phase": self.interaction_phase.value,
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "at": datetime.now(UTC).isoformat(),
            }
        )

    @staticmethod
    def _phase_for_published_state(state: str) -> InteractionPhase | None:
        mapping = {
            "connecting": InteractionPhase.CONNECTING,
            "ready": InteractionPhase.LISTENING,
            "speaker_enroll": InteractionPhase.LISTENING,
            "listening": InteractionPhase.LISTENING,
            "user_speaking": InteractionPhase.USER_SPEAKING,
            "backchannel": InteractionPhase.BACKCHANNEL,
            "thinking": InteractionPhase.THINKING_SILENT,
            "thinking_silent": InteractionPhase.THINKING_SILENT,
            "speaking": InteractionPhase.SPEAKING,
            "interrupted": InteractionPhase.INTERRUPTED,
            "tool_waiting": InteractionPhase.TOOL_WAITING,
            "recovering": InteractionPhase.RECOVERING,
            "closed": InteractionPhase.CLOSED,
        }
        return mapping.get(state)

    def publish_assistant_audio(
        self,
        action: str,
        *,
        gain: float,
    ) -> asyncio.Task[Any] | None:
        if action not in {"duck", "restore"}:
            raise ValueError(f"unsupported assistant audio action: {action}")
        fence = self.fence
        return self._publish(
            {
                "type": "assistant_audio",
                "session_id": self.session_id,
                "action": action,
                "gain": min(1.0, max(0.0, gain)),
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "at": datetime.now(UTC).isoformat(),
            }
        )

    def mark_audio_event(
        self,
        name: str,
        *,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
        mono_ns: int | None = None,
    ) -> None:
        """Record and publish one server-side first-audio stage without blocking media."""
        if status == "ok":
            self.latency_trace.mark(name, mono_ns=mono_ns)
        fence = self.fence
        event: dict[str, Any] = {
            "type": "audio_trace",
            "source": "agent",
            "session_id": self.session_id,
            "name": name,
            "status": status,
            "turn_id": fence.turn_id,
            "generation_id": fence.generation_id,
            "at": datetime.now(UTC).isoformat(),
        }
        if detail:
            event["detail"] = detail
        logger.info(
            "agent_audio_trace name=%s status=%s turn_id=%s generation_id=%s",
            name,
            status,
            fence.turn_id,
            fence.generation_id,
        )
        self._publish(event)

    def observe_client_audio_trace(
        self,
        event: dict[str, Any],
        *,
        mono_ns: int | None = None,
    ) -> bool:
        """Accept bounded client playback facts; permanent keys/audio never enter telemetry."""
        if (
            event.get("session_id") != self.session_id
            or event.get("name") not in CLIENT_AUDIO_TRACE_NAMES
            or event.get("status") not in {"ok", "error"}
        ):
            return False
        metrics: dict[str, int | float] = {}
        if event["name"] == "webrtc_inbound_audio":
            detail = event.get("detail")
            if not isinstance(detail, dict) or not detail:
                return False
            if not set(detail).issubset(CLIENT_AUDIO_METRIC_NAMES):
                return False
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for value in detail.values()
            ):
                return False
            metrics = {str(key): value for key, value in detail.items()}
        if event["name"] == "first_playback" and event["status"] == "ok":
            self.latency_trace.mark("client_first_playback", mono_ns=mono_ns)
        logger.info(
            "client_audio_trace name=%s status=%s turn_id=%s generation_id=%s metrics=%s",
            event["name"],
            event["status"],
            event.get("turn_id"),
            event.get("generation_id"),
            metrics,
        )
        return True

    def publish_transcript(
        self,
        *,
        speaker: str,
        text: str,
        final: bool,
        heard: bool | None = None,
        fence: GenerationFence | None = None,
    ) -> None:
        fence = fence or self.fence
        event: dict[str, Any] = {
            "type": "transcript_delta",
            "speaker": speaker,
            "text": text,
            "final": final,
            "turn_id": fence.turn_id,
            "generation_id": fence.generation_id,
        }
        if heard is not None:
            event["heard"] = heard
        self._publish(event)

    def observe_acoustic_emotion(
        self,
        provider_label: str,
        *,
        text: str = "",
        turn_id: int | None = None,
    ) -> EmotionObservation:
        current_turn_id = self.fence.turn_id
        if turn_id is None or turn_id < current_turn_id or turn_id > current_turn_id + 1:
            return neutral_observation(time.monotonic_ns(), self.emotion_smoother.ttl_ms)
        observation = self.emotion_smoother.observe_acoustic(
            provider_label,
            text=text,
            turn_id=turn_id,
        )
        logger.info(
            "emotion_observation label=%s provider_label=%s evidence=%s turn_id=%s",
            observation.label,
            observation.provider_label,
            ",".join(observation.evidence),
            turn_id,
        )
        self._emotion_by_turn[turn_id] = observation
        self._emotion_by_turn = {
            key: value for key, value in self._emotion_by_turn.items() if key >= current_turn_id
        }
        fence = self.fence
        self._publish(
            {
                "type": "emotion_observation",
                "session_id": self.session_id,
                "label": observation.label,
                "provider_label": observation.provider_label,
                "provider_confidence": None,
                "evidence": list(observation.evidence),
                "persist": False,
                "turn_id": turn_id,
                "generation_id": fence.generation_id,
                "expires_after_ms": self.emotion_smoother.ttl_ms,
                "at": datetime.now(UTC).isoformat(),
            }
        )
        return observation

    def _apply_speech_plan(self, user_text: str, *, turn_id: int) -> SpeechPlan:
        acoustic = self._emotion_by_turn.pop(turn_id, None)
        self._emotion_by_turn = {
            key: value for key, value in self._emotion_by_turn.items() if key > turn_id
        }
        observation = self.emotion_smoother.observe_text(user_text, acoustic=acoustic)
        self.speech_plan = speech_plan_for_turn(
            label=observation.label,
            provider_label=observation.provider_label,
            text=user_text,
            evidence=observation.evidence,
            use_markup_tags=self.use_paralinguistic_tags,
        )
        apply_plan = getattr(self.tts, "apply_speech_plan", None)
        if callable(apply_plan):
            apply_plan(
                emotion=self.speech_plan.voice_emotion,
                rate=self.speech_plan.rate,
            )
        logger.info(
            "speech_plan_selected emotion=%s rate=%.2f delivery=%s "
            "tts_prefix=%s strip=%s turn_id=%s",
            self.speech_plan.voice_emotion,
            self.speech_plan.rate,
            self.speech_plan.delivery_mode,
            self.speech_plan.tts_prefix or "-",
            self.speech_plan.strip_paralinguistic,
            turn_id,
        )
        return self.speech_plan

    def _publish_listener_cue(self, cue: ListenerCue, state: str) -> None:
        self._publish(
            {
                "type": "listener_cue",
                "session_id": self.session_id,
                "cue_id": cue.cue_id,
                "cue_epoch": cue.cue_epoch,
                "user_turn_id": cue.user_turn_id,
                "state": state,
                "phase": self.interaction_phase.value,
                "at": datetime.now(UTC).isoformat(),
            }
        )

    async def _play_listener_cue(self, cue: ListenerCue) -> None:
        if self._listener_cue_player is None or not self.cue_scheduler.is_current(cue):
            return
        handle = self._listener_cue_player(cue.text)
        if inspect.isawaitable(handle):
            handle = await handle
        if not self.cue_scheduler.is_current(cue):
            stop = getattr(handle, "stop", None)
            if callable(stop):
                stop()
            return
        self._active_listener_cue = cue
        self._active_listener_cue_handle = handle
        self.set_interaction_phase(
            InteractionPhase.BACKCHANNEL,
            cause=f"listener_cue:{cue.text}",
        )
        self._publish_listener_cue(cue, "started")
        try:
            wait_for_playout = getattr(handle, "wait_for_playout", None)
            if callable(wait_for_playout):
                await wait_for_playout()
            elif inspect.isawaitable(handle):
                await handle
            if self.cue_scheduler.is_current(cue):
                self._publish_listener_cue(cue, "finished")
        finally:
            if self._active_listener_cue is cue:
                self._active_listener_cue = None
                self._active_listener_cue_handle = None
            if self.interaction_phase is InteractionPhase.BACKCHANNEL:
                self.set_interaction_phase(
                    InteractionPhase.USER_SPEAKING,
                    cause="listener_cue_finished",
                )

    def cancel_listener_cue(self) -> None:
        self._cancel_listener_cue_candidate()
        cue = self._active_listener_cue
        self.cue_scheduler.cancel_turn()
        handle = self._active_listener_cue_handle
        stop = getattr(handle, "stop", None)
        if callable(stop):
            stop()
        self._active_listener_cue = None
        self._active_listener_cue_handle = None
        if cue is not None:
            self._publish_listener_cue(cue, "cancelled")
        if self.interaction_phase is InteractionPhase.BACKCHANNEL:
            self.set_interaction_phase(
                InteractionPhase.USER_SPEAKING,
                cause="listener_cue_cancelled",
                publish=False,
            )

    def _cancel_listener_cue_candidate(self) -> None:
        task = self._listener_cue_candidate_task
        if task is not None and not task.done():
            task.cancel()
        self._listener_cue_candidate_task = None

    def _schedule_listener_cue(self, text: str, *, now_ns: int | None) -> None:
        self._cancel_listener_cue_candidate()
        observed_at_ns = now_ns if now_ns is not None else time.monotonic_ns()

        async def _after_micro_pause() -> None:
            try:
                await asyncio.sleep(self.cue_scheduler.pause_ms / 1000)
                cue = self.cue_scheduler.observe_partial(
                    text,
                    now_ns=observed_at_ns + self.cue_scheduler.pause_ms * 1_000_000,
                    aec_healthy=self._listener_cue_aec_healthy,
                    main_response_active=self.orchestrator.state
                    in {
                        ConversationState.THINKING,
                        ConversationState.SPEAKING,
                        ConversationState.TOOL_WAITING,
                    },
                )
                if cue is not None:
                    if self._listener_cue_candidate_task is asyncio.current_task():
                        self._listener_cue_candidate_task = None
                    await self._play_listener_cue(cue)
            finally:
                if self._listener_cue_candidate_task is asyncio.current_task():
                    self._listener_cue_candidate_task = None

        self._listener_cue_candidate_task = self._spawn(
            _after_micro_pause(),
            name="listener-cue-micro-pause",
        )

    def update_pending_assistant_text(self, text: str) -> None:
        self._pending_assistant_text = text
        self.orchestrator.heard_tracker.set_full_text(text)

    def on_user_voice_started(self, *, now_ns: int | None = None) -> PlaybackInputDecision:
        self._fresh_user_speech = True
        self.speaker_verifier.mark_utterance_start()
        if not self._was_speaking:
            self.set_interaction_phase(
                InteractionPhase.USER_SPEAKING,
                cause="vad_start",
            )
        self.input_guard.start(during_playback=self._was_speaking, now_ns=now_ns)
        pending_turn_id = self.fence.turn_id + 1
        if self._emotion_turn_observer is not None:
            self._emotion_turn_observer(pending_turn_id)
        if not self._was_speaking:
            self.cue_scheduler.start_turn(
                user_turn_id=pending_turn_id,
                now_ns=now_ns,
            )
        if self.input_guard.candidate_during_playback:
            # Nearby talker often starts with short energy; require speaker match
            # before treating this as a real barge-in candidate.
            if self.speaker_verifier.active and not self._speaker_allows_user_input(
                context="barge_in_start"
            ):
                self.speaker_verifier.mark_utterance_end()
                # VAD may already have moved SPEAKING → INTERRUPTION_PENDING;
                # dismiss so later commit_turn is not stuck.
                self._spawn(
                    self.orchestrator.dismiss_pending_interruption(
                        cause="speaker_reject_barge_in"
                    ),
                    name="dismiss-false-barge",
                )
                return PlaybackInputDecision.IGNORE
            self.orchestrator.metrics.inc_interruption_candidate()
            return PlaybackInputDecision.WAIT
        return PlaybackInputDecision.ACCEPT

    def observe_user_transcript(
        self,
        text: str,
        *,
        final: bool,
        now_ns: int | None = None,
    ) -> PlaybackInputDecision:
        decision = self.input_guard.observe(
            text,
            final=final,
            assistant_text=self._pending_assistant_text or self._played_assistant_text,
            now_ns=now_ns,
        )
        if final:
            self._cancel_listener_cue_candidate()
        elif (
            not final
            and decision is PlaybackInputDecision.ACCEPT
            and self._listener_cue_player is not None
        ):
            self._schedule_listener_cue(text, now_ns=now_ns)
        return decision

    def accept_user_turn(
        self,
        text: str,
        *,
        speech_anchored: bool | None = None,
    ) -> tuple[bool, str | None]:
        self.speaker_verifier.mark_utterance_end()
        # Single control-plane decision: enroll / pure interrupt / chat.
        # Side effects (early enroll finalize, yield ack) stay here; intent is
        # owned by utterance_router so barge-in and turn-commit cannot diverge.
        route = self._route_candidate(text)
        if route.intent is UtteranceIntent.ENROLL:
            # User finished an enroll utterance — try finalize immediately so
            # we do not wait the full wall timeout after they already spoke.
            progress = self.speaker_verifier.enrollment_progress()
            speech_ms = int(progress.get("speech_ms") or 0)
            target_ms = int(progress.get("target_ms") or 2500)
            if speech_ms >= max(1500, int(target_ms * 0.85)):
                early = self.poll_speaker_enrollment()
                if early is not None and early.get("reason") == "enrolled":
                    logger.info(
                        "speaker_enroll early_finalize_on_endpoint speech_ms=%s "
                        "session_id=%s",
                        speech_ms,
                        self.session_id,
                    )
            self.orchestrator.metrics.inc_guarded_user_input("speaker_enrolling")
            logger.info(
                "user_turn_ignored reason=speaker_enrolling text_len=%s "
                "speech_ms=%s session_id=%s",
                len(text),
                speech_ms,
                self.session_id,
            )
            return False, route.reason
        if route.intent is UtteranceIntent.INTERRUPT_COMMAND:
            # 「等等」「停一下」「别说了」are control phrases, not chat questions.
            # If we let them through, the LLM answers「怎么了？」and covers the yield ack.
            self.input_guard.candidate_text = text
            self.orchestrator.metrics.inc_guarded_user_input(route.reason)
            logger.info(
                "user_turn_ignored reason=%s intent=%s text=%s session_id=%s",
                route.reason,
                route.intent,
                text[:40],
                self.session_id,
            )
            self.mark_audio_event(
                "interrupt_command_turn_suppressed",
                detail={
                    "text": text[:40],
                    "intent": route.intent,
                    "ack": route.ack_phrase,
                },
            )
            # Hand floor back: unlock LiveKit min_words so the next real utterance
            # can commit (prod: after 停一下, min_words stuck at 1000 + orphan FINAL).
            self._restore_listen_after_control(cause="interrupt_command_turn")
            # Ensure semantic ack (cooldown skips if interrupt path already said it).
            self._spawn(
                self._maybe_say_interrupt_yield(cause="interrupt_command_turn"),
                name="interrupt-cmd-yield",
            )
            return False, route.reason
        if self.input_guard.enabled and speech_anchored is not None:
            missing_anchor = not speech_anchored or not self._fresh_user_speech
            if missing_anchor:
                # Prod: after「停一下」LiveKit often emits orphan FINAL without
                # started/stopped speaking metrics → session goes permanently silent.
                if (
                    route.enter_chat
                    and route.intent is UtteranceIntent.CHAT
                    and self._within_control_restore_grace()
                    and len(route.normalized_text) >= 2
                ):
                    logger.info(
                        "speech_epoch_fail_open after_control text_len=%s "
                        "anchored=%s fresh=%s session_id=%s",
                        len(route.normalized_text),
                        speech_anchored,
                        self._fresh_user_speech,
                        self.session_id,
                    )
                    self.mark_audio_event(
                        "speech_epoch_fail_open",
                        detail={"reason": "after_control_grace"},
                    )
                    self._fresh_user_speech = False
                else:
                    self._fresh_user_speech = False
                    self.orchestrator.metrics.inc_guarded_user_input("missing_speech_epoch")
                    return False, "missing_speech_epoch"
            else:
                self._fresh_user_speech = False
        if self.speaker_verifier.active and not self._speaker_allows_user_input(
            context="turn_commit"
        ):
            return False, "speaker_mismatch"
        post_playback_reason = self.post_playback_guard_reason(text)
        if post_playback_reason is not None:
            self.orchestrator.metrics.inc_guarded_user_input(post_playback_reason)
            return False, post_playback_reason
        accepted, reason = self.input_guard.accept_turn(text)
        if not accepted:
            self.orchestrator.metrics.inc_guarded_user_input(reason or "unknown")
        return accepted, reason

    async def on_turn_committed(self, user_text: str) -> GenerationFence:
        self.cancel_listener_cue()
        self._apply_speech_plan(user_text, turn_id=self.fence.turn_id + 1)
        self._last_playback_completed_ns = None
        if self.orchestrator.state is ConversationState.CONNECTING:
            await self.orchestrator.ready()
        if self.orchestrator.state is ConversationState.TOOL_WAITING:
            await self.orchestrator.bump_tool_epoch_on_condition_change()
        await self.orchestrator.on_vad_start()
        fence = await self.orchestrator.commit_turn(user_text)
        self.set_interaction_phase(
            InteractionPhase.THINKING_SILENT,
            cause="turn_committed",
        )
        self.mark_audio_event("turn_committed")
        self._pending_assistant_text = ""
        self._played_assistant_text = ""
        self._playback_fence = None
        if self.tts is not None:
            self.tts.bind_fence(fence)
        if self._deep_client is not None and _needs_deep_path(user_text):
            await self._start_deep_task(fence)
        return fence

    async def on_assistant_speaking(
        self,
        full_text: str,
        words: list[TimedWord] | tuple[TimedWord, ...] | None = None,
    ) -> None:
        self._pending_assistant_text = full_text
        self._was_speaking = True
        w = list(words) if words else []
        # Only transition if we are in THINKING (or already SPEAKING is ok to re-enter carefully)
        if self.orchestrator.state is ConversationState.THINKING:
            await self.orchestrator.begin_speaking(w, full_text)
        elif self.orchestrator.state is ConversationState.SPEAKING:
            self.orchestrator.heard_tracker.set_full_text(full_text)
            if w:
                self.orchestrator.heard_tracker.add_words(w)
        else:
            # Still record tracker data for interrupt truncation.
            self.orchestrator.heard_tracker.set_full_text(full_text)
            if w:
                self.orchestrator.heard_tracker.add_words(w)

    async def on_playback_done(self, *, tools_active: bool = False) -> None:
        self._was_speaking = False
        if self.orchestrator.state is ConversationState.SPEAKING:
            await self.orchestrator.finish_speaking(tools_active=tools_active)
        elif self.orchestrator.state is ConversationState.THINKING and self._pending_assistant_text:
            # No audio path — still commit heard text as full if never interrupted.
            await self.orchestrator.begin_speaking([], self._pending_assistant_text)
            await self.orchestrator.finish_speaking(tools_active=tools_active)

    def set_interrupt_yield(
        self, speaker: Callable[[str], Awaitable[None]] | None
    ) -> None:
        """speaker(phrase) — phrase is chosen from interrupt semantics."""
        self._interrupt_yield = speaker

    def set_false_interrupt_recover(
        self, recover: Callable[[], Awaitable[None]] | None
    ) -> None:
        """Called when LiveKit already stopped audio but speaker gate rejected barge-in."""
        self._false_interrupt_recover = recover

    def _assistant_was_mid_reply(self, *, was_speaking: bool) -> bool:
        if was_speaking or self._playback_fence is not None:
            return True
        if self._playback_started_ns is None:
            return False
        # Cover the gap after LiveKit flips agent to listening but before our
        # interrupt handler runs (logs showed ~1s gap and silent death).
        age_ms = (time.monotonic_ns() - self._playback_started_ns) // 1_000_000
        return age_ms < 8_000

    def _within_control_restore_grace(self) -> bool:
        if self._last_listen_restore_ns is None:
            return False
        age_ms = (time.monotonic_ns() - self._last_listen_restore_ns) // 1_000_000
        return age_ms <= self.CONTROL_RESTORE_SPEECH_EPOCH_GRACE_MS

    def _restore_listen_after_control(self, *, cause: str) -> None:
        """After stop/wait, unlock turn commit and publish listening.

        Production logs (2026-07-17): after「停一下」yield finished in ~6ms and
        subsequent user speech was dropped as missing_speech_epoch while
        min_words stayed at PLAYBACK_INPUT_BLOCK_MIN_WORDS (1000).
        """
        if self._set_interruption_min_words is not None:
            self._set_interruption_min_words(self._base_interruption_min_words)
        self._was_speaking = False
        self._playback_fence = None
        self._fresh_user_speech = True
        self._last_listen_restore_ns = time.monotonic_ns()
        # Always force listening after control so UI/logs match.
        if self.interaction_phase is not InteractionPhase.LISTENING:
            self.set_interaction_phase(
                InteractionPhase.LISTENING,
                cause=f"restore_listen:{cause}",
            )
        self.mark_audio_event(
            "listen_restored_after_control",
            detail={
                "cause": cause,
                "min_words": self._base_interruption_min_words,
            },
        )

    async def _maybe_say_interrupt_yield(self, *, cause: str) -> None:
        """Short ack after mid-reply stop so users know we yielded, not crashed."""
        if self._interrupt_yield is None:
            return
        if cause in {"user_button", "stop_response", "rtc_recovered"}:
            return
        now = time.monotonic_ns()
        if (
            self._last_interrupt_yield_ns is not None
            and (now - self._last_interrupt_yield_ns) // 1_000_000
            < self.INTERRUPT_YIELD_COOLDOWN_MS
        ):
            # Still unlock listening if barge-in yield already fired.
            self._restore_listen_after_control(cause=f"yield_cooldown:{cause}")
            return
        self._last_interrupt_yield_ns = now
        candidate = self._interrupt_candidate_text()
        route = self._route_candidate(candidate)
        phrase = route.ack_phrase or interrupt_ack_phrase(candidate)
        self.mark_audio_event(
            "interrupt_yield_started",
            detail={
                "cause": cause,
                "phrase": phrase,
                "candidate": candidate[:40],
                "intent": route.intent,
            },
        )
        try:
            await self._interrupt_yield(phrase)
            self.mark_audio_event(
                "interrupt_yield_done",
                detail={"cause": cause, "phrase": phrase},
            )
        except Exception:
            logger.warning("interrupt yield failed cause=%s", cause, exc_info=True)
            self.mark_audio_event("interrupt_yield_done", status="error")
        finally:
            self._restore_listen_after_control(cause=f"yield_done:{cause}")

    async def _maybe_recover_false_interrupt(self, *, cause: str) -> None:
        """Resume after nearby noise stopped playout but speaker gate rejected it."""
        if self._false_interrupt_recover is None:
            return
        now = time.monotonic_ns()
        if (
            self._last_false_recover_ns is not None
            and (now - self._last_false_recover_ns) // 1_000_000
            < self.FALSE_INTERRUPT_RECOVER_COOLDOWN_MS
        ):
            return
        self._last_false_recover_ns = now
        self.mark_audio_event(
            "false_interrupt_recover_started",
            detail={"cause": cause},
        )
        try:
            await self._false_interrupt_recover()
            self.mark_audio_event(
                "false_interrupt_recover_done",
                detail={"cause": cause},
            )
        except Exception:
            logger.warning(
                "false interrupt recover failed cause=%s", cause, exc_info=True
            )
            self.mark_audio_event(
                "false_interrupt_recover_done",
                status="error",
                detail={"cause": cause},
            )

    async def on_real_interrupt(
        self,
        cause: str = "livekit_interruption",
        *,
        stop_playback: Callable[[], Awaitable[str | None]] | None = None,
        create_user_turn: bool = True,
        synchronized_transcript: str | None = None,
        force_generation_bump: bool = False,
    ) -> GenerationFence:
        self.cancel_listener_cue()
        was_speaking = self._was_speaking
        mid_reply = self._assistant_was_mid_reply(was_speaking=was_speaking)
        candidate = self._interrupt_candidate_text()
        # Barge-in uses the same router as turn-commit so speaker-reject recover
        # cannot fire on pure「停一下」while accept_user_turn treats it as control.
        barge_route = self._route_candidate(candidate)
        owner_cmd = barge_route.speaker_gate_override
        if (
            create_user_turn
            and self.speaker_verifier.active
            and not self._speaker_allows_user_input(context="interrupt")
            and not owner_cmd
        ):
            # Nearby talker: do not bump fence — but LiveKit may already have
            # stopped audio, so recover instead of dead silence.
            # Never recover when user said「停一下」etc. — that is a real yield.
            self.speaker_verifier.mark_utterance_end()
            await self.orchestrator.dismiss_pending_interruption(
                cause="speaker_reject_interrupt"
            )
            self.mark_audio_event(
                "interrupt_blocked_by_speaker",
                status="ignored",
                detail={
                    "cause": cause,
                    "mid_reply": mid_reply,
                    "candidate": candidate[:40],
                    "intent": barge_route.intent,
                },
            )
            if mid_reply:
                self._spawn(
                    self._maybe_recover_false_interrupt(cause=cause),
                    name="false-interrupt-recover",
                )
            return self.fence
        if owner_cmd and create_user_turn:
            logger.info(
                "explicit_interrupt_cmd text=%s intent=%s cause=%s session_id=%s",
                candidate[:40],
                barge_route.intent,
                cause,
                self.session_id,
            )
            self.mark_audio_event(
                "explicit_interrupt_cmd",
                detail={
                    "text": candidate[:40],
                    "cause": cause,
                    "intent": barge_route.intent,
                },
            )
        old_fence = self.fence
        new_fence = await self.orchestrator.confirm_interruption(
            cause=cause,
            stop_playback=stop_playback,
            create_user_turn=create_user_turn,
            synchronized_transcript=synchronized_transcript,
            force_generation_bump=force_generation_bump,
        )
        self.publish_assistant_audio("restore", gain=1.0)
        self._was_speaking = False
        self._last_playback_completed_ns = None
        self._pending_assistant_text = ""
        # Unlock commit gate immediately; yield will keep listening after playout.
        if self._set_interruption_min_words is not None:
            self._set_interruption_min_words(self._base_interruption_min_words)
        if self.tts is not None:
            self.tts.bind_fence(new_fence)
        if not new_fence.matches(old_fence):
            self.publish_assistant_state("interrupted")
            self.set_interaction_phase(
                InteractionPhase.INTERRUPTED,
                cause=f"interrupt:{cause}",
            )
            # User-facing yield when we actually stop mid-reply for the owner.
            if (
                create_user_turn
                and mid_reply
                and cause
                not in {
                    "user_button",
                    "stop_response",
                    "rtc_recovered",
                }
            ):
                self._spawn(
                    self._maybe_say_interrupt_yield(cause=cause),
                    name="interrupt-yield",
                )
            else:
                self._restore_listen_after_control(cause=f"interrupt_no_yield:{cause}")
        return new_fence

    async def on_playback_started(self) -> None:
        if self._set_interruption_min_words is not None:
            self._set_interruption_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
        self._last_playback_completed_ns = None
        self._playback_started_ns = time.monotonic_ns()
        if self._playback_fence is None or not self._playback_fence.matches(self.fence):
            self._playback_fence = self.fence
            self._played_assistant_text = ""
        await self.on_assistant_speaking(self._pending_assistant_text)
        self.mark_audio_event("playback_started")
        self.set_interaction_phase(InteractionPhase.SPEAKING, cause="playback_started")

    async def on_playback_finished(
        self,
        *,
        playback_position_s: float,
        interrupted: bool,
        synchronized_transcript: str | None,
    ) -> None:
        if interrupted:
            interrupted_from = self._playback_fence or self.fence
            combined_heard = self._combine_played_text(
                self._played_assistant_text,
                synchronized_transcript,
            )
            await self.on_real_interrupt(
                cause="livekit_playback_interrupted",
                # Completed segments are facts; if alignment is unavailable for the
                # interrupted segment, conservatively omit that partial segment.
                synchronized_transcript=combined_heard,
            )
            finalized = await self.orchestrator.finalize_interrupted_playback(
                interrupted_from=interrupted_from,
                synchronized_transcript=combined_heard,
            )
            if finalized is None:
                return
            heard, event_fence = finalized
            self._was_speaking = False
            self._pending_assistant_text = ""
            self._played_assistant_text = heard
            self._playback_fence = None
            if heard:
                self.publish_transcript(
                    speaker="assistant",
                    text=heard,
                    final=True,
                    heard=True,
                    fence=event_fence,
                )
        else:
            _ = playback_position_s
            self._played_assistant_text = self._combine_played_text(
                self._played_assistant_text,
                synchronized_transcript,
            )
            # Always clear speaking flag. Fixed session.say(add_to_chat_ctx=False)
            # may never emit conversation_item_added → on_assistant_reply_completed,
            # which used to leave _was_speaking stuck and block enroll PCM forever.
            self._was_speaking = False
            self._last_playback_completed_ns = time.monotonic_ns()
            if self._played_assistant_text:
                self.publish_transcript(
                    speaker="assistant",
                    text=self._played_assistant_text,
                    final=False,
                    heard=True,
                )

    async def on_assistant_reply_completed(self, text: str) -> None:
        """Commit one fully played reply after LiveKit adds its conversation item."""
        if self.orchestrator.state is not ConversationState.SPEAKING:
            return
        # LiveKit emits this item only after uninterrupted playout completes.
        heard = text
        heard = await self.orchestrator.finish_livekit_playback(
            tools_active=self._pending_tool_results > 0,
            synchronized_transcript=heard,
        )
        self._was_speaking = False
        self._pending_assistant_text = ""
        self._played_assistant_text = heard
        self._playback_fence = None
        self._last_playback_completed_ns = time.monotonic_ns()
        self.publish_assistant_state(
            "tool_waiting" if self._pending_tool_results > 0 else "listening"
        )
        if heard:
            self.publish_transcript(
                speaker="assistant",
                text=heard,
                final=True,
                heard=True,
            )

    def should_ignore_post_playback_backchannel(
        self,
        text: str,
        *,
        now_ns: int | None = None,
    ) -> bool:
        return self.post_playback_guard_reason(text, now_ns=now_ns) is not None

    def post_playback_guard_reason(
        self,
        text: str,
        *,
        now_ns: int | None = None,
    ) -> str | None:
        completed_ns = self._last_playback_completed_ns
        if completed_ns is None:
            return None
        elapsed_ms = ((now_ns or time.monotonic_ns()) - completed_ns) // 1_000_000
        if not 0 <= elapsed_ms <= POST_PLAYBACK_ECHO_GUARD_MS:
            return None
        reason = self.input_guard.guarded_reason(
            text,
            duration_ms=int(elapsed_ms),
            assistant_text=self._played_assistant_text,
        )
        if reason == "backchannel" or self.input_guard.enabled:
            return reason
        return None

    @staticmethod
    def _combine_played_text(existing: str, segment: str | None) -> str:
        if not segment:
            return existing
        if segment.startswith(existing):
            return segment
        if existing.endswith(segment):
            return existing
        return existing + segment

    def configure_deep_path(self, client: Any) -> None:
        """Run DeepSeek Pro behind TaskManager and summarize through the fast model."""
        self._deep_client = client

        async def _deep_reasoning(
            args: dict[str, Any],
            cancel_event: asyncio.Event,
        ) -> dict[str, Any]:
            fence = args["fence"]
            messages = self.orchestrator.context.build_messages(current_user_final="")
            deep_text = ""
            async for result_fence, chunk in client.stream_deep(messages, fence=fence):
                if cancel_event.is_set() or not result_fence.matches(fence):
                    return {"error": "cancelled"}
                deep_text += chunk.content
            if cancel_event.is_set():
                return {"error": "cancelled"}
            summary_messages = [
                {
                    "role": "system",
                    "content": "把结果压缩成一到三句自然中文口语，只保留结论，不输出Markdown。",
                },
                {"role": "user", "content": deep_text[:12000]},
            ]
            summary = ""
            async for result_fence, chunk in client.stream_fast(
                summary_messages,
                fence=fence,
            ):
                if cancel_event.is_set() or not result_fence.matches(fence):
                    return {"error": "cancelled"}
                summary += chunk.content
            return {"summary": summary}

        self.orchestrator.task_manager.register(
            ToolSpec(
                name="deep_reasoning",
                description="DeepSeek Pro background reasoning",
                input_schema={"type": "object"},
                cancellable=True,
                idempotent=True,
                timeout_s=90.0,
            ),
            _deep_reasoning,
        )

    async def _start_deep_task(self, fence: GenerationFence) -> None:
        rec = await self.orchestrator.task_manager.start(
            "deep_reasoning",
            {"fence": fence},
            fence,
        )
        self._pending_tool_results += 1

        async def _deliver() -> None:
            try:
                try:
                    payload = await rec.task
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.warning("deep background task failed", exc_info=True)
                    return
                while fence.matches(self.fence) and self.orchestrator.state in {
                    ConversationState.THINKING,
                    ConversationState.SPEAKING,
                    ConversationState.INTERRUPTION_PENDING,
                }:
                    await asyncio.sleep(0.05)
                if not fence.matches(self.fence):
                    self.orchestrator.gate_tool_result(fence, payload)
                    return
                accepted = await self.orchestrator.accept_background_result(fence, payload)
                if not isinstance(accepted, dict):
                    return
                spoken = spoken_result_summarizer(accepted)
                if self._result_speaker is not None and "error" not in accepted:
                    # P1-6: tool spoken result stays on the same generation fence.
                    if self.tts is not None:
                        self.tts.bind_fence(fence)
                    self.update_pending_assistant_text(spoken)
                    self.mark_audio_event(
                        "tool_result_same_generation",
                        detail={
                            "turn_id": fence.turn_id,
                            "generation_id": fence.generation_id,
                            "tool_epoch": fence.tool_epoch,
                        },
                    )
                    self._result_speaker(spoken)
                    self.publish_assistant_state("thinking")
            finally:
                self._pending_tool_results = max(0, self._pending_tool_results - 1)

        self._spawn(_deliver(), name=f"deep-result-{rec.tool_task_id}")

    def attach_session_events(self, session: Any) -> None:
        """Subscribe to public AgentSession events; playout is attached separately."""

        interruption = getattr(getattr(session, "options", None), "interruption", None)
        base_min_words = (
            int(interruption.get("min_words", 0)) if isinstance(interruption, dict) else 0
        )
        false_timeout = (
            float(interruption.get("false_interruption_timeout") or 0.8)
            if isinstance(interruption, dict)
            else 0.8
        )
        false_resume_task: asyncio.Task[Any] | None = None

        def _set_min_words(value: int) -> None:
            if isinstance(interruption, dict):
                interruption["min_words"] = value

        self._set_interruption_min_words = _set_min_words
        self._base_interruption_min_words = base_min_words

        def _restore_audio() -> None:
            self.publish_assistant_audio("restore", gain=1.0)

        def _cancel_false_resume() -> None:
            nonlocal false_resume_task
            if false_resume_task is not None:
                false_resume_task.cancel()
                false_resume_task = None

        ducked = False

        def _on_user_state(ev: Any) -> None:
            nonlocal false_resume_task, ducked
            state = str(getattr(ev, "new_state", ""))
            if state == "speaking":
                _cancel_false_resume()
                decision = self.on_user_voice_started()
                if decision is PlaybackInputDecision.WAIT:
                    _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                    self.mark_audio_event("barge_in_detected")
                    # Mild duck only: 0.25 sounded like random loud/soft swings.
                    self.publish_assistant_audio("duck", gain=0.55)
                    ducked = True
                else:
                    _set_min_words(base_min_words)
                return
            if state == "listening":
                # Always restore ducked gain when user stops (P1 duck-first).
                if ducked:
                    _restore_audio()
                    ducked = False
                if (
                    self.input_guard.candidate_active
                    and self.input_guard.candidate_during_playback
                    and self.input_guard.candidate_decision is PlaybackInputDecision.WAIT
                ):
                    candidate_started = self.input_guard.candidate_started_ns

                    async def _resume_false_interruption() -> None:
                        await asyncio.sleep(false_timeout)
                        if (
                            self.input_guard.candidate_active
                            and self.input_guard.candidate_started_ns == candidate_started
                            and self.input_guard.candidate_decision
                            is PlaybackInputDecision.WAIT
                        ):
                            _restore_audio()
                            _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                            self.orchestrator.metrics.inc_false_interruptions()

                    false_resume_task = self._spawn(
                        _resume_false_interruption(),
                        name="duplex-false-interruption-resume",
                    )

        def _on_agent_state(ev: Any) -> None:
            state = getattr(ev, "new_state", None) or getattr(ev, "state", None)
            if state is None and isinstance(ev, str):
                state = ev
            state_s = str(state)
            # Mic/VAD/ASR must remain open while assistant speaks.
            if state_s in ("speaking", "thinking"):
                assert self.orchestrator.mic_open and self.orchestrator.asr_active
            mapped = {
                "initializing": "connecting",
                "idle": "listening",
                "listening": "listening",
                # Quiet planning before first audio: explicit thinking_silent phase.
                "thinking": "thinking_silent",
                "speaking": "speaking",
            }.get(state_s)
            if mapped is not None:
                # Do not clobber an active listener backchannel with idle/listening.
                if (
                    mapped == "listening"
                    and self.interaction_phase is InteractionPhase.BACKCHANNEL
                ):
                    return
                self.publish_assistant_state(mapped)

        def _on_user_transcript(ev: Any) -> None:
            text = str(getattr(ev, "transcript", "") or "")
            if text:
                final = bool(getattr(ev, "is_final", False))
                decision = self.observe_user_transcript(text, final=final)
                if self.input_guard.candidate_during_playback:
                    if decision is PlaybackInputDecision.WAIT:
                        return
                    _cancel_false_resume()
                    if decision is PlaybackInputDecision.IGNORE:
                        logger.info(
                            "playback_input_ignored reason=%s",
                            self.input_guard.candidate_reason or "playback_noise",
                        )
                        _restore_audio()
                        _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                        return
                    _set_min_words(base_min_words)
                if not final:
                    self.publish_transcript(
                        speaker="user",
                        text=text,
                        final=False,
                    )

        def _on_conversation_item(ev: Any) -> None:
            item = getattr(ev, "item", None)
            role = getattr(item, "role", None) if item is not None else None
            if str(role) == "user":
                # Only raise min_words while assistant is mid-reply. Always
                # locking to 1000 after every user item left the session deaf
                # after「停一下」(orphan FINAL + blocked next turn).
                if self._was_speaking or self.interaction_phase in {
                    InteractionPhase.SPEAKING,
                    InteractionPhase.THINKING_SILENT,
                }:
                    _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                else:
                    _set_min_words(base_min_words)
                return
            if str(role) != "assistant":
                return
            text = ""
            if item is not None:
                content = getattr(item, "text_content", None)
                if callable(content):
                    text = str(content() or "")
                elif content is not None:
                    text = str(content)
            if text and not bool(getattr(item, "interrupted", False)):
                self._spawn(
                    self.on_assistant_reply_completed(text),
                    name="duplex-assistant-reply-completed",
                )

        def _make_unsub(event_name: str, event_handler: Any) -> Callable[[], None]:
            def _unsub() -> None:
                session.off(event_name, event_handler)

            return _unsub

        for name, handler in (
            ("agent_state_changed", _on_agent_state),
            ("user_state_changed", _on_user_state),
            ("user_input_transcribed", _on_user_transcript),
            ("conversation_item_added", _on_conversation_item),
        ):
            try:
                session.on(name, handler)
                self._unsubscribers.append(_make_unsub(name, handler))
            except Exception:
                logger.warning("could not attach session event %s", name, exc_info=True)

    def attach_playback_events(self, audio_output: Any) -> None:
        """Use LiveKit's actual playout position and synchronized transcript as facts."""

        def _on_started(_ev: Any) -> None:
            self._spawn(self.on_playback_started(), name="duplex-playback-started")

        def _on_finished(ev: Any) -> None:
            self._spawn(
                self.on_playback_finished(
                    playback_position_s=float(getattr(ev, "playback_position", 0.0) or 0.0),
                    interrupted=bool(getattr(ev, "interrupted", False)),
                    synchronized_transcript=getattr(ev, "synchronized_transcript", None),
                ),
                name="duplex-playback-finished",
            )

        for name, handler in (
            ("playback_started", _on_started),
            ("playback_finished", _on_finished),
        ):
            audio_output.on(name, handler)

            def _unsub(
                event_name: str = name,
                event_handler: Any = handler,
            ) -> None:
                audio_output.off(event_name, event_handler)

            self._unsubscribers.append(_unsub)

    async def close(self) -> None:
        self.cancel_listener_cue()
        self._set_interruption_min_words = None
        for unsub in self._unsubscribers:
            with contextlib.suppress(Exception):
                unsub()
        for task in tuple(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await self.orchestrator.close()
        if self._deep_client is not None:
            await self._deep_client.aclose()


def _needs_deep_path(text: str) -> bool:
    if len(text) >= 80:
        return True
    return any(
        hint in text
        for hint in (
            "详细比较",
            "深入分析",
            "制定计划",
            "搜索",
            "检索",
            "查资料",
            "文件分析",
            "多个方案",
        )
    )
