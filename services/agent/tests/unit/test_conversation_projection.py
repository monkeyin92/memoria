from __future__ import annotations

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.conversation_projection import (
    CommitEvidence,
    CommittedTurn,
    ConversationProjection,
    FloorState,
    PhaseReason,
    ProjectionEventKind,
    ProjectionRejectReason,
    SpeakerEvidence,
    TurnEvidence,
    TurnPhase,
    transition_turn_phase,
)
from services.agent.src.orchestration.speech_timeline import (
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
)


def _segment(
    segment_id: str,
    *,
    start: int,
    end: int,
    text: str = "",
    revision: int = 1,
    kind: SegmentKind = SegmentKind.ASR_PARTIAL,
    final: bool = False,
    stream_epoch: int = 1,
    task_epoch: int | None = None,
    confidence: float | None = None,
    residual_echo_score: float | None = None,
    near_end_rms: float | None = None,
    far_end_rms: float | None = None,
    loss_concealed: bool = False,
) -> SpeechSegment:
    return SpeechSegment(
        session_id="session",
        stream_epoch=stream_epoch,
        provider_task_epoch=(
            0 if kind is SegmentKind.VAD else (1 if task_epoch is None else task_epoch)
        ),
        segment_id=segment_id,
        revision=revision,
        kind=kind,
        capture_start_sample=start,
        capture_end_sample=end,
        text=text,
        final=final,
        confidence=confidence,
        voiced_end_sample=start if kind is SegmentKind.VAD and final else None,
        residual_echo_score=residual_echo_score,
        near_end_rms=near_end_rms,
        far_end_rms=far_end_rms,
        loss_concealed=loss_concealed,
    )


def _apply(
    projection: ConversationProjection,
    segment: SpeechSegment,
    *,
    playback_active: bool | None = None,
    fence: GenerationFence | None = None,
    aec_verified: bool | None = None,
    discontinuity: bool = False,
) -> None:
    assert projection.timeline.add(segment)
    projection.apply_continuous_event(
        segment,
        turn_id_hint=1,
        playback_active=playback_active,
        fence=fence,
        aec_verified=aec_verified,
        discontinuity=discontinuity,
    )


def test_multi_vad_multi_revision_is_one_provisional_then_one_committed_turn() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("session", timeline)
    owner = SpeakerEvidence("owner", "voice_match", authority_verified=True)
    events = (
        _segment("vad-1", start=0, end=1, kind=SegmentKind.VAD),
        _segment("asr-1", start=0, end=160, text="你号"),
        _segment("asr-1", start=0, end=160, text="你好", revision=2),
        _segment("vad-2", start=320, end=321, kind=SegmentKind.VAD),
        _segment(
            "asr-2",
            start=160,
            end=320,
            text="世界",
            kind=SegmentKind.ASR_FINAL,
            final=True,
        ),
    )

    patches = []
    for index, segment in enumerate(events):
        assert timeline.add(segment)
        patch = projection.apply_continuous_event(
            segment,
            turn_id_hint=1,
            speaker_evidence=SpeakerEvidence() if index == 0 else owner,
        )
        assert patch is not None
        patches.append(patch)

    assert patches[0].kind is ProjectionEventKind.PROVISIONAL_STARTED
    assert all(patch.turn.provisional_id == patches[0].turn.provisional_id for patch in patches)
    assert patches[-1].turn.text == "你好 世界"
    assert patches[0].turn.speaker_evidence.speaker_class == "uncertain"
    assert patches[1].turn.speaker_evidence.speaker_class == "owner"
    assert patches[-1].turn.persist_as_turn is False
    assert patches[-1].turn.history_eligible is False
    assert timeline.projected_text(stream_epoch=1, start_sample=0, end_sample=320) == "你好 世界"

    canonical = timeline.canonical_text(stream_epoch=1, start_sample=0, end_sample=320)
    committed = projection.commit_turn(
        CommitEvidence(
            session_id="session",
            stream_epoch=1,
            capture_start_sample=0,
            capture_end_sample=320,
            text=canonical,
            fence=GenerationFence("session", 1, 1, 0),
            speaker_evidence=owner,
            history_eligible=True,
        )
    )
    assert isinstance(committed, CommittedTurn)
    assert committed.text == "你好 世界"
    assert committed.history_eligible is True
    assert committed.persist_as_turn is True
    assert projection.provisional is None


