"""The device-facing close after both TTS engines fail before audio.

StepFun and the Doubao backup run as fake endpoint engines and both units fail
before the first frame reaches the wire. The real registry then requests
standby, and the real bridge emits one CLOSED event carrying reason
``tts_unavailable`` and ``play_line=True``: the fallback line was not yet
spoken, so the device must speak the existing offline clip itself.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
import pytest
from services.agent.src.providers.tts_failover import TTSUnavailableError
from services.agent.src.voice_core.aigc_notice import AigcNoticeConfig
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.tests.unit.media_session_support import (
    _device_identity,
    open_bridge_connection,
)
from services.agent.tests.unit.test_minor_session_limits import _ledger
from services.agent.tests.unit.test_tts_failover_aigc_rounds import (
    _MarkerRecordingAdapter,
    _round_audio,
    _RoundBridge,
    _RoundReply,
    _speech,
)


@pytest.mark.asyncio
async def test_device_close_after_tts_failover_failure_carries_play_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both fake engines fail before audio; the device close must say why."""

    speech, primary, backup = _speech("connection", "forbidden")
    markers: list[tuple[Any, bytes]] = []
    adapter = _MarkerRecordingAdapter(
        asr_session_factory=cast(Any, lambda: None),
        language_model=cast(Any, _RoundReply("你好呀")),
        speech_synthesis=cast(Any, speech),
        markers=markers,
    )
    media_bridge = _RoundBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=media_bridge,
        provider_factory=lambda _identity: adapter,
        aigc_notice_config=AigcNoticeConfig(enabled=False, mode="none"),
        aigc_round_logger=None,
        daily_chat_client=_ledger(
            httpx.Response(200, json={"used_seconds": 0}),
            [],
        ),
        owner_silence_timeout_s=100,
        speaking_stall_timeout_s=0.3,
    )
    registry.install()
    identity = _device_identity("tts-close-fallback")

    async def no_wake_ack(_registry: Any, _context: Any) -> None:
        return None

    monkeypatch.setattr(MediaVoiceCoreRegistry, "_speak_device_wake_ack", no_wake_ack)
    connection = open_bridge_connection(registry.bridge, identity)
    context = await registry.open_session(identity)
    try:
        fence, reply_task = await _round_audio(
            registry,
            identity,
            context,
            wait_provider=False,
        )
        outcome = await asyncio.gather(reply_task, return_exceptions=True)
        assert isinstance(outcome[0], TTSUnavailableError)
        assert "all TTS engines failed" in str(outcome[0])
    finally:
        await registry.finalize_session(identity.session_id)

    messages: list[media_pb2.CoreToMedia] = []
    while True:
        try:
            item = connection.outgoing.get_nowait()
        except asyncio.QueueEmpty:
            break
        if item is not None:
            messages.append(item)
    closed = [
        message.state
        for message in messages
        if message.WhichOneof("event") == "state"
        and message.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    ]
    assert [event.reason for event in closed] == ["tts_unavailable"]
    assert [event.play_line for event in closed] == [True]
    assert primary.received == ["你好呀"]
    assert backup.received == ["你好呀"]
