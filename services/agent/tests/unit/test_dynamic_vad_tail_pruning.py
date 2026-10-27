from __future__ import annotations

import time
from typing import Any

import pytest
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.src.voice_core.speech_timeline import ASRResult
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _seed_pending_media_turn,
    open_bridge_connection,
)


@pytest.fixture
def identity() -> SessionIdentity:
    return SessionIdentity(
        "d07-vad-session-1",
        account_id="acc-1",
        device_id="device-1",
        client_type="device",
        subject_id="subj-1",
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_version=1,
        stream_epoch=1,
    )


async def _setup_registry_and_context(
    identity: SessionIdentity,
    *,
    grace_s: float = 0.9,
    min_grace_s: float = 0.7,
    max_grace_s: float = 1.1,
    complete_grace_s: float = 0.5,
    incomplete_grace_s: float = 1.2,
    initial_text: str = "",
) -> tuple[MediaVoiceCoreRegistry, Any]:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    open_bridge_connection(bridge, identity)
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=grace_s,
        turn_endpoint_min_grace_s=min_grace_s,
        turn_endpoint_max_grace_s=max_grace_s,
        turn_endpoint_grace_complete_s=complete_grace_s,
        turn_endpoint_grace_incomplete_s=incomplete_grace_s,
    )
    if initial_text:
        context = await _seed_pending_media_turn(registry, identity, text=initial_text)
    else:
        context = await registry.open_session(identity)
    return registry, context


async def test_adaptive_endpoint_grace_complete(identity: SessionIdentity) -> None:
    """Complete short sentence prunes grace from 0.9s to 0.5s."""
    registry, context = await _setup_registry_and_context(identity, initial_text="讲个故事吧")
    grace = registry._adaptive_endpoint_grace(context)
    assert grace == pytest.approx(0.5, abs=0.001)


async def test_adaptive_endpoint_grace_incomplete(identity: SessionIdentity) -> None:
    """Incomplete clause extends grace from 0.9s to 1.2s."""
    registry, context = await _setup_registry_and_context(identity, initial_text="让我想想")
    grace = registry._adaptive_endpoint_grace(context)
    assert grace == pytest.approx(1.2, abs=0.001)


async def test_adaptive_endpoint_grace_neutral(identity: SessionIdentity) -> None:
    """Neutral/longer utterance keeps standard configured adaptive grace (0.9s)."""
    long_text = "我今天在学校吃了一个特别特别甜而且味道很香的大苹果"
    registry, context = await _setup_registry_and_context(identity, initial_text=long_text)
    grace = registry._adaptive_endpoint_grace(context)
    assert grace == pytest.approx(0.9, abs=0.001)


async def test_adaptive_endpoint_grace_zero_grace_preserved(identity: SessionIdentity) -> None:
    """Configured zero grace is preserved across all semantic categories."""
    registry, context = await _setup_registry_and_context(
        identity, grace_s=0.0, initial_text="讲个故事吧"
    )
    assert registry._adaptive_endpoint_grace(context) == 0.0

    registry, context = await _setup_registry_and_context(
        identity, grace_s=0.0, initial_text="让我想想"
    )
    assert registry._adaptive_endpoint_grace(context) == 0.0


async def test_adaptive_endpoint_grace_fast_test_grace_preserved(
    identity: SessionIdentity,
) -> None:
    """Fast test grace (e.g. 0.01s < min_grace) is returned directly."""
    registry, context = await _setup_registry_and_context(
        identity, grace_s=0.01, min_grace_s=0.7, initial_text="讲个故事吧"
    )
    assert registry._adaptive_endpoint_grace(context) == 0.01


async def test_reactive_grace_pruned_on_complete_partial(identity: SessionIdentity) -> None:
    """When an endpoint is armed, an incoming complete partial prunes grace to 0.5s."""
    registry, context = await _setup_registry_and_context(identity)

    # Arm endpoint without text (neutral)
    endpoint = 16_000
    now = time.monotonic()
    context.pending.turn_start_sample = 0
    context.pending.turn_endpoint_sample = endpoint
    context.pending.child_speech_ended_mono = now
    registry._schedule_turn_commit(context)

    initial_deadline = context.pending.turn_endpoint_grace_deadline
    assert initial_deadline is not None
    assert initial_deadline == pytest.approx(now + 0.9, abs=0.05)

    # Complete partial arrives 50ms later
    partial = ASRResult(
        task_epoch=1,
        sentence_id="partial-1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=endpoint,
        text="讲个故事吧",
        is_final=False,
        stream_epoch=identity.stream_epoch,
    )
    registry._observe_partial_asr_result(context, partial)

    # Grace deadline must be pruned to speech_end + 0.5s
    pruned_deadline = context.pending.turn_endpoint_grace_deadline
    assert pruned_deadline is not None
    assert pruned_deadline < initial_deadline
    assert pruned_deadline == pytest.approx(now + 0.5, abs=0.05)


async def test_reactive_grace_extended_on_incomplete_partial(identity: SessionIdentity) -> None:
    """When an endpoint is armed, an incoming incomplete partial extends grace to 1.2s."""
    registry, context = await _setup_registry_and_context(identity)

    endpoint = 16_000
    now = time.monotonic()
    context.pending.turn_start_sample = 0
    context.pending.turn_endpoint_sample = endpoint
    context.pending.child_speech_ended_mono = now
    registry._schedule_turn_commit(context)

    initial_deadline = context.pending.turn_endpoint_grace_deadline
    assert initial_deadline is not None
    assert initial_deadline == pytest.approx(now + 0.9, abs=0.05)

    # Incomplete partial arrives
    partial = ASRResult(
        task_epoch=1,
        sentence_id="partial-1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=endpoint,
        text="让我想想",
        is_final=False,
        stream_epoch=identity.stream_epoch,
    )
    registry._observe_partial_asr_result(context, partial)

    # Grace deadline must be extended to speech_end + 1.2s
    extended_deadline = context.pending.turn_endpoint_grace_deadline
    assert extended_deadline is not None
    assert extended_deadline > initial_deadline
    assert extended_deadline == pytest.approx(now + 1.2, abs=0.05)


async def test_reactive_grace_ignored_when_grace_disabled(identity: SessionIdentity) -> None:
    """Disabled grace (turn_endpoint_grace_s=0.0) never alters deadlines on partials."""
    registry, context = await _setup_registry_and_context(identity, grace_s=0.0)

    endpoint = 16_000
    now = time.monotonic()
    context.pending.turn_start_sample = 0
    context.pending.turn_endpoint_sample = endpoint
    context.pending.child_speech_ended_mono = now
    registry._schedule_turn_commit(context)

    # Deadline was now + 0.0
    initial_deadline = context.pending.turn_endpoint_grace_deadline
    assert initial_deadline is not None

    partial = ASRResult(
        task_epoch=1,
        sentence_id="partial-1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=endpoint,
        text="讲个故事吧",
        is_final=False,
        stream_epoch=identity.stream_epoch,
    )
    registry._observe_partial_asr_result(context, partial)

    assert context.pending.turn_endpoint_grace_deadline == initial_deadline