def test_commit_allows_asr_subrange_inside_leading_vad_provisional() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("session", timeline)
    vad = _segment("vad", start=0, end=1, kind=SegmentKind.VAD)
    final = _segment(
        "asr",
        start=160,
        end=640,
        text="梅莫里亚你好",
        kind=SegmentKind.ASR_FINAL,
        final=True,
    )
    for segment in (vad, final):
        assert timeline.add(segment)
        assert projection.apply_continuous_event(segment, turn_id_hint=1) is not None

    evidence = CommitEvidence(
        session_id="session",
        stream_epoch=1,
        capture_start_sample=160,
        capture_end_sample=640,
        text="梅莫里亚你好",
        fence=GenerationFence("session", 1, 1, 0),
        speaker_evidence=SpeakerEvidence("owner", "voice_match", True),
        history_eligible=True,
    )

    assert projection.validate_commit(evidence) is None
    committed = projection.commit_turn(evidence)

    assert isinstance(committed, CommittedTurn)
    assert committed.capture_start_sample == 160
    assert committed.capture_end_sample == 640


def test_commit_rejects_ranges_outside_the_provisional_interval() -> None:
    for start_sample, end_sample in ((80, 640), (160, 800)):
        timeline = SpeechTimeline()
        timeline.start_stream_epoch(1)
        projection = ConversationProjection("session", timeline)
        final = _segment(
            "asr",
            start=160,
            end=640,
            text="边界测试",
            kind=SegmentKind.ASR_FINAL,
            final=True,
        )
        assert timeline.add(final)
        assert projection.apply_continuous_event(final, turn_id_hint=1) is not None
        evidence = CommitEvidence(
            session_id="session",
            stream_epoch=1,
            capture_start_sample=start_sample,
            capture_end_sample=end_sample,
            text="边界测试",
            fence=GenerationFence("session", 1, 1, 0),
            speaker_evidence=SpeakerEvidence(),
            history_eligible=False,
        )

        assert projection.validate_commit(evidence) is ProjectionRejectReason.RANGE_MISMATCH
        assert projection.commit_turn(evidence) is ProjectionRejectReason.RANGE_MISMATCH
        assert projection.provisional is not None


def test_guest_or_ambiguous_evidence_cannot_gain_history_eligibility() -> None:
    for speaker in (
        SpeakerEvidence("guest", "owner_mismatch", authority_verified=True),
        SpeakerEvidence("uncertain", "authority_unavailable", authority_verified=False),
    ):
        timeline = SpeechTimeline()
        segment = _segment(
            "asr",
            start=0,
            end=160,
            text="访客内容",
            kind=SegmentKind.ASR_FINAL,
            final=True,
        )
        assert timeline.add(segment)
        projection = ConversationProjection("session", timeline)
        assert projection.apply_continuous_event(
            segment,
            turn_id_hint=1,
            speaker_evidence=speaker,
        )
        committed = projection.commit_turn(
            CommitEvidence(
                session_id="session",
                stream_epoch=1,
                capture_start_sample=0,
                capture_end_sample=160,
                text="访客内容",
                fence=GenerationFence("session", 1, 1, 0),
                speaker_evidence=speaker,
                history_eligible=True,
            )
        )
        assert isinstance(committed, CommittedTurn)
        assert committed.history_eligible is False


def test_rejected_or_backchannel_candidate_is_discarded_without_persistence() -> None:
    timeline = SpeechTimeline()
    segment = _segment("asr", start=0, end=160, text="嗯")
    assert timeline.add(segment)
    projection = ConversationProjection("session", timeline)
    assert projection.apply_continuous_event(segment, turn_id_hint=1)

    mismatch = projection.commit_turn(
        CommitEvidence(
            session_id="session",
            stream_epoch=1,
            capture_start_sample=0,
            capture_end_sample=160,
            text="另一句",
            fence=GenerationFence("session", 1, 1, 0),
            speaker_evidence=SpeakerEvidence(),
            history_eligible=False,
        )
    )
    assert mismatch is ProjectionRejectReason.TEXT_MISMATCH
    discarded = projection.discard_provisional(1, "backchannel")
    assert discarded is not None
    assert discarded.kind is ProjectionEventKind.PROVISIONAL_DISCARDED
    assert discarded.to_payload()["persist_as_turn"] is False
    assert discarded.to_payload()["history_eligible"] is False
    assert discarded.reason == "backchannel"


