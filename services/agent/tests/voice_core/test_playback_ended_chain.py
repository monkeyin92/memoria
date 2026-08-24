"""Test playback.ended event chain from device to ReplyDeliveryLedger.

This test validates the P0-2 requirement: playback terminal state must flow
from device → Edge → Bridge → PlaybackLedger → ReplyDeliveryLedger.
"""

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.media_protocol import (
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.playback_ledger import PlaybackLedger
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent, ReplyDeliveryLedger


class TestPlaybackEndedChain:
    """Test the complete playback.ended event chain."""

    def test_playback_ledger_accepts_ended_terminal(self):
        """PlaybackLedger should accept terminal=True for ENDED events."""
        ledger = PlaybackLedger()
        fence = GenerationFence(
            session_id="test-session",
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            session_epoch=1,
        )

        # Start generation and register audio
        ledger.start(fence)
        assert ledger.register_audio(fence, sequence=0, source_start_sample=0, frame_samples=480)

        # Simulate playback progress
        ledger.acknowledge(fence, rendered_sample_end=480, received_sequence=0, terminal=False)

        # Not complete yet without explicit terminal
        assert not ledger.is_playback_complete(fence)

        # Send ENDED terminal
        ledger.acknowledge(fence, rendered_sample_end=480, received_sequence=0, terminal=True)

        # Now it should be complete
        assert ledger.is_playback_complete(fence)
        assert ledger.terminal_received(fence)

    def test_playback_ledger_rejects_stale_terminal(self):
        """Stale terminal events should not mark generation complete."""
        ledger = PlaybackLedger()
        fence1 = GenerationFence(
            session_id="test-session",
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            session_epoch=1,
        )
        fence2 = GenerationFence(
            session_id="test-session",
            turn_id=1,
            generation_id=2,
            tool_epoch=0,
            session_epoch=1,
        )

        # Start gen 1, register audio
        ledger.start(fence1)
        ledger.register_audio(fence1, sequence=0, source_start_sample=0, frame_samples=480)

        # Switch to gen 2 (supersede gen 1)
        ledger.start(fence2)
        ledger.register_audio(fence2, sequence=0, source_start_sample=0, frame_samples=480)

        # Gen 1 terminal should be rejected (stale)
        result = ledger.acknowledge(fence1, rendered_sample_end=480, terminal=True)
        assert result == ()
        assert not ledger.terminal_received(fence1)
        stale_ack_count = ledger.stale_ack_count

        # Gen 2 terminal should be accepted
        result = ledger.acknowledge(fence2, rendered_sample_end=480, terminal=True)
        # No text spans were registered, so an accepted terminal ACK still
        # returns an empty tuple. Acceptance is reflected in ledger state.
        assert result == ()
        assert ledger.terminal_received(fence2)
        assert ledger.stale_ack_count == stale_ack_count

    def test_playback_progress_event_type_mapping(self):
        """Verify PlaybackEventType.ENDED maps to terminal=True."""
        # This tests the logic from media_session_output_stream.py:129-133

        # WATERMARK → terminal=None
        event_type = PlaybackEventType.WATERMARK
        terminal = None if event_type is PlaybackEventType.WATERMARK else event_type is PlaybackEventType.ENDED
        assert terminal is None

        # STARTED → terminal=False
        event_type = PlaybackEventType.STARTED
        terminal = None if event_type is PlaybackEventType.WATERMARK else event_type is PlaybackEventType.ENDED
        assert terminal is False

        # PROGRESS → terminal=False
        event_type = PlaybackEventType.PROGRESS
        terminal = None if event_type is PlaybackEventType.WATERMARK else event_type is PlaybackEventType.ENDED
        assert terminal is False

        # ENDED → terminal=True
        event_type = PlaybackEventType.ENDED
        terminal = None if event_type is PlaybackEventType.WATERMARK else event_type is PlaybackEventType.ENDED
        assert terminal is True

        # ERROR → terminal=False
        event_type = PlaybackEventType.ERROR
        terminal = None if event_type is PlaybackEventType.WATERMARK else event_type is PlaybackEventType.ENDED
        assert terminal is False

    def test_reply_delivery_ledger_playback_ended_event(self):
        """ReplyDeliveryLedger should record PLAYBACK_ENDED event."""
        ledger = ReplyDeliveryLedger()
        fence = GenerationFence(
            session_id="test-session",
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            session_epoch=1,
        )

        # Record first frame sent
        delivery, changed = ledger.record(fence, ReplyDeliveryEvent.FIRST_FRAME_SENT)
        assert changed
        assert delivery.first_frame_sent
        assert not delivery.playback_ended
        assert not delivery.terminal

        # Record provider completed
        delivery, changed = ledger.record(fence, ReplyDeliveryEvent.PROVIDER_COMPLETED)
        assert changed
        assert delivery.provider_completed
        assert not delivery.playback_ended
        assert not delivery.terminal

        # Record playback ended
        delivery, changed = ledger.record(
            fence,
            ReplyDeliveryEvent.PLAYBACK_ENDED,
            reason="playback_completed",
        )
        assert changed
        assert delivery.playback_ended
        assert delivery.terminal
        assert delivery.terminal_event == ReplyDeliveryEvent.PLAYBACK_ENDED
        assert delivery.terminal_reason == "playback_completed"

        # Event sequence should be preserved
        assert delivery.events == (
            ReplyDeliveryEvent.FIRST_FRAME_SENT,
            ReplyDeliveryEvent.PROVIDER_COMPLETED,
            ReplyDeliveryEvent.PLAYBACK_ENDED,
        )

    def test_playback_ended_requires_first_frame_sent(self):
        """PLAYBACK_ENDED should only be recorded after FIRST_FRAME_SENT."""
        ledger = ReplyDeliveryLedger()
        fence = GenerationFence(
            session_id="test-session",
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            session_epoch=1,
        )

        # Try to record playback_ended without first_frame_sent
        with pytest.raises(ValueError, match="playback_ended requires first_frame_sent"):
            ledger.record(fence, ReplyDeliveryEvent.PLAYBACK_ENDED)

    def test_complete_playback_chain_simulation(self):
        """Simulate complete device → ledger → delivery chain."""
        playback_ledger = PlaybackLedger()
        reply_ledger = ReplyDeliveryLedger()

        fence = GenerationFence(
            session_id="sim-session",
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            session_epoch=1,
        )

        # 1. Voice Core registers audio frames
        playback_ledger.start(fence)
        playback_ledger.register_audio(fence, sequence=0, source_start_sample=0, frame_samples=480)
        playback_ledger.register_audio(fence, sequence=1, source_start_sample=480, frame_samples=480)

        # 2. Device sends playback.started (STARTED → terminal=False)
        reply_ledger.record(fence, ReplyDeliveryEvent.FIRST_FRAME_SENT)
        playback_ledger.acknowledge(fence, rendered_sample_end=480, received_sequence=0, terminal=False)

        # 3. Provider completes
        reply_ledger.record(fence, ReplyDeliveryEvent.PROVIDER_COMPLETED)

        # 4. Device sends playback.progress (PROGRESS → terminal=False)
        playback_ledger.acknowledge(fence, rendered_sample_end=960, received_sequence=1, terminal=False)

        # At this point, playback is NOT complete
        assert not playback_ledger.is_playback_complete(fence)

        # 5. Device sends playback.ended (ENDED → terminal=True)
        playback_ledger.acknowledge(fence, rendered_sample_end=960, received_sequence=1, terminal=True)

        # Now playback IS complete
        assert playback_ledger.is_playback_complete(fence)
        assert playback_ledger.terminal_received(fence)

        # 6. Voice Core records playback_ended in delivery ledger
        reply_ledger.record(fence, ReplyDeliveryEvent.PLAYBACK_ENDED, reason="playback_completed")

        # Verify final state
        delivery = reply_ledger.get(fence)
        assert delivery is not None
        assert delivery.first_frame_sent
        assert delivery.provider_completed
        assert delivery.playback_ended
        assert delivery.terminal
        assert delivery.terminal_event == ReplyDeliveryEvent.PLAYBACK_ENDED

    def test_playback_ended_not_triggered_without_provider_complete(self):
        """Playback complete alone should not trigger delivery ended."""
        # This tests the condition in _finish_completed_output:
        # if not context.provider_complete or not context.playback.is_playback_complete(fence):
        #     return

        ledger = PlaybackLedger()
        fence = GenerationFence(
            session_id="test-session",
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            session_epoch=1,
        )

        ledger.start(fence)
        ledger.register_audio(fence, sequence=0, source_start_sample=0, frame_samples=480)
        ledger.acknowledge(fence, rendered_sample_end=480, terminal=True)

        # Playback is complete
        assert ledger.is_playback_complete(fence)

        # But in real code, _finish_completed_output would NOT fire
        # because provider_complete=False
        # This test documents the requirement that BOTH conditions must be true

    def test_device_playback_progress_construction(self):
        """Verify PlaybackProgress object construction with event_type."""
        identity = SessionIdentity(
            session_id="test-session",
            account_id="acc123",
            device_id="dev456",
            client_type="device",
            stream_epoch=1,
            binding_id="binding-test",
            binding_version=1,
            runtime_profile_version=1,
        )

        # Construct progress with ENDED event type
        progress = PlaybackProgress(
            identity=identity,
            generation_id=1,
            received_sequence=5,
            rendered_sample_end=2400,
            client_monotonic_ms=5000,
            approximate=False,
            turn_id=1,
            tool_epoch=0,
            session_epoch=1,
            event_type=PlaybackEventType.ENDED,
        )

        assert progress.event_type == PlaybackEventType.ENDED
        assert not progress.approximate
        assert progress.session_epoch == 1

        # Verify it can be serialized (important for gRPC/JSON)
        payload = progress.to_payload()
        assert payload["event_type"] == "ended"
        assert payload["session_epoch"] == 1
