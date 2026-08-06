from __future__ import annotations

import pytest
from services.agent.src.orchestration.interaction_plane import (
    InteractionEvent,
    InteractionPlane,
    InteractionSnapshot,
)
from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
from services.agent.src.orchestration.utterance_router import route_utterance


def test_vad_start_ducks_and_prewarms_without_waiting_for_turn_commit() -> None:
    decision = InteractionPlane().decide(
        InteractionSnapshot(
            event=InteractionEvent.VAD_START,
            assistant_speaking=True,
            has_speech_energy=True,
        )
    )

    assert decision.duck_output
    assert decision.start_prefetch
    assert decision.warm_fast_model
    assert not decision.cancel_generation


def test_hard_stop_keyword_cancels_without_asr_final() -> None:
    decision = InteractionPlane().decide(
        InteractionSnapshot(
            event=InteractionEvent.KEYWORD,
            assistant_speaking=True,
            text="停一下",
            utterance_route=route_utterance("停一下"),
            keyword_hard_stop=True,
            keyword_confidence=0.01,
        )
    )

    assert decision.cancel_generation
    assert not decision.persist_turn
    assert decision.acknowledgement == "嗯，你说。"


def test_hard_stop_wire_decision_is_not_reinterpreted_from_text() -> None:
    decision = InteractionPlane().decide(
        InteractionSnapshot(
            event=InteractionEvent.KEYWORD,
            assistant_speaking=True,
            text="provider-token",
            keyword_hard_stop=True,
        )
    )

    assert decision.cancel_generation


@pytest.mark.parametrize(
    ("text", "elapsed_ms", "final", "should_cancel"),
    [
        ("天气", 200, True, False),
        ("天气", 500, True, True),
        ("我想问一下天气", 100, False, True),
    ],
)
def test_ordinary_barge_in_requires_duration_or_semantic_evidence(
    text: str,
    elapsed_ms: int,
    final: bool,
    should_cancel: bool,
) -> None:
    decision = InteractionPlane().decide(
        InteractionSnapshot(
            event=InteractionEvent.TRANSCRIPT,
            assistant_speaking=True,
            text=text,
            elapsed_ms=elapsed_ms,
            final=final,
            has_speech_energy=True,
            playback_decision=PlaybackInputDecision.ACCEPT,
            utterance_route=route_utterance(text),
        )
    )

    assert decision.cancel_generation is should_cancel
    assert decision.duck_output is (not should_cancel)


def test_backchannel_resumes_without_persistence_or_delegation() -> None:
    decision = InteractionPlane().decide(
        InteractionSnapshot(
            event=InteractionEvent.TRANSCRIPT,
            assistant_speaking=True,
            text="嗯",
            elapsed_ms=300,
            final=True,
            has_speech_energy=True,
            playback_decision=PlaybackInputDecision.ACCEPT,
            turn_committed=True,
        )
    )

    assert decision.backchannel
    assert decision.continue_output
    assert not decision.cancel_generation
    assert not decision.persist_turn
    assert not decision.start_delegation


def test_authoritative_media_final_counts_as_semantic_barge_in_evidence() -> None:
    decision = InteractionPlane().decide(
        InteractionSnapshot(
            event=InteractionEvent.TRANSCRIPT,
            assistant_speaking=True,
            text="天气",
            elapsed_ms=20,
            final=True,
            has_speech_energy=True,
            semantic_evidence=True,
            playback_decision=PlaybackInputDecision.ACCEPT,
        )
    )

    assert decision.cancel_generation


def test_turn_detector_only_gates_commit_and_delegation_not_prefetch() -> None:
    plane = InteractionPlane()
    provisional = plane.decide(
        InteractionSnapshot(
            event=InteractionEvent.TRANSCRIPT,
            assistant_speaking=False,
            text="帮我查天气",
            final=False,
            turn_committed=False,
        )
    )
    committed = plane.decide(
        InteractionSnapshot(
            event=InteractionEvent.TRANSCRIPT,
            assistant_speaking=False,
            text="帮我查天气",
            final=True,
            turn_committed=True,
        )
    )

    assert provisional.start_prefetch and provisional.warm_fast_model
    assert not provisional.persist_turn and not provisional.start_delegation
    assert committed.persist_turn and committed.start_delegation