def test_discarded_turn_hint_cannot_reuse_provisional_identity_or_revision() -> None:
    timeline = SpeechTimeline()
    first_segment = _segment("asr-1", start=0, end=160, text="等等")
    assert timeline.add(first_segment)
    projection = ConversationProjection("session", timeline)

    first = projection.apply_continuous_event(first_segment, turn_id_hint=1)
    assert first is not None
    discarded = projection.discard_provisional(1, "backchannel")
    assert discarded is not None

    next_segment = _segment("asr-2", start=160, end=320, text="下一句")
    assert timeline.add(next_segment)
    next_started = projection.apply_continuous_event(next_segment, turn_id_hint=1)
    assert next_started is not None

    assert first.turn.revision == 1
    assert discarded.turn.revision == 2
    assert next_started.turn.provisional_id != first.turn.provisional_id
    assert next_started.turn.revision > discarded.turn.revision

    committed = projection.commit_turn(
        CommitEvidence(
            session_id="session",
            stream_epoch=1,
            capture_start_sample=160,
            capture_end_sample=320,
            text="下一句",
            fence=GenerationFence("session", 1, 1, 0),
            speaker_evidence=SpeakerEvidence("owner", "voice_match", True),
            history_eligible=True,
        )
    )
    assert isinstance(committed, CommittedTurn)
    assert committed.provisional_id == next_started.turn.provisional_id
    assert committed.revision > next_started.turn.revision


def test_late_speaker_evidence_patches_existing_provisional_without_new_turn() -> None:
    timeline = SpeechTimeline()
    segment = _segment("asr", start=0, end=160, text="身份稍后确认")
    assert timeline.add(segment)
    projection = ConversationProjection("session", timeline)
    started = projection.apply_continuous_event(segment, turn_id_hint=1)
    assert started is not None

    patch = projection.apply_speaker_evidence(
        SpeakerEvidence("owner", "voice_match", authority_verified=True)
    )
    assert patch is not None
    assert patch.kind is ProjectionEventKind.PROVISIONAL_PATCH
    assert patch.turn.provisional_id == started.turn.provisional_id
    assert patch.turn.text == started.turn.text
    assert patch.turn.revision == started.turn.revision + 1


def test_phase_follows_idle_acoustic_semantic_end_candidate_commit() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("session", timeline)
    owner = SpeakerEvidence("owner", "voice_match", True)
    assert projection.phase is TurnPhase.IDLE
    _apply(projection, _segment("vad-start", start=0, end=320, kind=SegmentKind.VAD))
    assert projection.phase is TurnPhase.ACOUSTIC_ONLY
    assert projection.floor_state is FloorState.USER_HOLDS_FLOOR
    assert projection.current_frame is not None
    assert not projection.current_frame.allows_generation_cancel()
    _apply(
        projection,
        _segment("asr", start=0, end=1600, text="今天天气怎么样"),
        fence=GenerationFence("session", 1, 1, 0),
    )
    assert projection.phase is TurnPhase.SEMANTIC_SPEAKING
    assert projection.current_frame is not None
    assert projection.current_frame.allows_generation_cancel()
    _apply(
        projection,
        _segment("vad-end", start=1600, end=1601, kind=SegmentKind.VAD, final=True),
    )
    assert projection.phase is TurnPhase.END_CANDIDATE
    assert projection.pending_endpoint is True
    assert projection.phase_reason is PhaseReason.ENDPOINT_COVERED
    committed = projection.commit_turn(
        CommitEvidence(
            session_id="session",
            stream_epoch=1,
            capture_start_sample=0,
            capture_end_sample=1600,
            text="今天天气怎么样",
            fence=GenerationFence("session", 1, 1, 0),
            speaker_evidence=owner,
            history_eligible=True,
        )
    )
    assert isinstance(committed, CommittedTurn)
    assert projection.phase is TurnPhase.IDLE
    assert projection.provisional is None
    assert projection.pending_endpoint is False


