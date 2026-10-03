"""Mutable state owned by one Media Voice session."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.conversation_projection import ConversationProjection
from services.agent.src.voice_core.asr_stream_supervisor import ASRStreamSupervisor
from services.agent.src.voice_core.media_audio_ingress import MediaAudioIngressState
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session_pending_turn import PendingTurn
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputClaim,
    MediaVoiceProvider,
    OutputDispatchResult,
    OutputOwnerLease,
    OutputWork,
)
from services.agent.src.voice_core.playback_ledger import PlaybackLedger
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryLedger


@dataclass(slots=True)
class OutputState:
    """The session's reply output: owner lease, dispatch work and playback."""

    playback: PlaybackLedger = field(default_factory=PlaybackLedger)
    reply_delivery: ReplyDeliveryLedger = field(default_factory=ReplyDeliveryLedger)
    output_sequence: int = 0
    output_text_offset: int = 0
    assistant_text: str = ""
    tts_started_ns: int | None = None
    first_audio_observed: bool = False
    provider_complete: bool = False
    output_complete_emitted: bool = False
    reply_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reply_task: asyncio.Task[Any] | None = None
    output_owner: OutputOwnerLease | None = None
    output_work: dict[str, OutputWork] = field(default_factory=dict)
    output_dispatch_task: asyncio.Task[OutputDispatchResult] | None = None
    output_retry_task: asyncio.Task[bool] | None = None
    output_results: list[OutputDispatchResult] = field(default_factory=list)
    delegation_output_claims: dict[GenerationFence, DelegationOutputClaim] = field(
        default_factory=dict
    )

    def begin_turn_output(self, fence: GenerationFence) -> bool:
        """Make ``fence`` the playback generation and zero the per-response counters.

        Returns False and keeps every counter when frames of this fence are already on the wire.
        A live-lookup acknowledgement is started by the delegation while the commit that created
        the fence is still finishing; zeroing the counters then makes the next output of the same
        fence restart at sequence 0, which the downlink rejects as ``sequence_gap`` (it expects the
        next number), so the answer after the acknowledgement never plays and the device stays in
        SPEAKING (2026-10-01, a lookup that ended without a result).
        """

        self.playback.start(fence)
        if self.audio_sent_for(fence):
            return False
        self.output_sequence = 0
        self.output_text_offset = 0
        self.assistant_text = ""
        self.provider_complete = False
        self.output_complete_emitted = False
        self.tts_started_ns = None
        self.first_audio_observed = False
        return True

    def audio_sent_for(self, fence: GenerationFence) -> bool:
        """True once a frame of ``fence`` was accepted for the device, whatever dispatch sent it."""

        delivery = self.reply_delivery.get(fence)
        return (delivery is not None and delivery.first_frame_sent) or (
            self.playback.received_sequence(fence) >= 0
        )


@dataclass(slots=True)
class MediaVoiceSessionState:
    """One registry-owned authority record; collaborators never clone it."""

    identity: SessionIdentity
    runtime: DuplexRuntime
    provider: MediaVoiceProvider
    asr: ASRStreamSupervisor
    projection: ConversationProjection
    ingress: MediaAudioIngressState
    output: OutputState = field(default_factory=OutputState)
    pending: PendingTurn = field(default_factory=PendingTurn)
    stream_epoch: int = 0
    floor_epoch: int = 0
    turn_started_ns: int | None = None
    turn_commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The terminal close owns cancellation even when endpoint re-arming shields
    # the caller. Only one preparation may run under turn_commit_lock.
    turn_commit_task: asyncio.Task[tuple[GenerationFence | None, str | None]] | None = None
    turn_endpoint_task: asyncio.Task[None] | None = None
    # Uplink-capture boundary of the last completed/failed playback window,
    # including an echo-tail margin: finals that start before it may still be
    # the reply's tail on the uplink and must not endpoint or own a turn.
    last_playback_end_sample: int | None = None
    # Highest accepted ASR evidence end (finals and non-empty partials); feeds
    # the playback boundary snapshot taken when playback completes.
    last_asr_evidence_end_sample: int = 0
    # When the last ASR final was accepted (monotonic); the commit timing log reports how long after it the
    # turn started and finished committing.
    last_final_accepted_at: float | None = None
    observed_within_turn_pause_s: float | None = None
    # Normalised text of the last media turn that actually committed.  A
    # duplicate ASR final of one question commits a contiguous extension of the
    # same range while its reply is still synthesizing; opening a second turn
    # there releases the first delegation and cancels its audible cue, leaving a
    # truncated cue plus silence (epoch 1900).  Identical text carries no new
    # information, so that repeat is skipped while the reply is in flight.
    last_committed_turn_text: str = ""
    last_committed_turn_fence: GenerationFence | None = None
    last_committed_turn_at: float | None = None
    # A live-lookup filler is a user-facing cue, so the device may hear it at
    # most once per lookup burst.  One question can be transcribed into several
    # finals, and each final commits its own turn and its own delegation, so a
    # sibling delegation cannot see the acknowledgement its predecessor already
    # played.  Remember that acknowledgement's fence (and when it was admitted)
    # so the deep result is not prefixed with the same phrase again.
    live_lookup_filler_fence: GenerationFence | None = None
    live_lookup_filler_admitted_at: float | None = None
    owner_silence_task: asyncio.Task[None] | None = None
    owner_silence_deadline: float | None = None
    owner_silence_remaining_s: float | None = None
    owner_silence_grace_used: bool = False
    owner_silence_grace_deadline: float | None = None
    #: Fences an expired timer waiting for standby_lock; never resets within
    #: the session. Incremented by accepted owner-activity evidence (an admitted VAD
    #: edge or an accepted transcript). A close that snapshotted an
    #: older revision is stale and must not fire.
    owner_silence_activity_revision: int = 0
    standby_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    standby_requested: bool = False
    standby_reason: str | None = None
    conversation_initiation_provisional_id: str | None = None
    conversation_yield_candidate_fence: GenerationFence | None = None
    device_wake_ack_fence: GenerationFence | None = None
    device_wake_ack_pending: bool = False
    speaker_enrollment_task: asyncio.Task[None] | None = None
    # P0-04 D3: when the session began (monotonic) for a minor's time limit,
    # and whether it heard the crisis reply (never cut short afterwards).
    started_at: float = field(default_factory=time.monotonic)
    crisis_reply_heard: bool = False
    pending_missed_hearing_nudge: bool = False
    missed_hearing_nudge_count: int = 0
    last_missed_hearing_nudge_at: float | None = None
    closed: bool = False
