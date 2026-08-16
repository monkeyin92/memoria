from __future__ import annotations

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.conversation_projection import (
    CommitEvidence,
    CommittedTurn,
    ConversationProjection,
    ProjectionEventKind,
    ProjectionRejectReason,
    SpeakerEvidence,
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
) -> SpeechSegment:
    return SpeechSegment(
        session_id="session",
        stream_epoch=1,
        provider_task_epoch=1 if kind is not SegmentKind.VAD else 0,
        segment_id=segment_id,
        revision=revision,
        kind=kind,
        capture_start_sample=start,
        capture_end_sample=end,
        text=text,
        final=final,
        voiced_end_sample=start if kind is SegmentKind.VAD and final else None,
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
