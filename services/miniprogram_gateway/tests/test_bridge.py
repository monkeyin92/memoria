from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from livekit import rtc
from services.common.miniprogram_gateway_ticket import GatewayTicketClaims
from services.miniprogram_gateway.bridge import (
    GatewayMediaError,
    GatewayOutboundMessage,
    MiniProgramLiveKitBridge,
    PcmFrameAccumulator,
)
from services.miniprogram_gateway.config import MiniProgramGatewaySettings


def test_pcm_accumulator_reframes_recorder_chunks_without_losing_tail() -> None:
    accumulator = PcmFrameAccumulator(sample_rate=16_000, frame_ms=20)
    first = accumulator.feed(b"\x01\x00" * 100)
    second = accumulator.feed(b"\x02\x00" * 220)

    assert first == ()
    assert len(second) == 1
    assert len(second[0]) == 640
    assert second[0][:200] == b"\x01\x00" * 100
    assert second[0][200:] == b"\x02\x00" * 220


def test_pcm_accumulator_rejects_partial_sample_and_unbounded_buffer() -> None:
    accumulator = PcmFrameAccumulator(sample_rate=16_000, frame_ms=20, max_buffered_frames=1)
    with pytest.raises(GatewayMediaError, match="whole samples"):
        accumulator.feed(b"\x00")
    with pytest.raises(GatewayMediaError, match="buffer exceeded"):
        accumulator.feed(b"\x00\x00" * 321)


@pytest.mark.asyncio
async def test_new_generation_discards_buffered_audio_before_forwarding_ui_event() -> None:
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    agent = SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)

    bridge._on_data_received(
        SimpleNamespace(
            participant=agent,
            topic="voice-agent.ui",
            data=json.dumps(
                {
                    "type": "assistant_state",
                    "turn_id": 1,
                    "generation_id": 1,
                    "state": "speaking",
                }
            ).encode(),
        )
    )
    assert (await bridge.next_outbound()).event == {
        "type": "ui_event",
        "topic": "voice-agent.ui",
        "event": {
            "type": "assistant_state",
            "turn_id": 1,
            "generation_id": 1,
            "state": "speaking",
        },
    }

    bridge._enqueue_audio(GatewayOutboundMessage(binary=b"stale-audio"))
    bridge._on_data_received(
        SimpleNamespace(
            participant=agent,
            topic="voice-agent.ui",
            data=json.dumps(
                {
                    "type": "assistant_state",
                    "turn_id": 2,
                    "generation_id": 2,
                    "state": "thinking",
                }
            ).encode(),
        )
    )

    assert (await bridge.next_outbound()).event == {
        "type": "audio_reset",
        "generation_id": 2,
    }
    assert (await bridge.next_outbound()).event == {
        "type": "ui_event",
        "topic": "voice-agent.ui",
        "event": {
            "type": "assistant_state",
            "turn_id": 2,
            "generation_id": 2,
            "state": "thinking",
        },
    }
    assert bridge._audio_messages.empty()

    bridge._on_data_received(
        SimpleNamespace(
            participant=agent,
            topic="voice-agent.ui",
            data=json.dumps(
                {
                    "type": "assistant_state",
                    "turn_id": 1,
                    "generation_id": 1,
                    "state": "speaking",
                }
            ).encode(),
        )
    )
    assert bridge._generation_id == 2
    assert bridge._event_messages.empty()
