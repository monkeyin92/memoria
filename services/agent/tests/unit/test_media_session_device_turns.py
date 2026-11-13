"""Media session: device clock-fact and conversation-close pins, rescue and empty ASR turns."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.prompts import (
    BRIDGE_PHRASES,
)
from services.agent.src.voice_core.media_protocol import (
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.media_session_types import (
    ProviderAudioTaskSnapshot,
)
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    _AckCapturingProvider,
    _CapturingGenerationBridge,
    _device_identity,
    _finish_output_owner_playback,
    _verified_owner_decision,
)
from services.agent.tests.unit.runtime_state_helpers import set_floor
from services.speaker.domain import SpeakerDecision


@pytest.mark.asyncio
async def test_device_vad_end_during_wake_playback_is_ignored() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()

    def runtime_factory(session_id: str) -> DuplexRuntime:
        return DuplexRuntime.create(session_id=session_id, barge_in_enabled=False)

    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=runtime_factory,
    )
    registry.install()
    identity = _device_identity("device-wake-vad-ignore")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        set_floor(context.runtime, assistant_speaking=True)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=0,
                segment_id="wake-echo-end",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=1_280,
                capture_end_sample=1_281,
                final=True,
                voiced_end_sample=1_280,
            ),
        )
        assert context.pending.turn_endpoint_sample is None
        assert context.pending.pending_partial is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_final_commits_before_vad_end() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-early-clock-fact")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="clock-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.pending.turn_endpoint_sample == 16_000
        assert context.turn_endpoint_task is not None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_conversation_close_final_commits_before_vad_end() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-early-conversation-close")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="farewell-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="farewell-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="好的，那你早点休息，再见。",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.pending.turn_endpoint_sample == 16_000
        assert context.pending.conversation_close_endpoint_pinned == 16_000
        assert context.turn_endpoint_task is not None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["再见", "好的，再见", "那就先这样吧"])
@pytest.mark.parametrize("final_text", ["好的，再见", "给我讲一个故事"])
async def test_device_conversation_close_partial_waits_for_final(text: str, final_text: str) -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-immediate-partial-conversation-close")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)

        async def semantic_close(value: str) -> bool:
            return value == "那就先这样吧"

        context.runtime.set_conversation_close_semantic_resolver(semantic_close)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="farewell-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        partial = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="farewell-partial",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text=text,
            is_final=False,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, partial)
        assert context.pending.conversation_close_endpoint_pinned is None
        assert context.pending.turn_endpoint_sample is None
        context.pending.conversation_close_partial_stable_since = time.monotonic() - 2
        registry._maybe_early_commit_stable_conversation_close_partial(context)
        await asyncio.sleep(0)
        assert context.pending.conversation_close_endpoint_pinned is None
        assert context.pending.turn_endpoint_sample is None
        context.pending.turn_endpoint_sample = 16_000
        assert not await registry._commit_stable_partial_fallback(context, 16_000)
        assert not context.standby_requested
        assert context.pending.pending_partial is not None
        assert not context.pending.pending_partial.is_final
        context.pending.turn_endpoint_sample = None
        final = replace(partial, text=final_text, is_final=True, revision=2)
        assert await registry.accept_asr_result(identity.session_id, final)
        if final_text == "好的，再见":
            assert context.pending.conversation_close_endpoint_pinned == 16_000
        else:
            assert context.pending.conversation_close_endpoint_pinned is None
            assert not context.standby_requested
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_conversation_close_pin_blocks_late_vad_end_extension() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-conversation-close-pin")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="farewell-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="farewell-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="好的，再见",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        pinned = context.pending.conversation_close_endpoint_pinned
        assert pinned == 16_000
        assert context.pending.turn_endpoint_sample == pinned

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="late-vad-end",
                revision=2,
                kind=SegmentKind.VAD,
                capture_start_sample=200_000,
                capture_end_sample=200_001,
                final=True,
                voiced_end_sample=200_000,
            ),
        )
        assert context.pending.turn_endpoint_sample != 200_000
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_conversation_close_semantic_final_commits_before_vad_end() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-semantic-conversation-close")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        # The farewell follows the wake ack: while a reply is audible a
        # semantic-only close is held, never early-endpointed.
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        calls: list[str] = []

        async def resolver(text: str) -> bool:
            calls.append(text)
            return text == "那先不聊了"

        context.runtime.set_conversation_close_semantic_resolver(resolver)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="farewell-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="farewell-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="那先不聊了",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        task = context.pending.conversation_close_semantic_task
        if task is not None:
            await task
        assert calls == ["那先不聊了"]
        assert (
            context.pending.conversation_close_endpoint_pinned == 16_000
            or context.pending.turn_endpoint_sample == 16_000
            or context.standby_requested
        )
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_bare_stop_word_never_pins_a_conversation_close() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-stop-word-not-farewell")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        calls: list[str] = []

        async def resolver(text: str) -> bool:
            # The classifier that read a bare 「停」 as a farewell on the device.
            calls.append(text)
            return True

        context.runtime.set_conversation_close_semantic_resolver(resolver)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="stop-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="stop-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=8_000,
            text="停",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        task = context.pending.conversation_close_semantic_task
        if task is not None:
            await task
        assert calls == []
        assert context.pending.conversation_close_endpoint_pinned is None
        assert not context.standby_requested
        assert not context.runtime.conversation_close_needed("停")
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_pin_blocks_late_vad_end_extension() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-clock-fact-pin")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="clock-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.pending.clock_fact_endpoint_pinned == 16_000
        assert context.pending.turn_endpoint_sample == 16_000
        assert context.pending.turn_retire_sample == 16_000

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="late-vad-end",
                revision=2,
                kind=SegmentKind.VAD,
                capture_start_sample=200_000,
                capture_end_sample=200_001,
                final=True,
                voiced_end_sample=200_000,
            ),
        )
        assert context.pending.turn_endpoint_sample != 200_000
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_concatenated_timeline_commits_canonical_segment() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-clock-fact-concat")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="clock-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        partial = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-partial",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=False,
            confidence=0.8,
        )
        final = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=2,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天是星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, partial)
        assert await registry.accept_asr_result(identity.session_id, final)
        assert context.pending.turn_endpoint_sample == 16_000

        fence, reason = await registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=16_000,
        )
        assert fence is not None
        assert reason is None
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天是星期几"]
        assert context.projection.provisional is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_prepare_provisional_drift_still_commits() -> None:
    bridge = _CapturingGenerationBridge()
    identity = _device_identity("device-clock-fact-drift")
    runtime = DuplexRuntime.create(session_id=identity.session_id, barge_in_enabled=False)
    registry_holder: dict[str, MediaVoiceCoreRegistry] = {}
    context_holder: dict[str, Any] = {}

    class _ProvisionalDriftProvider(_AckCapturingProvider):
        async def prepare_committed_turn(
            self,
            _identity: SessionIdentity,
            text: str,
        ) -> GenerationFence:
            drift = SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="late-partial",
                revision=3,
                kind=SegmentKind.ASR_PARTIAL,
                capture_start_sample=0,
                capture_end_sample=16_000,
                text="今天星期几",
            )
            context = context_holder["context"]
            assert runtime.ingest_media_speech_segment(drift)
            await registry_holder["registry"]._apply_projection_segment(context, drift)
            return await runtime.on_turn_committed(text, input_modality="audio")

    provider = _ProvisionalDriftProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: runtime,
    )
    registry.install()
    registry_holder["registry"] = registry
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        context_holder["context"] = context
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="clock-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天是星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.pending.turn_endpoint_sample == 16_000

        fence, reason = await registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=16_000,
        )
        assert fence is not None
        assert reason is None
        user_turns = [
            turn.content
            for turn in runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天是星期几"]
        assert context.projection.provisional is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_pin_survives_replayed_vad_start() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-clock-fact-vad-start")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="clock-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        pinned = context.pending.clock_fact_endpoint_pinned
        assert pinned == 16_000
        assert context.pending.turn_endpoint_sample == pinned

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="replayed-vad-start",
                revision=2,
                kind=SegmentKind.VAD,
                capture_start_sample=20_000,
                capture_end_sample=20_001,
            ),
        )
        assert context.pending.clock_fact_endpoint_pinned == pinned
        assert context.pending.turn_endpoint_sample == pinned
        assert context.turn_endpoint_task is not None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_pinned_clock_fact_commits_without_asr_endpoint_coverage() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-clock-fact-coverage")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="clock-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        context.pending.turn_end_sample = 8_000
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_commit_survives_recovery_reschedule() -> None:
    """Late overlap recovery must not abort an in-flight clock-fact commit.

    Field (2026-09-03 epoch 1366): FunASR early clock-fact entered commit
    (speaker classify), SenseVoice ``cross_sentence_overlap`` recovery
    re-armed ``_schedule_turn_commit`` and cancelled the grace task mid-flight;
    ASR tail timeout then discarded the weekday turn despite timeline text.
    """

    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-clock-fact-reschedule")
    session = bridge.bridge.open(identity)
    commit_entered = asyncio.Event()
    allow_commit = asyncio.Event()
    original_commit = registry._commit_pending_turn

    async def delayed_commit(context, *, provider_final_missing=False, schedule_prepare_retry=True):
        commit_entered.set()
        await allow_commit.wait()
        return await original_commit(
            context,
            provider_final_missing=provider_final_missing,
            schedule_prepare_retry=schedule_prepare_retry,
        )

    registry._commit_pending_turn = delayed_commit  # type: ignore[method-assign]
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        provider.texts.clear()

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="clock-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.speech_timeline import ASRResult

        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        assert context.pending.clock_fact_endpoint_pinned == 16_000
        first_task = context.turn_endpoint_task
        assert first_task is not None

        await asyncio.wait_for(commit_entered.wait(), timeout=1)
        # Simulate late offline recovery re-arming the same endpoint.
        registry._schedule_turn_commit(context)
        second_task = context.turn_endpoint_task
        assert second_task is not None
        assert second_task is not first_task
        allow_commit.set()

        for _ in range(50):
            user_turns = [
                turn.content
                for turn in context.runtime.orchestrator.context.turns
                if turn.role == "user" and turn.content
            ]
            if user_turns == ["今天星期几"] and context.pending.turn_endpoint_sample is None:
                break
            await asyncio.sleep(0.05)
        else:
            user_turns = [
                turn.content
                for turn in context.runtime.orchestrator.context.turns
                if turn.role == "user" and turn.content
            ]
            raise AssertionError(
                f"clock-fact turn did not commit after recovery reschedule: "
                f"user_turns={user_turns!r} endpoint={context.pending.turn_endpoint_sample}"
            )
    finally:
        allow_commit.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_mostly_committed_straddling_final_is_dropped_not_readopted() -> None:
    """One question must not become two turns (real session 2026-09-14 epoch 1946).

    The offline rescue of already committed audio arrived as a shorter second
    transcript covering samples 11200-392000 while the committed watermark was
    369280, so 94% of its audio had already been committed and answered. Without
    complete word timings that text cannot be cut at the watermark, and adopting
    it re-answered the same audio: the operator heard the filler twice and the
    pending first answer was preempted.
    """

    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-straddle-rescue")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        context.asr.mark_committed(369_280)
        context.runtime.commit_media_speech_range(
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=369_280,
        )
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason
        from services.agent.src.voice_core.speech_timeline import ASRResult

        rescue = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=2,
            sentence_id="rescue-final",
            revision=1,
            capture_start_sample=11_200,
            capture_end_sample=392_000,
            text="明天南京的天气怎么样呢",
            is_final=True,
            confidence=0.9,
        )
        decision = await registry._accept_asr_result_decision(
            identity.session_id,
            rescue,
        )
        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING
        assert context.pending.live_query_forced_text is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_commits_when_provisional_lags_asr_end() -> None:
    """A weekday pin must commit even if projection still holds a short VAD range.

    Field (2026-09-04 epoch 1384): FunASR accepted ``今天星期几`` and early-
    committed endpoint=154880, then ``validate_commit`` failed closed as
    ``projection_range_mismatch``. The board stayed silent until owner silence.
    """

    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-clock-fact-range-lag")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="clock-vad-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=84_160,
                capture_end_sample=84_161,
            ),
        )
        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=123_520,
            capture_end_sample=154_880,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, accepted)
        # Reproduce the field lag: VAD-only provisional end, ASR pin already armed.
        context.projection._provisional = replace(  # noqa: SLF001
            context.projection.provisional,
            capture_start_sample=84_160,
            capture_end_sample=84_161,
            text="今天星期几",
        )
        context.pending.turn_start_sample = 84_160
        context.pending.turn_end_sample = 154_880
        context.pending.turn_endpoint_sample = 154_880
        context.pending.turn_retire_sample = 154_880
        context.pending.clock_fact_endpoint_pinned = 154_880
        context.pending.turn_endpoint_grace_deadline = time.monotonic()
        registry._schedule_turn_commit(context)
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_clock_fact_recovered_after_overlap_without_vad() -> None:
    """Overlap recovery must arm turn_start, not only the clock-fact pin.

    Field (2026-09-04 epoch 1384 hop 2): the weekday final was rejected as
    ``cross_sentence_overlap``, recovery pinned endpoint=205760 with
    ``turn_start_sample is None``, then ASR tail timeout discarded the turn
    as ``low_rms``.
    """

    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-clock-fact-overlap-no-vad")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        # The echo below repeats what the device just said; the echo guard
        # of a final that straddles the playback boundary reads that text.
        context.output.assistant_text = "你好，我是茉莉，今天想聊点什么呀？"
        provider.started.clear()
        provider.completed.clear()
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

        echo = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="echo-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=180_000,
            text="你好我是茉莉今天想聊点什么呀",
            is_final=True,
            confidence=0.9,
        )
        echo_decision = await registry._accept_asr_result_decision(
            identity.session_id,
            echo,
        )
        assert echo_decision.accepted is not None
        weekday = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-overlap",
            revision=1,
            capture_start_sample=123_520,
            capture_end_sample=205_760,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
            rescue_synthesized=True,
        )
        decision = await registry._accept_asr_result_decision(
            identity.session_id,
            weekday,
        )
        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
        assert context.pending.turn_start_sample is not None
        assert context.pending.turn_endpoint_sample == 205_760
        await asyncio.sleep(0.2)
        user_turns = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns == ["今天星期几"]
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_low_energy_rescue_close_rejection_does_not_request_standby() -> None:
    """A near-silent rescue final must not turn overlap rejection into close."""

    class LowEnergyRescueProvider(_AckCapturingProvider):
        def current_asr_audio_task_snapshot(self) -> ProviderAudioTaskSnapshot:
            return ProviderAudioTaskSnapshot(
                task_epoch=1,
                task_sample_origin=0,
                audio_start_sample=0,
                audio_end_sample=336_960,
                send_count=1,
                observed_sample_count=336_960,
                peak_abs=0,
                rms=0.0,
                all_zero=True,
            )

    provider = LowEnergyRescueProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-low-energy-close-rescue")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        # The echo below repeats what the device just said; the echo guard
        # of a final that straddles the playback boundary reads that text.
        context.output.assistant_text = "你好，我是茉莉，今天想聊点什么呀？"

        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

        echo = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="echo-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=64_000,
            text="你好我是茉莉今天想聊点什么呀",
            is_final=True,
            confidence=0.9,
        )
        assert (
            await registry._accept_asr_result_decision(identity.session_id, echo)
        ).accepted is not None

        farewell = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="low-energy-farewell-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=336_960,
            text="你说的好多呀，好的，我知道了，再见！",
            is_final=True,
            confidence=0.9,
            rescue_synthesized=True,
        )
        decision = await registry._accept_asr_result_decision(
            identity.session_id,
            farewell,
        )

        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
        assert context.pending.conversation_close_endpoint_pinned is None
        assert context.pending.turn_endpoint_sample is None
        assert context.standby_requested is False
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rescue_rms", "rescue_peak_abs", "expect_close"),
    [
        (389, 2_616, False),
        (3_263, 32_768, True),
    ],
    ids=["room-noise-rescue", "owner-speech-rescue"],
)
async def test_device_straddling_rescue_farewell_needs_speech_energy(
    rescue_rms: int,
    rescue_peak_abs: int,
    expect_close: bool,
) -> None:
    """A rescue farewell decoded from room noise must not end the session.

    Run 2026-09-24 session b18fede9: a weather answer waited behind noise
    VAD, SenseVoice turned the noise into three characters (rms 389, peak
    2616, FunASR silent), the straddle recovery honoured it as a farewell,
    and the pending answer was cancelled before its first frame.
    """

    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity(f"device-straddle-rescue-close-{rescue_rms}")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        context.asr.mark_committed(64_000)
        context.runtime.commit_media_speech_range(
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=64_000,
        )
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

        farewell = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="0",
            revision=1,
            capture_start_sample=40_000,
            capture_end_sample=160_000,
            text="拜拜。",
            is_final=True,
            rescue_synthesized=True,
            rescue_rms=rescue_rms,
            rescue_peak_abs=rescue_peak_abs,
        )
        decision = await registry._accept_asr_result_decision(
            identity.session_id,
            farewell,
        )

        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING
        if expect_close:
            assert context.pending.conversation_close_endpoint_pinned == 160_000
        else:
            assert context.pending.conversation_close_endpoint_pinned is None
            assert context.pending.turn_endpoint_sample is None
            assert context.standby_requested is False
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_empty_asr_asks_user_to_repeat() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0,
        turn_endpoint_min_grace_s=0,
        turn_endpoint_max_grace_s=0.01,
        turn_endpoint_absolute_timeout_s=0.02,
    )
    registry.install()
    identity = _device_identity("device-empty-hear")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        provider.texts.clear()

        async def classify_owner(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
            return _verified_owner_decision()

        context.runtime.set_speaker_classifier(classify_owner, sample_rate=16_000)
        context.runtime._speaker_pcm.extend(b"\x00\x20" * 8_000)

        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=0,
                segment_id="empty-asr-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=0,
                segment_id="empty-asr-end",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=16_000,
                capture_end_sample=16_001,
                final=True,
                voiced_end_sample=16_000,
            ),
        )
        endpoint_sample = context.pending.turn_endpoint_sample
        assert endpoint_sample == 16_000
        endpoint_task = context.turn_endpoint_task
        assert endpoint_task is not None
        endpoint_task.cancel()
        await asyncio.gather(endpoint_task, return_exceptions=True)
        timeout_handle = context.pending.turn_endpoint_timeout_handle
        if timeout_handle is not None:
            timeout_handle.cancel()
            context.pending.turn_endpoint_timeout_handle = None

        decision = _verified_owner_decision()
        context.runtime._speaker_decision = decision
        context.runtime._speaker_class = decision.classification

        await registry._expire_endpoint_tail(
            identity.session_id,
            identity.stream_epoch,
            endpoint_sample,
        )

        await asyncio.wait_for(provider.started.wait(), timeout=1)
        assert provider.texts == [BRIDGE_PHRASES[2]]
        assert context.runtime.output_floor_allows_assistant
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_device_empty_asr_without_owner_stays_silent() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = _device_identity("device-empty-no-owner")
    session = bridge.bridge.open(identity)
    try:
        await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        provider.texts.clear()

        fence, reason = await registry.commit_user_turn(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
            start_sample=0,
            end_sample=16_000,
        )
        assert fence is None
        assert reason == "empty_media_turn"
        await asyncio.sleep(0.05)
        assert provider.texts == []
        assert not provider.started.is_set()
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_missing_final_classifier_error_keeps_session_open(caplog: pytest.LogCaptureFixture) -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(bridge=bridge, provider_factory=lambda _identity: provider)
    registry.install()
    identity = _device_identity("missing-final-classifier-error")
    bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)

        async def broken_classifier(_text: str) -> bool:
            raise RuntimeError("classifier unavailable")

        context.runtime.set_conversation_close_semantic_resolver(broken_classifier)
        context.pending.pending_partial = ASRResult(
            stream_epoch=identity.stream_epoch, task_epoch=1, sentence_id="partial",
            revision=1, capture_start_sample=0, capture_end_sample=16000,
            text="那就先这样吧", is_final=False, confidence=0.9,
        )
        context.pending.turn_start_sample = 0
        context.pending.turn_endpoint_sample = 16000
        assert not await registry._commit_stable_partial_fallback(context, 16000)
        assert not context.standby_requested
        assert context.pending.conversation_close_endpoint_pinned is None
        assert "close classifier unavailable" in caplog.text
    finally:
        await registry.finalize_session(identity.session_id)