def test_end_candidate_retracts_on_child_pause_continuation() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("session", timeline)
    _apply(projection, _segment("vad-start", start=0, end=320, kind=SegmentKind.VAD))
    _apply(projection, _segment("asr-1", start=0, end=1600, text="我想"))
    _apply(
        projection,
        _segment("vad-end", start=1600, end=1601, kind=SegmentKind.VAD, final=True),
    )
    assert projection.phase is TurnPhase.END_CANDIDATE
    _apply(projection, _segment("vad-resume", start=8000, end=8320, kind=SegmentKind.VAD))
    assert projection.phase is TurnPhase.SEMANTIC_SPEAKING
    assert projection.phase_reason is PhaseReason.PENDING_ENDPOINT_RETRACTED
    _apply(projection, _segment("asr-2", start=1600, end=9600, text="我想再问一句", revision=2))
    assert projection.phase is TurnPhase.SEMANTIC_SPEAKING


def test_playback_acoustic_only_does_not_allow_cancel() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("session", timeline)
    _apply(
        projection,
        _segment("vad-start", start=0, end=320, kind=SegmentKind.VAD),
        playback_active=True,
        fence=GenerationFence("session", 1, 2, 0),
    )
    assert projection.phase is TurnPhase.ACOUSTIC_ONLY
    assert projection.current_frame is not None
    assert not projection.current_frame.allows_generation_cancel()
    assert projection.current_frame.must_hold() is False
    _apply(
        projection,
        _segment("嗯", start=0, end=480, text="嗯"),
        playback_active=True,
    )
    assert projection.phase is TurnPhase.BACKCHANNEL
    _apply(
        projection,
        _segment("content", start=0, end=2400, text="不要再说了", revision=2),
        playback_active=True,
    )
    assert projection.phase is TurnPhase.SEMANTIC_SPEAKING
    assert projection.current_frame is not None
    assert projection.current_frame.allows_generation_cancel()


def test_echo_loss_and_low_vad_enter_uncertain() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("session", timeline)
    _apply(
        projection,
        _segment(
            "echo",
            start=0,
            end=320,
            kind=SegmentKind.VAD,
            residual_echo_score=0.9,
            near_end_rms=0.1,
            far_end_rms=0.8,
        ),
        playback_active=True,
        aec_verified=True,
    )
    assert projection.phase is TurnPhase.UNCERTAIN
    assert projection.phase_reason is PhaseReason.ECHO_OR_NOISE
    projection.reset_phase(stream_epoch=1)
    _apply(
        projection,
        _segment("low-vad", start=320, end=640, kind=SegmentKind.VAD, confidence=0.2),
        playback_active=True,
    )
    assert projection.phase is TurnPhase.UNCERTAIN
    assert projection.phase_reason is PhaseReason.LOW_VAD
    projection.reset_phase(stream_epoch=1)
    _apply(
        projection,
        _segment("loss", start=640, end=960, kind=SegmentKind.VAD, loss_concealed=True),
    )
    assert projection.phase is TurnPhase.UNCERTAIN
    assert projection.phase_reason is PhaseReason.LOSS_CONCEALED


def test_stale_stream_fence_and_asr_revision_do_not_transition() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("session", timeline)
    live = GenerationFence("session", 1, 2, 0)
    _apply(
        projection,
        _segment("vad", start=0, end=320, kind=SegmentKind.VAD),
        fence=live,
    )
    assert projection.phase is TurnPhase.ACOUSTIC_ONLY
    _apply(
        projection,
        _segment("stale-asr", start=0, end=1600, text="迟到语义"),
        fence=GenerationFence("session", 1, 1, 0),
    )
    assert projection.phase is TurnPhase.ACOUSTIC_ONLY
    _apply(projection, _segment("asr", start=0, end=800, text="你好", revision=2), fence=live)
    assert projection.phase is TurnPhase.SEMANTIC_SPEAKING
    old_revision = _segment("asr", start=0, end=900, text="更旧", revision=1)
    assert projection.timeline.add(old_revision) is False
    assert projection.apply_continuous_event(old_revision, turn_id_hint=1) is None
    assert projection.phase is TurnPhase.SEMANTIC_SPEAKING
    assert (
        transition_turn_phase(
            TurnPhase.ACOUSTIC_ONLY,
            TurnEvidence(stale_stream=True, vad_start=True),
        )
        is None
    )


