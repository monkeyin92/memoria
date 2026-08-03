from __future__ import annotations

from services.agent.src.orchestration.speech_timeline import (
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
)


def segment(
    segment_id: str,
    *,
    start: int,
    end: int,
    revision: int = 1,
    kind: SegmentKind = SegmentKind.ASR_FINAL,
    final: bool = True,
    text: str = "",
    epoch: int = 1,
) -> SpeechSegment:
    return SpeechSegment(
        session_id="session",
        stream_epoch=epoch,
        provider_task_epoch=1,
        segment_id=segment_id,
        revision=revision,
        kind=kind,
        capture_start_sample=start,
        capture_end_sample=end,
        text=text,
        final=final,
    )


def test_revisions_replace_partials_without_fifo_assumptions() -> None:
    timeline = SpeechTimeline()
    assert timeline.add(segment("sentence", start=0, end=320, final=False, text="我今"))
    assert timeline.add(segment("sentence", start=0, end=320, revision=2, text="我今天"))

    committed = timeline.commit_range(stream_epoch=1, start_sample=0, end_sample=320)

    assert [item.text for item in committed] == ["我今天"]
    assert timeline.committed_sample == 320


def test_late_results_before_watermark_and_old_epoch_are_dropped() -> None:
    timeline = SpeechTimeline()
    timeline.add(segment("old", start=0, end=320, text="旧"))
    timeline.commit_range(stream_epoch=1, start_sample=0, end_sample=320)

    assert not timeline.add(segment("late", start=0, end=160, text="迟到"))
    timeline.start_stream_epoch(2)
    assert not timeline.add(segment("epoch-1", start=0, end=160, text="旧 epoch", epoch=1))
    assert timeline.add(segment("current", start=0, end=160, text="新 epoch", epoch=2))


def test_multiple_vad_intervals_can_commit_as_one_logical_turn() -> None:
    timeline = SpeechTimeline()
    timeline.add(segment("vad-1", start=0, end=320, kind=SegmentKind.VAD))
    timeline.add(segment("asr-1", start=0, end=320, text="我今天"))
    timeline.add(segment("vad-2", start=640, end=960, kind=SegmentKind.VAD))
    timeline.add(segment("asr-2", start=640, end=960, text="去公园"))

    assert timeline.canonical_text(
        stream_epoch=1,
        start_sample=0,
        end_sample=960,
    ) == "我今天 去公园"
