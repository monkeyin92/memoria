"""Tests for listening state manager."""

import asyncio
import pytest
import time

from services.agent.src.voice_core.listening_state_manager import (
    ListeningState,
    ListeningStateConfig,
    ListeningStateManager,
)


class TestListeningStateManager:
    """Test suite for ListeningStateManager."""

    def test_initialization(self):
        """Test manager initialization with default config."""
        manager = ListeningStateManager()
        assert manager.config.idle_timeout == 5.0
        assert manager.config.silence_timeout == 1.5
        assert manager.config.auto_transition is True

    def test_initialization_custom_config(self):
        """Test manager initialization with custom config."""
        config = ListeningStateConfig(
            idle_timeout=10.0,
            silence_timeout=2.0,
            auto_transition=False,
        )
        manager = ListeningStateManager(config)
        assert manager.config.idle_timeout == 10.0
        assert manager.config.silence_timeout == 2.0
        assert manager.config.auto_transition is False

    def test_default_state_is_idle(self):
        """Test that new sessions start in IDLE state."""
        manager = ListeningStateManager()
        state = manager.get_state("test_session")
        assert state == ListeningState.IDLE

    def test_should_not_accept_audio_in_idle(self):
        """Test that audio is not accepted in IDLE state."""
        manager = ListeningStateManager()
        assert manager.should_accept_audio("test_session") is False

    @pytest.mark.asyncio
    async def test_transition_to_listening(self):
        """Test transitioning from IDLE to LISTENING."""
        manager = ListeningStateManager()
        session_id = "test_session"

        # Should start in IDLE
        assert manager.get_state(session_id) == ListeningState.IDLE

        # Transition to LISTENING
        result = await manager.transition_to_listening(session_id)
        assert result is True
        assert manager.get_state(session_id) == ListeningState.LISTENING
        assert manager.should_accept_audio(session_id) is True

    @pytest.mark.asyncio
    async def test_transition_to_listening_already_listening(self):
        """Test transitioning to LISTENING when already listening."""
        manager = ListeningStateManager()
        session_id = "test_session"

        await manager.transition_to_listening(session_id)
        result = await manager.transition_to_listening(session_id)

        # Should return False (already listening)
        assert result is False
        assert manager.get_state(session_id) == ListeningState.LISTENING

    @pytest.mark.asyncio
    async def test_speech_detected(self):
        """Test speech detection updates state."""
        manager = ListeningStateManager()
        session_id = "test_session"

        await manager.transition_to_listening(session_id)
        await manager.on_speech_detected(session_id)

        # Should still be in LISTENING
        assert manager.get_state(session_id) == ListeningState.LISTENING

        # Check internal state
        state_data = manager._states[session_id]
        assert state_data.speech_detected is True

    @pytest.mark.asyncio
    async def test_transition_to_processing(self):
        """Test transitioning to PROCESSING state."""
        manager = ListeningStateManager()
        session_id = "test_session"

        await manager.transition_to_listening(session_id)
        await manager.on_speech_detected(session_id)
        await manager.transition_to_processing(session_id)

        assert manager.get_state(session_id) == ListeningState.PROCESSING
        # Should not accept audio in PROCESSING
        assert manager.should_accept_audio(session_id) is False

    @pytest.mark.asyncio
    async def test_transition_to_speaking(self):
        """Test transitioning to SPEAKING state."""
        manager = ListeningStateManager()
        session_id = "test_session"

        await manager.transition_to_speaking(session_id)

        assert manager.get_state(session_id) == ListeningState.SPEAKING
        # Should accept audio if interruption allowed (default)
        assert manager.should_accept_audio(session_id) is True

    @pytest.mark.asyncio
    async def test_speaking_no_interruption(self):
        """Test that audio is rejected during SPEAKING if interruption disabled."""
        config = ListeningStateConfig(allow_interruption=False)
        manager = ListeningStateManager(config)
        session_id = "test_session"

        await manager.transition_to_speaking(session_id)

        assert manager.get_state(session_id) == ListeningState.SPEAKING
        # Should NOT accept audio when interruption disabled
        assert manager.should_accept_audio(session_id) is False

    @pytest.mark.asyncio
    async def test_transition_to_idle(self):
        """Test transitioning back to IDLE."""
        manager = ListeningStateManager()
        session_id = "test_session"

        await manager.transition_to_listening(session_id)
        await manager.transition_to_idle(session_id, "manual")

        assert manager.get_state(session_id) == ListeningState.IDLE
        assert manager.should_accept_audio(session_id) is False

    @pytest.mark.asyncio
    async def test_idle_timeout(self):
        """Test automatic timeout when no speech detected."""
        config = ListeningStateConfig(idle_timeout=0.5)  # 500ms for fast test
        manager = ListeningStateManager(config)
        session_id = "test_session"

        # Enter LISTENING without detecting speech
        await manager.transition_to_listening(session_id)
        assert manager.get_state(session_id) == ListeningState.LISTENING

        # Wait for timeout
        await asyncio.sleep(0.6)

        # Should have timed out to IDLE
        assert manager.get_state(session_id) == ListeningState.IDLE

    @pytest.mark.asyncio
    async def test_no_timeout_when_speech_detected(self):
        """Test that timeout doesn't trigger when speech is detected."""
        config = ListeningStateConfig(idle_timeout=0.5)
        manager = ListeningStateManager(config)
        session_id = "test_session"

        # Enter LISTENING and detect speech
        await manager.transition_to_listening(session_id)
        await manager.on_speech_detected(session_id)

        # Wait past timeout
        await asyncio.sleep(0.6)

        # Should still be LISTENING (speech detected prevents timeout)
        assert manager.get_state(session_id) == ListeningState.LISTENING

    @pytest.mark.asyncio
    async def test_speech_end_with_auto_transition(self):
        """Test automatic transition to PROCESSING after speech ends."""
        config = ListeningStateConfig(
            silence_timeout=0.3,  # 300ms for fast test
            auto_transition=True,
        )
        manager = ListeningStateManager(config)
        session_id = "test_session"

        await manager.transition_to_listening(session_id)
        await manager.on_speech_detected(session_id)

        # Speech ends
        asyncio.create_task(manager.on_speech_ended(session_id))

        # Wait for silence timeout
        await asyncio.sleep(0.4)

        # Should have transitioned to PROCESSING
        assert manager.get_state(session_id) == ListeningState.PROCESSING

    @pytest.mark.asyncio
    async def test_speech_end_without_auto_transition(self):
        """Test that speech end doesn't auto-transition when disabled."""
        config = ListeningStateConfig(
            silence_timeout=0.3,
            auto_transition=False,
        )
        manager = ListeningStateManager(config)
        session_id = "test_session"

        await manager.transition_to_listening(session_id)
        await manager.on_speech_detected(session_id)
        await manager.on_speech_ended(session_id)

        # Wait
        await asyncio.sleep(0.4)

        # Should still be LISTENING (no auto-transition)
        assert manager.get_state(session_id) == ListeningState.LISTENING

    @pytest.mark.asyncio
    async def test_cleanup_session(self):
        """Test session cleanup."""
        manager = ListeningStateManager()
        session_id = "test_session"

        await manager.transition_to_listening(session_id)
        assert session_id in manager._states

        manager.cleanup_session(session_id)
        assert session_id not in manager._states

    @pytest.mark.asyncio
    async def test_multiple_sessions(self):
        """Test managing multiple sessions independently."""
        manager = ListeningStateManager()

        session1 = "session_1"
        session2 = "session_2"

        # Session 1 goes to LISTENING
        await manager.transition_to_listening(session1)
        assert manager.get_state(session1) == ListeningState.LISTENING

        # Session 2 stays IDLE
        assert manager.get_state(session2) == ListeningState.IDLE

        # Session 2 goes to SPEAKING
        await manager.transition_to_speaking(session2)
        assert manager.get_state(session2) == ListeningState.SPEAKING

        # Session 1 should still be LISTENING
        assert manager.get_state(session1) == ListeningState.LISTENING

    @pytest.mark.asyncio
    async def test_timeout_task_cancellation(self):
        """Test that timeout tasks are properly cancelled."""
        manager = ListeningStateManager()
        session_id = "test_session"

        await manager.transition_to_listening(session_id)
        state_data = manager._states[session_id]
        timeout_task = state_data.timeout_task

        assert timeout_task is not None
        assert not timeout_task.done()

        # Transition to another state should cancel timeout
        await manager.transition_to_processing(session_id)

        # Give it a moment
        await asyncio.sleep(0.1)

        assert timeout_task.cancelled() or timeout_task.done()


