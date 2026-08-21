"""Listening state manager to prevent always-on recording.

Manages device listening states: idle -> listening -> processing -> idle
Prevents continuous recording and reduces false triggers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)


class ListeningState(str, Enum):
    """Device listening states."""

    IDLE = "idle"  # Not recording, waiting for trigger
    LISTENING = "listening"  # Actively recording and processing
    PROCESSING = "processing"  # Processing user input
    SPEAKING = "speaking"  # Device is speaking (should not record)


@dataclass(slots=True)
class ListeningStateConfig:
    """Configuration for listening state management."""

    # Timeout for auto-exit from LISTENING state (seconds)
    idle_timeout: float = 5.0

    # Minimum silence duration to consider speech ended (seconds)
    silence_timeout: float = 1.5

    # Enable automatic state transitions
    auto_transition: bool = True

    # Allow interruption during speaking
    allow_interruption: bool = True

    def __post_init__(self) -> None:
        if self.idle_timeout <= 0 or self.silence_timeout <= 0:
            raise ValueError("Timeouts must be positive")


@dataclass(slots=True)
class ListeningStateData:
    """Per-session listening state data."""

    state: ListeningState = ListeningState.IDLE
    last_speech_time: float = field(default_factory=time.time)
    last_activity_time: float = field(default_factory=time.time)
    speech_detected: bool = False
    timeout_task: asyncio.Task[None] | None = None


class ListeningStateManager:
    """Manages device listening states to prevent always-on recording."""

    def __init__(self, config: ListeningStateConfig | None = None) -> None:
        self.config = config or ListeningStateConfig()
        self._states: dict[str, ListeningStateData] = {}

    def get_state(self, session_id: str) -> ListeningState:
        """Get current state for a session."""
        if session_id not in self._states:
            self._states[session_id] = ListeningStateData()
        return self._states[session_id].state

    def should_accept_audio(self, session_id: str) -> bool:
        """Check if session should accept audio input."""
        state = self.get_state(session_id)
        # Accept audio only in LISTENING state, or if interruption is allowed during SPEAKING
        if state == ListeningState.LISTENING:
            return True
        if state == ListeningState.SPEAKING and self.config.allow_interruption:
            return True
        return False

    async def transition_to_listening(self, session_id: str) -> bool:
        """Transition to LISTENING state (e.g., wake word detected or button pressed).

        Returns:
            True if transition succeeded, False if already listening
        """
        if session_id not in self._states:
            self._states[session_id] = ListeningStateData()

        state_data = self._states[session_id]
        current_state = state_data.state

        if current_state == ListeningState.LISTENING:
            logger.debug(f"Session {session_id} already in LISTENING state")
            return False

        logger.info(f"Session {session_id}: {current_state} -> LISTENING")
        state_data.state = ListeningState.LISTENING
        state_data.last_activity_time = time.time()
        state_data.speech_detected = False

        # Start idle timeout task
        if state_data.timeout_task:
            state_data.timeout_task.cancel()
        state_data.timeout_task = asyncio.create_task(
            self._idle_timeout_handler(session_id)
        )

        return True

    async def on_speech_detected(self, session_id: str) -> None:
        """Called when speech is detected (VAD speech_start event)."""
        if session_id not in self._states:
            return

        state_data = self._states[session_id]
        if state_data.state != ListeningState.LISTENING:
            return

        state_data.speech_detected = True
        state_data.last_speech_time = time.time()
        state_data.last_activity_time = time.time()

        logger.debug(f"Session {session_id}: Speech detected")

    async def on_speech_ended(self, session_id: str) -> None:
        """Called when speech ends (VAD speech_end event)."""
        if session_id not in self._states:
            return

        state_data = self._states[session_id]
        if state_data.state != ListeningState.LISTENING:
            return

        logger.debug(f"Session {session_id}: Speech ended")

        # If speech was detected, wait for silence timeout then transition to PROCESSING
        if state_data.speech_detected and self.config.auto_transition:
            await asyncio.sleep(self.config.silence_timeout)

            # Check if still in LISTENING and no new speech
            if state_data.state == ListeningState.LISTENING:
                time_since_speech = time.time() - state_data.last_speech_time
                if time_since_speech >= self.config.silence_timeout:
                    await self.transition_to_processing(session_id)

    async def transition_to_processing(self, session_id: str) -> None:
        """Transition to PROCESSING state (user finished speaking)."""
        if session_id not in self._states:
            return

        state_data = self._states[session_id]
        if state_data.state != ListeningState.LISTENING:
            return

        logger.info(f"Session {session_id}: LISTENING -> PROCESSING")
        state_data.state = ListeningState.PROCESSING

        # Cancel timeout task
        if state_data.timeout_task:
            state_data.timeout_task.cancel()
            state_data.timeout_task = None

    async def transition_to_speaking(self, session_id: str) -> None:
        """Transition to SPEAKING state (device is generating response)."""
        if session_id not in self._states:
            self._states[session_id] = ListeningStateData()

        state_data = self._states[session_id]
        logger.info(f"Session {session_id}: {state_data.state} -> SPEAKING")
        state_data.state = ListeningState.SPEAKING

        # Cancel any pending timeout
        if state_data.timeout_task:
            state_data.timeout_task.cancel()
            state_data.timeout_task = None

    async def transition_to_idle(self, session_id: str, reason: str = "manual") -> None:
        """Transition to IDLE state."""
        if session_id not in self._states:
            return

        state_data = self._states[session_id]
        logger.info(f"Session {session_id}: {state_data.state} -> IDLE (reason: {reason})")
        state_data.state = ListeningState.IDLE
        state_data.speech_detected = False

        # Cancel timeout task
        if state_data.timeout_task:
            state_data.timeout_task.cancel()
            state_data.timeout_task = None

    async def _idle_timeout_handler(self, session_id: str) -> None:
        """Handle idle timeout - auto-exit LISTENING state if no speech."""
        try:
            await asyncio.sleep(self.config.idle_timeout)

            if session_id not in self._states:
                return

            state_data = self._states[session_id]

            # If still in LISTENING and no speech detected, go back to IDLE
            if state_data.state == ListeningState.LISTENING and not state_data.speech_detected:
                await self.transition_to_idle(session_id, reason="idle_timeout")
                logger.info(
                    f"Session {session_id}: Auto-exited LISTENING after "
                    f"{self.config.idle_timeout}s idle timeout"
                )

        except asyncio.CancelledError:
            pass  # Task was cancelled, normal operation

    def cleanup_session(self, session_id: str) -> None:
        """Clean up session state."""
        if session_id in self._states:
            state_data = self._states[session_id]
            if state_data.timeout_task:
                state_data.timeout_task.cancel()
            del self._states[session_id]
            logger.debug(f"Cleaned up listening state for session {session_id}")


__all__ = [
    "ListeningState",
    "ListeningStateConfig",
    "ListeningStateManager",
]
