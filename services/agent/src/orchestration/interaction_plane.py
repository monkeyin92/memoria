"""Single, side-effect-free decision plane for continuous user interaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from services.agent.src.orchestration.interruption_guard import (
    ChineseInterruptionGuard,
    InterruptDecision,
    PlaybackInputDecision,
    is_backchannel,
)
from services.agent.src.orchestration.utterance_router import UtteranceRoute, route_utterance


class InteractionEvent(StrEnum):
    VAD_START = "vad_start"
    KEYWORD = "keyword"
    TRANSCRIPT = "transcript"


@dataclass(frozen=True, slots=True)
class InteractionSnapshot:
    event: InteractionEvent
    assistant_speaking: bool
    text: str = ""
    elapsed_ms: int = 0
    final: bool = False
    has_speech_energy: bool = False
    looks_like_noise_only: bool = False
    guarded_reason: str | None = None
    playback_decision: PlaybackInputDecision = PlaybackInputDecision.ACCEPT
    utterance_route: UtteranceRoute | None = None
    keyword_hard_stop: bool = False
    keyword_confidence: float = 0.0
    semantic_evidence: bool = False
    turn_committed: bool = False


@dataclass(frozen=True, slots=True)
class InteractionDecision:
    reason: str
    duck_output: bool = False
    cancel_generation: bool = False
    continue_output: bool = False
    backchannel: bool = False
    persist_turn: bool = False
    start_prefetch: bool = False
    warm_fast_model: bool = False
    start_delegation: bool = False
    acknowledgement: str | None = None


@dataclass(slots=True)
class InteractionPlane:
    """Compose existing Router/guard evidence without executing effects."""

    interruption_guard: ChineseInterruptionGuard = field(default_factory=ChineseInterruptionGuard)
    sustained_speech_ms: int = 250

    def decide(self, snapshot: InteractionSnapshot) -> InteractionDecision:
        if snapshot.event is InteractionEvent.VAD_START:
            return InteractionDecision(
                reason="vad_duck" if snapshot.assistant_speaking else "vad_prefetch",
                duck_output=snapshot.assistant_speaking,
                start_prefetch=True,
                warm_fast_model=True,
            )

        route = snapshot.utterance_route or route_utterance(snapshot.text)
        if snapshot.event is InteractionEvent.KEYWORD:
            # Edge owns acoustic and keyword semantics.  Once signed on the
            # wire, hard_stop is the only decision both planes consume.
            hard_stop = snapshot.keyword_hard_stop
            return InteractionDecision(
                reason="keyword_hard_stop" if hard_stop else "keyword_unconfirmed",
                duck_output=snapshot.assistant_speaking,
                cancel_generation=hard_stop,
                acknowledgement=route.ack_phrase if hard_stop else None,
            )

        has_text = bool(route.normalized_text)
        prefetch = has_text
        if snapshot.assistant_speaking and snapshot.guarded_reason is not None:
            return InteractionDecision(
                reason=snapshot.guarded_reason,
                continue_output=True,
                backchannel=snapshot.guarded_reason == "backchannel",
            )
        if snapshot.assistant_speaking and is_backchannel(
            snapshot.text,
            duration_ms=snapshot.elapsed_ms,
        ):
            return InteractionDecision(
                reason="backchannel",
                continue_output=True,
                backchannel=True,
                start_prefetch=False,
            )
        if snapshot.playback_decision is PlaybackInputDecision.IGNORE:
            return InteractionDecision(
                reason="playback_input_ignored",
                continue_output=snapshot.assistant_speaking,
            )
        if snapshot.assistant_speaking and snapshot.playback_decision is PlaybackInputDecision.WAIT:
            return InteractionDecision(
                reason="barge_in_evidence_pending",
                duck_output=True,
                start_prefetch=has_text,
                warm_fast_model=has_text,
            )
        if route.should_interrupt:
            return InteractionDecision(
                reason=route.reason,
                duck_output=snapshot.assistant_speaking,
                cancel_generation=snapshot.assistant_speaking,
                persist_turn=route.enter_chat and snapshot.turn_committed,
                start_prefetch=route.enter_chat,
                warm_fast_model=route.enter_chat,
                start_delegation=route.enter_chat and snapshot.turn_committed,
                acknowledgement=route.ack_phrase,
            )
        if snapshot.assistant_speaking:
            evidence = self.interruption_guard.evaluate(
                elapsed_ms=snapshot.elapsed_ms,
                asr_text=snapshot.text,
                has_speech_energy=snapshot.has_speech_energy,
                looks_like_noise_only=snapshot.looks_like_noise_only,
            )
            if evidence in {InterruptDecision.RESUME, InterruptDecision.FALSE_INTERRUPTION}:
                return InteractionDecision(
                    reason=evidence.value,
                    continue_output=True,
                )
            confirmed = (
                snapshot.semantic_evidence
                or evidence is InterruptDecision.CONFIRM_INTERRUPT
                or (snapshot.final and snapshot.elapsed_ms >= self.sustained_speech_ms and has_text)
            )
            return InteractionDecision(
                reason="ordinary_barge_in" if confirmed else "barge_in_evidence_pending",
                duck_output=not confirmed,
                cancel_generation=confirmed,
                persist_turn=confirmed and snapshot.turn_committed,
                start_prefetch=prefetch,
                warm_fast_model=prefetch,
                start_delegation=confirmed and snapshot.turn_committed,
            )
        return InteractionDecision(
            reason="turn_committed" if snapshot.turn_committed else "turn_provisional",
            persist_turn=snapshot.turn_committed and route.enter_chat,
            start_prefetch=prefetch,
            warm_fast_model=prefetch,
            start_delegation=snapshot.turn_committed and route.enter_chat,
        )
