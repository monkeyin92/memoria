"""Authenticated device VAD events projected onto the AgentSession state seam."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from types import SimpleNamespace
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
    on_start: Callable[[], None] | None = None
    on_endpoint: Callable[[], None] | None = None
    on_endpoint_deferred: Callable[[float], None] | None = None
    _last_sample: int = 0
    _active: bool = False
    _participant_sid: str | None = None
    _participant_identity: str | None = None
    _holdoff_until: float = 0.0
    _speech_started_at: float = 0.0
    _deferred_endpoint: bool = False
    _playback_active: bool = False
    _speech_started_during_playback: bool = False

    def begin_playback_holdoff(
        self,
        duration_s: float = DEVICE_POST_PLAYBACK_HOLDOFF_S,
    ) -> None:
        """Defer turn commits until on-device playback has had time to finish.

        This must never drop ``vad.start``/``vad.end``. The board only emits
        them on state changes, so a dropped pair leaves this projector
        permanently desynchronised from the board and gates the FunASR uplink
        off for the rest of the session. Only the commit is deferred.
        """

        self._holdoff_until = monotonic() + duration_s
        logger.info(
            "device VAD holdoff_s=%.2f session_id=%s",
            duration_s,
            self.session_id,
        )

    @property
    def speech_started_at(self) -> float:
        """Monotonic timestamp of the ``vad.start`` that opened the uplink."""

        return self._speech_started_at

    @property
    def holdoff_remaining_s(self) -> float:
        return max(0.0, self._holdoff_until - monotonic())

    def set_playback_active(self, active: bool) -> None:
        """Track whether the device speaker is still emitting audio.

        The board has no hardware AEC reference, so speech that begins while
        the speaker is live is treated as loudspeaker echo. Device sessions are
        controlled half-duplex and never accept barge-in, so discarding it
        matches the product contract instead of losing a real turn.
        """

        self._playback_active = bool(active)

    @property
    def speech_started_during_playback(self) -> bool:
        return self._speech_started_during_playback

    def _emit_endpoint(self) -> None:
        """Commit now, or once the playback holdoff has elapsed."""

        remaining = self.holdoff_remaining_s
        if remaining <= 0.0:
            self._deferred_endpoint = False
            if self.on_endpoint is not None:
                self.on_endpoint()
            return
        if self.on_endpoint_deferred is not None:
            self._deferred_endpoint = True
            self.on_endpoint_deferred(remaining)
            return
        logger.warning(
            "device VAD endpoint deferred without handler session_id=%s",
            self.session_id,
        )
        if self.on_endpoint is not None:
            self.on_endpoint()

    def accept(self, packet: Any) -> bool:
        if getattr(packet, "topic", None) != DEVICE_VAD_TOPIC:
            return False
        if getattr(packet, "kind", None) != rtc.DataPacketKind.KIND_RELIABLE:
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
            self._speech_started_at = monotonic()
            self._speech_started_during_playback = self._playback_active
            # A new utterance supersedes a commit still waiting out the holdoff.
            self._deferred_endpoint = False
            state = "speaking"
        else:
            if not self._active:
                return True
            if participant_sid != self._participant_sid:
                self._participant_sid = participant_sid
            self._active = False
            state = "listening"
        self._last_sample = sample_position
        if state == "speaking":
            if self.on_start is not None:
                self.on_start()
        else:
            self._emit_endpoint()
        update_state = getattr(self.session, "_update_user_state", None)
        if callable(update_state):
            update_state(state)
        else:
            self.session.emit("user_state_changed", SimpleNamespace(new_state=state))
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