if __name__ == "__main__":
    # Run basic sanity test
    async def main():
        print("Running listening state manager sanity test...\n")

        manager = ListeningStateManager()

        # Test 1: Initial state
        session_id = "test_session"
        state = manager.get_state(session_id)
        print(f"✓ Initial state: {state} (should be IDLE)")
        assert state == ListeningState.IDLE

        # Test 2: Should not accept audio in IDLE
        accepts = manager.should_accept_audio(session_id)
        print(f"✓ Accept audio in IDLE: {accepts} (should be False)")
        assert accepts is False

        # Test 3: Transition to LISTENING
        await manager.transition_to_listening(session_id)
        state = manager.get_state(session_id)
        print(f"✓ After transition: {state} (should be LISTENING)")
        assert state == ListeningState.LISTENING

        # Test 4: Should accept audio in LISTENING
        accepts = manager.should_accept_audio(session_id)
        print(f"✓ Accept audio in LISTENING: {accepts} (should be True)")
        assert accepts is True

        # Test 5: Detect speech
        await manager.on_speech_detected(session_id)
        print(f"✓ Speech detected")

        # Test 6: Transition to PROCESSING
        await manager.transition_to_processing(session_id)
        state = manager.get_state(session_id)
        print(f"✓ After processing: {state} (should be PROCESSING)")
        assert state == ListeningState.PROCESSING

        # Test 7: Idle timeout test
        print("\n✓ Testing idle timeout (5 seconds)...")
        await manager.transition_to_listening(session_id)
        print(f"  State: {manager.get_state(session_id)}")
        print(f"  Waiting for timeout...")

        await asyncio.sleep(5.5)
        state = manager.get_state(session_id)
        print(f"  After timeout: {state} (should be IDLE)")
        assert state == ListeningState.IDLE

        print("\n✅ All sanity tests passed!")
        print("\nRun full test suite with: pytest services/agent/tests/test_listening_state_manager.py")

    asyncio.run(main())
