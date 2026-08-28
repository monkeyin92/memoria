"""Authenticated device VAD events projected onto the AgentSession state seam."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from time import monotonic
from typing import Any

from livekit import rtc

DEVICE_VAD_TOPIC = "voice-agent.device-vad"
DEVICE_MICROPHONE_TRACK_NAME = "device-microphone"
# FunASR finals on this path often arrive 7s after vad.end. LiveKit only
# keeps a nonempty FINAL if it lands before transcript_timeout.
DEVICE_TURN_TRANSCRIPT_TIMEOUT_S = 8.0
DEVICE_EMPTY_FINAL_GRACE_S = 2.0
DEVICE_POST_PLAYBACK_HOLDOFF_S = 2.0
DEVICE_ENDPOINTING_MIN_DELAY_S = 0.05
DEVICE_ENDPOINTING_MAX_DELAY_S = 0.40

logger = logging.getLogger(__name__)


def device_turn_commit_busy(task: Any) -> bool:
    """Return whether a device VAD wait-then-commit is already running."""

    done = getattr(task, "done", None)
    return task is not None and not (callable(done) and done())


async def commit_device_user_turn_after_asr(
    session: Any,
    *,
    session_id: str,
    stt: Any,
    since: float,
    timeout: float = DEVICE_TURN_TRANSCRIPT_TIMEOUT_S,
) -> bool:
    """Wait for a nonempty FunASR final, then commit the LiveKit user turn."""

    waiter = getattr(stt, "wait_for_nonempty_final", None)
    if callable(waiter):
        ready = await waiter(
            since=since,
            timeout=timeout,
            empty_grace_s=DEVICE_EMPTY_FINAL_GRACE_S,
        )
        logger.info("device VAD asr_ready=%s session_id=%s", ready, session_id)
        if not ready:
            logger.info(
                "user_turn_ignored reason=empty_transcript session_id=%s",
                session_id,
            )
            return False
    return commit_device_user_turn(session, session_id=session_id)


def commit_device_user_turn(session: Any, *, session_id: str) -> bool:
    """Commit one LiveKit user turn after an authoritative device VAD end.

    Device sessions cannot wait for FunASR ``END_OF_SPEECH``: empty or
    unusable finals never set the STT speaking flag, so ``turn_detection=stt``
    leaves the AgentSession stuck in ``user_speaking``.
    """

    agent_state = str(getattr(session, "agent_state", "") or "")
    if agent_state in {"speaking", "thinking"}:
        logger.info(
            "device VAD end skipped turn commit agent_state=%s session_id=%s",
            agent_state,
            session_id,
        )
        return False
    commit = getattr(session, "commit_user_turn", None)
    if not callable(commit):
        return False
    commit(
        transcript_timeout=DEVICE_TURN_TRANSCRIPT_TIMEOUT_S,
        stt_flush_duration=0.0,
    )
    logger.info("device VAD committed user turn session_id=%s", session_id)
    return True


@dataclass(slots=True)
class DeviceVadProjector:
    """Consume only reliable VAD facts from this session's device microphone."""

    session: Any
    session_id: str
    on_endpoint: Callable[[], None] | None = None
    _last_sample: int = 0
    _active: bool = False
    _participant_sid: str | None = None
    _participant_identity: str | None = None
    _holdoff_until: float = 0.0

    def begin_playback_holdoff(
        self,
        duration_s: float = DEVICE_POST_PLAYBACK_HOLDOFF_S,
    ) -> None:
        """Ignore board VAD until on-device playback has had time to finish."""

        self._holdoff_until = monotonic() + duration_s
        if self._active:
            self._active = False
            logger.info(
                "device VAD holdoff cancelled active speech session_id=%s",
                self.session_id,
            )
        logger.info(
            "device VAD holdoff_s=%.2f session_id=%s",
            duration_s,
            self.session_id,
        )

    def accept(self, packet: Any) -> bool:
        if getattr(packet, "topic", None) != DEVICE_VAD_TOPIC:
            return False
        if getattr(packet, "kind", None) != rtc.DataPacketKind.KIND_RELIABLE:
            return True
        if monotonic() < self._holdoff_until:
            return True
        participant = getattr(packet, "participant", None)
        if not _owns_device_microphone(participant):
            return True
        participant_sid = str(getattr(participant, "sid", ""))
        participant_identity = str(getattr(participant, "identity", ""))
        if not participant_sid or (
            self._participant_identity is not None
            and participant_identity != self._participant_identity
        ):
            return True
        try:
            event = json.loads(bytes(packet.data).decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return True
        if not isinstance(event, dict) or set(event) != {
            "type",
            "session_id",
            "sample_position",
        }:
            return True
        event_type = event.get("type")
        sample_position = event.get("sample_position")
        if (
            event.get("session_id") != self.session_id
            or event_type not in {"vad.start", "vad.end"}
            or isinstance(sample_position, bool)
            or not isinstance(sample_position, int)
            or not 0 <= sample_position <= 0xFFFFFFFFFFFFFFFF
            or sample_position < self._last_sample
        ):
            return True
        if event_type == "vad.start":
            if self._active:
                return True
            self._participant_sid = participant_sid
            self._participant_identity = participant_identity
            self._active = True
            state = "speaking"
        else:
            if not self._active:
                return True
            if participant_sid != self._participant_sid:
                self._participant_sid = participant_sid
            self._active = False
            state = "listening"
        self._last_sample = sample_position
        update_state = getattr(self.session, "_update_user_state", None)
        if callable(update_state):
            update_state(state)
        else:
            self.session.emit("user_state_changed", SimpleNamespace(new_state=state))
        if state == "listening" and self.on_endpoint is not None:
            self.on_endpoint()
        return True


def _owns_device_microphone(participant: Any) -> bool:
    if participant is None or not str(getattr(participant, "identity", "")):
        return False
    publications = getattr(participant, "track_publications", None)
    if not isinstance(publications, Mapping):
        return False
    return any(
        str(getattr(publication, "name", "")) == DEVICE_MICROPHONE_TRACK_NAME
        and getattr(publication, "source", None) == rtc.TrackSource.SOURCE_MICROPHONE
        for publication in publications.values()
    )


__all__ = [
    "DEVICE_EMPTY_FINAL_GRACE_S",
    "DEVICE_ENDPOINTING_MAX_DELAY_S",
    "DEVICE_ENDPOINTING_MIN_DELAY_S",
    "DEVICE_MICROPHONE_TRACK_NAME",
    "DEVICE_POST_PLAYBACK_HOLDOFF_S",
    "DEVICE_TURN_TRANSCRIPT_TIMEOUT_S",
    "DEVICE_VAD_TOPIC",
    "DeviceVadProjector",
    "commit_device_user_turn",
    "commit_device_user_turn_after_asr",
    "device_turn_commit_busy",
]