def test_late_final_cannot_rewrite_committed_watermark() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("session", timeline)
    _apply(projection, _segment("vad", start=0, end=320, kind=SegmentKind.VAD))
    _apply(
        projection,
        _segment(
            "final", start=0, end=1600, text="提交文本", kind=SegmentKind.ASR_FINAL, final=True
        ),
    )
    committed = projection.commit_turn(
        CommitEvidence(
            session_id="session",
            stream_epoch=1,
            capture_start_sample=0,
            capture_end_sample=1600,
            text="提交文本",
            fence=GenerationFence("session", 1, 1, 0),
            speaker_evidence=SpeakerEvidence("owner", "voice_match", True),
            history_eligible=True,
        )
    )
    assert isinstance(committed, CommittedTurn)
    timeline.commit_range(stream_epoch=1, start_sample=0, end_sample=1600)
    late = _segment(
        "late",
        start=0,
        end=1600,
        text="覆盖已提交",
        kind=SegmentKind.ASR_FINAL,
        final=True,
        revision=9,
    )
    assert timeline.add(late) is False
    assert projection.apply_continuous_event(late, turn_id_hint=1) is None
    assert projection.phase is TurnPhase.IDLE
    assert projection.provisional is None


def test_phase_and_floor_update_atomically_and_frames_stay_bounded() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("session", timeline)
    for index in range(8):
        start = index * 1_280
        _apply(
            projection,
            _segment(f"vad-{index}", start=start, end=start + 320, kind=SegmentKind.VAD),
        )
    assert projection.phase is TurnPhase.ACOUSTIC_ONLY
    assert projection.current_frame is not None
    assert projection.current_frame.frame_index == 7
    assert projection.consecutive_phase_frames <= 32
    patch = projection.apply_continuous_event(
        projection.timeline.pending[-1],
        turn_id_hint=1,
    )
    assert patch is None or patch.turn.floor_state is projection.floor_state
    assert projection.phase is TurnPhase.ACOUSTIC_ONLY


def test_kws_does_not_wait_for_projection_frame() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("session", timeline)
    _apply(projection, _segment("vad", start=0, end=320, kind=SegmentKind.VAD))
    kws = SpeechSegment(
        session_id="session",
        stream_epoch=1,
        provider_task_epoch=0,
        segment_id="kws",
        revision=1,
        kind=SegmentKind.KWS,
        capture_start_sample=320,
        capture_end_sample=640,
        text="停止",
        final=True,
        hard_stop=True,
    )
    assert timeline.add(kws)
    assert projection.apply_continuous_event(kws, turn_id_hint=1) is None
    assert projection.phase is TurnPhase.ACOUSTIC_ONLY


def test_uncertain_recovers_to_end_candidate_when_asr_covers() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("session", timeline)
    _apply(projection, _segment("vad-start", start=0, end=320, kind=SegmentKind.VAD))
    _apply(projection, _segment("partial", start=0, end=800, text="今天"))
    _apply(
        projection,
        _segment("vad-end", start=1600, end=1601, kind=SegmentKind.VAD, final=True),
    )
    assert projection.phase is TurnPhase.UNCERTAIN
    assert projection.phase_reason is PhaseReason.ASR_UNCOVERED
    _apply(
        projection,
        _segment(
            "final",
            start=0,
            end=1700,
            text="今天天气",
            kind=SegmentKind.ASR_FINAL,
            final=True,
            revision=2,
        ),
    )
    assert projection.phase is TurnPhase.END_CANDIDATE
    assert projection.phase_reason is PhaseReason.EVIDENCE_RECOVERED
