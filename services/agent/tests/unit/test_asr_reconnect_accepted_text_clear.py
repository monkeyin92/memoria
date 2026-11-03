"""Regression test for _accepted_text not being cleared on reconnect.

This bug caused text from old stream epochs to accumulate indefinitely,
leading to memory leaks and potential incorrect prefix matching in
_untimed_tail_after_committed_prefix when sample ranges collided.

GitHub issue: card5 vol30 hardware run found text lengths accumulating
"""

from __future__ import annotations

from services.agent.src.orchestration.speech_timeline import SpeechTimeline
from services.agent.src.voice_core.asr_stream_supervisor import ASRStreamSupervisor
from services.agent.src.voice_core.speech_timeline import ASRResult


def test_accepted_text_cleared_on_reconnect():
    """Verify _accepted_text is cleared when reconnect() is called.

    Before the fix, _accepted_text persisted across reconnects while all other
    state (_revisions, _final_intervals, etc.) was cleared. This caused:
    1. Memory leak - text from old stream epochs accumulated indefinitely
    2. Potential incorrect prefix matching if capture_start_sample collided
    """
    timeline = SpeechTimeline()
    supervisor = ASRStreamSupervisor(
        timeline=timeline,
        stream_epoch=1,
        task_epoch=1,
        sample_rate=16000,
        reconnect_audio_ms=500,
        max_result_history=10,
    )

    # Accept a result in stream epoch 1
    result1 = ASRResult(
        stream_epoch=1,
        task_epoch=1,
        sentence_id="sent1",
        revision=1,
        is_final=False,
        capture_start_sample=0,
        capture_end_sample=16000,
        text="Hello world",
        rescue_synthesized=False,
    )

    decision = supervisor.accept_result(result1, session_id="test_session")
    assert decision.accepted is not None, "Result should be accepted"
    assert len(supervisor._accepted_text) > 0, "_accepted_text should have entries after accept"

    # Reconnect to stream epoch 2
    reconnected = supervisor.reconnect(stream_epoch=2)
    assert reconnected, "Reconnect should succeed"

    # BUG FIX: _accepted_text must be cleared on reconnect
    assert (
        len(supervisor._accepted_text) == 0
    ), "_accepted_text must be cleared on reconnect to prevent memory leak"


def test_no_cross_stream_text_accumulation():
    """Test that text from old streams doesn't accumulate across multiple reconnects."""
    timeline = SpeechTimeline()
    supervisor = ASRStreamSupervisor(
        timeline=timeline,
        stream_epoch=1,
        task_epoch=1,
        sample_rate=16000,
        reconnect_audio_ms=500,
        max_result_history=10,
    )

    # Simulate 3 stream epochs, each accepting 3 results
    for stream in range(1, 4):
        if stream > 1:
            supervisor.reconnect(stream_epoch=stream)

        for i in range(3):
            result = ASRResult(
                stream_epoch=stream,
                task_epoch=stream,
                sentence_id=f"sent{i}",
                revision=1,
                is_final=False,
                capture_start_sample=i * 16000,
                capture_end_sample=(i + 1) * 16000,
                text=f"Stream{stream} text {i}",
                rescue_synthesized=False,
            )
            supervisor.accept_result(result, session_id="test")

        # After each stream, should have at most 3 entries (current stream only)
        # not 3*stream_number entries (accumulated)
        assert (
            len(supervisor._accepted_text) <= 3
        ), f"Stream {stream}: _accepted_text should not accumulate across reconnects"


def test_reconnect_clears_all_state():
    """Verify reconnect clears all state consistently, including _accepted_text."""
    timeline = SpeechTimeline()
    supervisor = ASRStreamSupervisor(
        timeline=timeline,
        stream_epoch=1,
        task_epoch=1,
        sample_rate=16000,
        reconnect_audio_ms=500,
        max_result_history=10,
    )

    # Populate all state
    result = ASRResult(
        stream_epoch=1,
        task_epoch=1,
        sentence_id="sent1",
        revision=1,
        is_final=True,
        capture_start_sample=0,
        capture_end_sample=16000,
        text="Test",
        rescue_synthesized=False,
    )
    supervisor.accept_result(result, session_id="test")

    # Verify state is populated
    assert len(supervisor._revisions) > 0
    assert len(supervisor._accepted_text) > 0
    assert len(supervisor._final_intervals) > 0

    # Reconnect
    supervisor.reconnect(stream_epoch=2)

    # All state should be cleared consistently
    assert len(supervisor._revisions) == 0, "_revisions should be cleared"
    assert len(supervisor._final_intervals) == 0, "_final_intervals should be cleared"
    assert (
        len(supervisor._final_interval_order) == 0
    ), "_final_interval_order should be cleared"
    assert (
        len(supervisor._latest_task_by_segment) == 0
    ), "_latest_task_by_segment should be cleared"
    assert len(supervisor._revision_order) == 0, "_revision_order should be cleared"
    assert len(supervisor._segment_order) == 0, "_segment_order should be cleared"
    assert (
        len(supervisor._accepted_text) == 0
    ), "_accepted_text should be cleared (BUG FIX)"
