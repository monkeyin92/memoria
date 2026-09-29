"""``VoiceFloorState`` / ``FloorSnapshot``: one floor owner, typed predicates."""

from __future__ import annotations

import itertools
import logging

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.state_machine import ConversationState, InteractionPhase
from services.agent.src.voice_floor import FloorSnapshot, VoiceFloorState
from services.agent.tests.unit.runtime_state_helpers import set_floor

_COMBINATIONS = list(
    itertools.product(ConversationState, InteractionPhase, (False, True), (False, True))
)


@pytest.mark.parametrize(("state", "phase", "speaking", "fresh"), _COMBINATIONS)
def test_predicates_match_the_replaced_call_site_expressions(
    state: ConversationState,
    phase: InteractionPhase,
    speaking: bool,
    fresh: bool,
) -> None:
    floor = FloorSnapshot(
        conversation_state=state,
        phase=phase,
        assistant_speaking=speaking,
        fresh_user_speech=fresh,
    )
    # media_session_connection / media_session_input (pre-batch-5 text)
    assert floor.interruptible is (state.name in ("SPEAKING", "INTERRUPTION_PENDING"))
    # media_session_standby._resume_owner_silence_after_reconnect
    assert floor.listening_or_connecting is (phase.value in {"connecting", "listening"})
    # media_session_standby owner-silence pause
    assert floor.assistant_holds_floor is (
        speaking or phase.value in {"thinking_silent", "speaking", "backchannel", "interrupted"}
    )
    # DuplexRuntime.output_floor_allows_assistant
    assert floor.output_floor_allows_assistant is (
        not fresh and phase not in {InteractionPhase.USER_SPEAKING, InteractionPhase.INTERRUPTED}
    )
    state_speaking = state in {
        ConversationState.SPEAKING,
        ConversationState.INTERRUPTION_PENDING,
    }
    assert floor.divergent is not (
        state_speaking == (phase is InteractionPhase.SPEAKING) == speaking
    )


def test_update_keeps_every_field_it_is_not_given() -> None:
    floor = VoiceFloorState()
    floor.update(assistant_speaking=True, pending_assistant_text="回答")
    floor.update(played_assistant_text="已播放")
    assert floor.assistant_speaking is True
    assert floor.pending_assistant_text == "回答"
    assert floor.echo_reference_text == "回答"
    floor.update(pending_assistant_text="", last_playback_completed_ns=None)
    assert floor.echo_reference_text == "已播放"
    assert floor.set_phase(InteractionPhase.CONNECTING) is None
    assert floor.set_phase(InteractionPhase.LISTENING) is InteractionPhase.CONNECTING
    assert floor.phase is InteractionPhase.LISTENING


@pytest.mark.asyncio
async def test_published_state_goes_through_the_single_phase_writer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = DuplexRuntime.create(session_id="ses_floor_writer")
    with caplog.at_level(logging.INFO, logger="services.agent"):
        runtime.publish_assistant_state("thinking")
        runtime.publish_assistant_state("thinking_silent")
        runtime.set_interaction_phase(InteractionPhase.LISTENING, cause="unit")
    phase_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("interaction_phase ")
    ]
    assert phase_lines == [
        "interaction_phase from=connecting to=thinking_silent cause=publish:thinking "
        "session_id=ses_floor_writer turn_id=0 generation_id=0",
        "interaction_phase from=thinking_silent to=listening cause=unit "
        "session_id=ses_floor_writer turn_id=0 generation_id=0",
    ]
    assert runtime.interaction_phase is InteractionPhase.LISTENING
    assert runtime.floor.phase is InteractionPhase.LISTENING
    await runtime.close()


@pytest.mark.asyncio
async def test_divergence_counter_counts_a_latch_without_a_state_transition() -> None:
    """``on_assistant_speaking`` outside THINKING/SPEAKING sets the latch and
    phase SPEAKING without a ``ConversationState`` transition: counted once."""

    runtime = DuplexRuntime.create(session_id="ses_floor_divergence")
    metrics = runtime.orchestrator.metrics
    labels = {
        "conversation_state": "connecting",
        "phase": "speaking",
        "assistant_speaking": "true",
    }
    runtime.set_interaction_phase(InteractionPhase.LISTENING, cause="unit")
    assert "voice_floor_divergence_total" not in metrics.labeled

    assert await runtime.on_assistant_speaking("你好。") is True
    assert runtime.orchestrator.state is ConversationState.CONNECTING
    floor = runtime.floor
    assert floor.assistant_speaking and floor.divergent and not floor.interruptible
    assert metrics.get("voice_floor_divergence_total", labels) == 1.0
    # Same-phase writes do not re-count.
    assert await runtime.on_assistant_speaking("你好。") is True
    assert metrics.get("voice_floor_divergence_total", labels) == 1.0
    assert b"voice_floor_divergence_total" in metrics.render_prometheus()
    await runtime.close()


def test_floor_test_helper_writes_through_the_mutation_api() -> None:
    runtime = DuplexRuntime.create(session_id="ses_floor_helper")
    set_floor(runtime, assistant_speaking=True, fresh_user_speech=True)
    assert runtime.assistant_speaking is True
    assert runtime.floor.assistant_holds_floor is True
    assert runtime.output_floor_allows_assistant is False
