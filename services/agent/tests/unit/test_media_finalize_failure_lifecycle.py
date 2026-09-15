"""ASR faults must not strand or erase the authoritative input lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from services.agent.src.orchestration.conversation_projection import ProjectionPatch
from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.media_session_state import MediaVoiceSessionState
from services.agent.src.voice_core.speech_timeline import ASRResult
from services.agent.tests.unit.test_media_session import _verified_owner_decision
from services.agent.tests.unit.test_media_standby_races import (
    _device_registry,
    _expire_owner_timer,
    _expire_speech_watchdog,
    _user_turns,
    _vad,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_grace", [False, True])
@pytest.mark.parametrize("owner_classified", [False, True])
async def test_finalize_fault_resumes_only_the_remaining_silence_budget(
    monkeypatch: pytest.MonkeyPatch, with_grace: bool, owner_classified: bool,
) -> None:
    registry, context, provider, runtime, identity = await _device_registry(
        "finalize-fault-resumes-budget"
    )
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session

    async def fail(_identity: SessionIdentity) -> tuple[ASRResult, ...]:
        raise RuntimeError("injected ASR finalization fault")

    monkeypatch.setattr(provider, "finalize_speech_segment", fail, raising=False)
    try:
        registry._cancel_owner_silence_timer(context, preserve_remaining=False)
        context.owner_silence_remaining_s = 7.5
        registry._arm_owner_silence_timer(context, reset=False)
        if with_grace:
            context.admitted_input_stream_epoch = identity.stream_epoch
            await _expire_owner_timer(registry, context)
        await registry.on_speech_segment(session, _vad(identity))
        remaining = context.owner_silence_remaining_s
        assert context.max_user_speech_task is not None
        assert context.owner_silence_task is None
        if owner_classified:
            # Even a classification already attached to this failed input
            # must not turn provider failure into a fresh silence allowance.
            runtime._speaker_class = "owner"
            runtime._speaker_decision = _verified_owner_decision()
        context.asr.last_sent_sample = 960
        await registry.on_speech_segment(session, _vad(identity, start=960, final=True))

        assert context.ingress.provider_failed
        assert context.turn_start_sample is None
        assert context.active_vad_start_sample is None
        assert context.max_user_speech_task is None
        assert context.turn_endpoint_timeout_handle is None
        # No later listening event is injected: the fault path itself must
        # restore the existing budget instead of waiting for another callback.
        assert context.owner_silence_task is not None
        assert not context.owner_silence_task.done()
        assert context.owner_silence_deadline is not None
        assert context.owner_silence_remaining_s == remaining
        assert context.owner_silence_grace_used is with_grace
        assert _user_turns(runtime) == []
        await _expire_owner_timer(registry, context)
        assert context.closed
        assert context.standby_reason == "owner_silence_timeout"
    finally:
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_finalize_fault_publication_cannot_clear_a_new_vad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, context, provider, runtime, identity = await _device_registry(
        "finalize-fault-publication"
    )
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    entered, release = asyncio.Event(), asyncio.Event()
    original = registry._emit_projection_patch

    async def publish(ctx: MediaVoiceSessionState, patch: ProjectionPatch) -> None:
        await original(ctx, patch)
        if patch.reason == "asr_finalize_failed":
            entered.set()
            await release.wait()

    async def fail(_identity: SessionIdentity) -> tuple[ASRResult, ...]:
        raise RuntimeError("injected ASR finalization fault")

    monkeypatch.setattr(registry, "_emit_projection_patch", publish)
    monkeypatch.setattr(provider, "finalize_speech_segment", fail, raising=False)
    task: asyncio.Task[None] | None = None
    try:
        await registry.on_speech_segment(session, _vad(identity))
        context.asr.last_sent_sample = 960
        task = asyncio.create_task(
            registry.on_speech_segment(session, _vad(identity, start=960, final=True))
        )
        await asyncio.wait_for(entered.wait(), 1)
        await registry.on_speech_segment(session, _vad(identity, start=1280))
        watchdog = context.max_user_speech_task
        deadline = context.max_user_speech_deadline
        provisional = context.projection.provisional
        assert watchdog is not None
        assert context.active_vad_start_sample == 1280
        release.set()
        await asyncio.wait_for(task, 1)
        assert context.active_vad_start_sample == 1280
        assert context.max_user_speech_task is watchdog
        assert context.max_user_speech_deadline == deadline
        assert context.projection.provisional is provisional
        assert context.owner_silence_task is None
        assert _user_turns(runtime) == []
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded_by", ["new_vad", "reconnect", "terminal"])
async def test_late_finalize_fault_cannot_clear_replacement_input(
    monkeypatch: pytest.MonkeyPatch, superseded_by: str,
) -> None:
    registry, context, provider, runtime, identity = await _device_registry(
        "late-finalize-fault"
    )
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(_identity: SessionIdentity) -> tuple[ASRResult, ...]:
        entered.set()
        await release.wait()
        raise RuntimeError("injected late ASR finalization fault")

    monkeypatch.setattr(provider, "finalize_speech_segment", fail, raising=False)
    task: asyncio.Task[None] | None = None
    try:
        await registry.on_speech_segment(session, _vad(identity))
        context.asr.last_sent_sample = 960
        task = asyncio.create_task(
            registry.on_speech_segment(session, _vad(identity, start=960, final=True))
        )
        await asyncio.wait_for(entered.wait(), 1)
        if superseded_by == "terminal":
            await _expire_speech_watchdog(registry, context)
        elif superseded_by == "reconnect":
            new_identity = replace(identity, stream_epoch=identity.stream_epoch + 1)
            session = registry.bridge._open_connection(new_identity).session
            # The bridge claims the new epoch before registry reuse waits for
            # the provider's finalize lock. Release that old callback first.
        else:
            await registry.on_speech_segment(session, _vad(identity, start=1280))
        watchdog = context.max_user_speech_task
        deadline = context.max_user_speech_deadline
        provisional = context.projection.provisional
        active_start = context.active_vad_start_sample
        release.set()
        await asyncio.wait_for(task, 1)
        assert context.max_user_speech_task is watchdog
        assert context.max_user_speech_deadline == deadline
        assert context.active_vad_start_sample == active_start
        assert context.projection.provisional is provisional
        assert _user_turns(runtime) == []
        if superseded_by == "terminal":
            assert context.closed
            assert identity.session_id not in registry._sessions
            assert not context.ingress.provider_failed
        else:
            if superseded_by == "reconnect":
                assert not context.ingress.provider_failed
                await registry._reuse_session(context, new_identity)
                await registry.on_speech_segment(session, _vad(new_identity, start=1280))
                watchdog = context.max_user_speech_task
                active_start = context.active_vad_start_sample
            assert watchdog is not None
            assert not context.closed
            assert active_start == 1280
            await _expire_speech_watchdog(registry, context)
            assert context.closed
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await registry._finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_vad_during_publication", [False, True])
async def test_finalize_recovery_frame_keeps_a_live_input_or_silence_watch(
    monkeypatch: pytest.MonkeyPatch, new_vad_during_publication: bool,
) -> None:
    registry, context, provider, runtime, identity = await _device_registry(
        "finalize-recovery-next-frame"
    )
    registry.max_user_speech_duration_s = 60
    session = registry.bridge._open_connection(identity).session
    entered, release = asyncio.Event(), asyncio.Event()
    recoveries: list[int] = []
    original = registry._emit_projection_patch

    async def fail(_identity: SessionIdentity) -> tuple[ASRResult, ...]:
        raise RuntimeError("injected ASR finalization fault before recovery frame")

    async def recover(recovery_identity: SessionIdentity) -> None:
        recoveries.append(recovery_identity.stream_epoch)

    async def publish(ctx: MediaVoiceSessionState, patch: ProjectionPatch) -> None:
        await original(ctx, patch)
        if patch.reason == "audio_discontinuity" and new_vad_during_publication:
            entered.set()
            await release.wait()

    monkeypatch.setattr(provider, "finalize_speech_segment", fail, raising=False)
    monkeypatch.setattr(provider, "recover_after_failure", recover, raising=False)
    monkeypatch.setattr(registry, "_emit_projection_patch", publish)
    try:
        registry._cancel_owner_silence_timer(context, preserve_remaining=False)
        context.owner_silence_remaining_s = 7.5
        registry._arm_owner_silence_timer(context, reset=False)
        await registry.on_speech_segment(session, _vad(identity))
        context.asr.last_sent_sample = 960
        await registry.on_speech_segment(session, _vad(identity, start=960, final=True))
        assert context.ingress.provider_failed
        await registry.on_speech_segment(session, _vad(identity, start=1280))
        assert context.max_user_speech_task is not None
        assert context.owner_silence_task is None
        remaining = context.owner_silence_remaining_s
        runtime._speaker_class = "owner"
        runtime._speaker_decision = _verified_owner_decision()
        await registry.on_audio_frame(
            session, AudioFrame(identity, 1, 1600, 320, b"\x01\x00" * 320),
        )
        if new_vad_during_publication:
            await asyncio.wait_for(entered.wait(), 1)
            await registry.on_speech_segment(session, _vad(identity, start=1920))
            watchdog = context.max_user_speech_task
            deadline = context.max_user_speech_deadline
            provisional = context.projection.provisional
            assert watchdog is not None
            release.set()
        await asyncio.wait_for(registry._audio_ingress._wait_until_idle(context), 1)
        assert recoveries == [identity.stream_epoch]
        assert provider.audio_calls == [1]
        assert not context.ingress.provider_failed
        assert not context.ingress.discontinuity_pending
        assert _user_turns(runtime) == []
        assert 0 <= context.owner_silence_remaining_s <= remaining
        if new_vad_during_publication:
            assert context.active_vad_start_sample == 1920
            assert context.max_user_speech_task is watchdog
            assert not watchdog.done()
            assert context.max_user_speech_deadline == deadline
            assert context.projection.provisional is provisional
            assert context.owner_silence_task is None
            await _expire_speech_watchdog(registry, context)
        else:
            # The restart deliberately discards the discontinuous input, but
            # must resume the remaining budget without another listening event.
            assert context.active_vad_start_sample is None
            assert context.max_user_speech_task is None
            assert context.owner_silence_task is not None
            assert not context.owner_silence_task.done()
            await _expire_owner_timer(registry, context)
        assert context.closed
    finally:
        release.set()
        await asyncio.wait_for(registry._audio_ingress._wait_until_idle(context), 1)
        await registry._finalize_session(identity.session_id)
