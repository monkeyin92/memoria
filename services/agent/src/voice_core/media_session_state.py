"""Mutable state owned by one Media Voice session."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.conversation_projection import ConversationProjection
from services.agent.src.voice_core.asr_stream_supervisor import ASRStreamSupervisor
from services.agent.src.voice_core.media_audio_ingress import MediaAudioIngressState
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputClaim,
    MediaVoiceProvider,
    OutputDispatchResult,
    OutputOwnerLease,
    OutputWork,
)
from services.agent.src.voice_core.playback_ledger import PlaybackLedger
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryLedger
from services.agent.src.voice_core.speech_timeline import ASRResult


@dataclass(slots=True)
class MediaVoiceSessionState:
    """One registry-owned authority record; collaborators never clone it."""

    identity: SessionIdentity
    runtime: DuplexRuntime
    provider: MediaVoiceProvider
    asr: ASRStreamSupervisor
    projection: ConversationProjection
    ingress: MediaAudioIngressState
    playback: PlaybackLedger = field(default_factory=PlaybackLedger)
    reply_delivery: ReplyDeliveryLedger = field(default_factory=ReplyDeliveryLedger)
    output_sequence: int = 0
    output_text_offset: int = 0
    assistant_text: str = ""
    stream_epoch: int = 0
    floor_epoch: int = 0
    turn_started_ns: int | None = None
    tts_started_ns: int | None = None
    first_audio_observed: bool = False
    provider_complete: bool = False
    output_complete_emitted: bool = False
    reply_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reply_task: asyncio.Task[Any] | None = None
    output_owner: OutputOwnerLease | None = None
    output_work: dict[str, OutputWork] = field(default_factory=dict)
    output_dispatch_task: asyncio.Task[OutputDispatchResult] | None = None
    output_results: list[OutputDispatchResult] = field(default_factory=list)
    delegation_output_claims: dict[GenerationFence, DelegationOutputClaim] = field(
        default_factory=dict
    )
    committed_asr_keys: OrderedDict[tuple[int, str, int, int], None] = field(
        default_factory=OrderedDict
    )
    turn_start_sample: int | None = None
    turn_end_sample: int | None = None
    turn_endpoint_sample: int | None = None
    turn_retire_sample: int | None = None
    turn_endpoint_task: asyncio.Task[None] | None = None
    turn_endpoint_grace_deadline: float | None = None
    turn_endpoint_tail_deadline: float | None = None
    turn_endpoint_timeout_handle: asyncio.TimerHandle | None = None
    turn_commit_retry_task: asyncio.Task[None] | None = None
    turn_commit_retry_attempt: int = 0
    turn_commit_retry_stream_epoch: int | None = None
    turn_commit_retry_endpoint_sample: int | None = None
    observed_within_turn_pause_s: float | None = None
    pending_partial: ASRResult | None = None
    owner_silence_task: asyncio.Task[None] | None = None
    owner_silence_deadline: float | None = None
    owner_silence_remaining_s: float | None = None
    owner_silence_grace_used: bool = False
    # Independent wall-clock bound for one accepted user utterance.  This is
    # deliberately separate from owner-silence timing: a stuck VAD stream
    # must eventually fail closed even while the owner is still speaking.
    max_user_speech_task: asyncio.Task[None] | None = None
    max_user_speech_deadline: float | None = None
    standby_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    standby_requested: bool = False
    standby_reason: str | None = None
    conversation_initiation_provisional_id: str | None = None
    conversation_yield_candidate_fence: GenerationFence | None = None
    closed: bool = False
