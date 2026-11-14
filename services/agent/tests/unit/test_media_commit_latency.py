"""The final-to-commit gate: a bounded wait for the device VAD end, and one verdict per turn.

Soak 20261029-200043: the slow turns spent 1.0-2.0 s between the ASR final and the commit waiting for the
device VAD end, and the two semantic classifiers answered twice for the same turn because the final's text
and the commit's projection differed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.orchestration.state_machine import InteractionPhase
from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    PlaybackEventType,
    PlaybackProgress,
)
from services.agent.src.voice_core.media_session_commit import _resolve_media_turn_text
from services.agent.src.voice_core.speech_timeline import ASRResult
from services.agent.tests.unit.media_session_support import (
    _accept_media_asr_decision,
    _accept_media_asr_final,
    _device_vad,
    _finish_previous_reply,
    _wait_until,
)

_START = 40_000
_HEAD = "明天我们去"
_TAIL = "动物园好不好"
_VOICED_PCM = b"\x01\x10" * 320
_QUIET_ROOM_PCM = b"\x08\x00" * 320
_SILENT_PCM = b"\x00\x00" * 320


class _Verdict:
    """A semantic classifier of a test: it records the sentence it is asked and answers False at once."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.calls.append(text)
        return False


async def _no_asr(_identity: Any, _frame: AudioFrame) -> tuple[Any, ...]:
    return ()


async def _open(
    device_media_session: Any,
    session_id: str,
    *,
    grace_s: float = 0.5,
) -> SimpleNamespace:
    window = await device_media_session(session_id, during_playback=False)
    window.context.ingress.denoiser.process(_SILENT_PCM)
    window.context.ingress.denoiser.reset()
    window.provider.ingest_audio = _no_asr
    window.next_seq = 1
    window.close_verdict = _Verdict()
    window.live_verdict = _Verdict()
    runtime = window.context.runtime
    runtime.set_conversation_close_semantic_resolver(window.close_verdict)
    runtime.set_live_lookup_semantic_resolver(window.live_verdict)
    registry = window.registry
    registry.turn_endpoint_grace_s = grace_s
    registry.turn_endpoint_min_grace_s = 0.0
    registry.turn_endpoint_max_grace_s = grace_s
    return window


async def _send_uplink_frames(
    window: SimpleNamespace,
    *,
    start_sample: int,
    count: int,
    pcm: bytes,
    pace_s: float = 0.0,
    discontinuity_first: bool = False,
) -> int:
    sample = start_sample
    frame_samples = len(pcm) // 2
    for index in range(count):
        await window.registry.on_audio_frame(
            window.session,
            AudioFrame(
                identity=window.identity,
                sequence=window.next_seq,
                capture_start_sample=sample,
                frame_samples=frame_samples,
                payload=pcm,
                discontinuity=discontinuity_first and index == 0,
            ),
        )
        window.next_seq += 1
        sample += frame_samples
        if pace_s > 0:
            await asyncio.sleep(pace_s)
    return sample


async def _no_finals(_identity: Any) -> tuple[Any, ...]:
    return ()


def _turn_text(window: SimpleNamespace, start: int, end: int) -> str:
    text = _resolve_media_turn_text(
        window.context,
        stream_epoch=window.identity.stream_epoch,
        start_sample=start,
        end_sample=end,
    )
    assert text
    return text


def _extract_missing_vad_end_log(caplog: pytest.LogCaptureFixture) -> tuple[float, float]:
    matches = [
        record.getMessage()
        for record in caplog.records
        if "media final committed without device VAD end" in record.getMessage()
    ]
    assert len(matches) == 1
    waited = re.search(r"waited_s=([0-9.]+)", matches[0])
    threshold = re.search(r"threshold_s=([0-9.]+)", matches[0])
    assert waited is not None and threshold is not None, matches[0]
    return float(waited.group(1)), float(threshold.group(1))


@pytest.mark.asyncio
async def test_a_final_commits_without_a_device_vad_end_after_the_cap(
    device_media_session: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing device VAD end recovers once post-final silence passes the 900 ms hangover + 300 ms wait."""

    caplog.set_level(logging.INFO)
    window = await _open(device_media_session, "commit-final-wait-no-vad-end")
    await _device_vad(window, "vad-start", _START, final=False)

    started = time.monotonic()
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=_START + 16_000,
        text=_HEAD,
    )
    assert window.provider.prepared == []
    await _send_uplink_frames(
        window,
        start_sample=_START + 16_000,
        count=60,
        pcm=_SILENT_PCM,
    )

    await _wait_until(lambda: bool(window.provider.prepared), timeout=2.0)
    elapsed = time.monotonic() - started
    waited_s, threshold_s = _extract_missing_vad_end_log(caplog)

    assert window.provider.prepared == [_HEAD]
    assert 0.2 <= elapsed < 0.8
    assert threshold_s == 1.2
    assert 0.25 <= waited_s < 0.65


@pytest.mark.asyncio
async def test_a_stalled_uplink_keeps_the_turn_open(device_media_session: Any) -> None:
    """No uplink past the final means its VAD end is not overdue: the turn keeps waiting."""

    window = await _open(device_media_session, "commit-final-wait-stalled")
    await _device_vad(window, "vad-start", _START, final=False)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=_START + 16_000,
        text=_HEAD,
    )

    await asyncio.sleep(0.8)
    assert window.provider.prepared == []


@pytest.mark.asyncio
async def test_a_vad_end_inside_the_cap_keeps_the_normal_commit(
    device_media_session: Any,
) -> None:
    """An end that arrives inside the wait wins: the commit reads up to it, not up to the ASR range."""

    window = await _open(device_media_session, "commit-final-wait-vad-end", grace_s=0.01)
    await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="p-1",
        start_sample=_START + 8_000,
        end_sample=_START + 20_000,
        text=_TAIL,
        is_final=False,
    )

    started = time.monotonic()
    await _device_vad(window, "vad-start", _START, final=False)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=_START + 8_000,
        text=_HEAD,
    )
    window.provider.finalize_speech_segment = _no_finals
    window.context.asr.last_sent_sample = _START + 20_000
    committed = _turn_text(window, _START, _START + 20_000)
    assert committed == f"{_HEAD} {_TAIL}"
    await _device_vad(window, "vad-end", _START + 20_000, final=True)

    await _wait_until(lambda: bool(window.provider.prepared), timeout=1.0)

    assert time.monotonic() - started < 0.25  # the normal grace, not the 300 ms wait
    assert window.provider.prepared == [committed]


@pytest.mark.asyncio
async def test_each_classifier_answers_once_for_the_turn(device_media_session: Any) -> None:
    """The sentence asked at the final answers for the turn; the commit's longer projection joins it."""

    window = await _open(device_media_session, "commit-one-verdict", grace_s=0.01)
    await _device_vad(window, "vad-start", _START, final=False)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=_START + 8_000,
        text=_HEAD,
    )
    await asyncio.sleep(0.02)  # let the close verdict start at the final
    await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="p-1",
        start_sample=_START + 8_000,
        end_sample=_START + 20_000,
        text=_TAIL,
        is_final=False,
    )
    window.provider.finalize_speech_segment = _no_finals
    window.context.asr.last_sent_sample = _START + 20_000
    committed = _turn_text(window, _START, _START + 20_000)
    assert committed == f"{_HEAD} {_TAIL}"
    await _device_vad(window, "vad-end", _START + 20_000, final=True)

    await _wait_until(lambda: bool(window.provider.prepared), timeout=1.0)

    assert window.provider.prepared == [committed]
    assert window.live_verdict.calls == [_HEAD]
    assert window.close_verdict.calls == [_HEAD]


@pytest.mark.asyncio
async def test_the_endpoint_grace_absorbs_the_provider_finalize(
    device_media_session: Any,
) -> None:
    """Soak 20261031-012237: the Doubao task finalize ran before the grace, not inside it.

    The grace is the child's silence after the device VAD end. Time the bridge spends waiting for the
    provider's last packet is its own cost, so it comes out of that silence instead of being added.
    """

    window = await _open(device_media_session, "commit-grace-under-finalize", grace_s=0.5)
    window.registry.turn_endpoint_grace_complete_s = 0.6  # keep the neutral 0.5 s grace
    window.registry.turn_endpoint_grace_incomplete_s = 0.6
    await _device_vad(window, "vad-start", _START, final=False)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=_START + 16_000,
        text=_HEAD,
    )

    async def slow_finalize(_identity: Any) -> tuple[Any, ...]:
        await asyncio.sleep(0.3)
        return ()

    window.provider.finalize_speech_segment = slow_finalize
    window.context.asr.last_sent_sample = _START + 20_000
    started = time.monotonic()
    await _device_vad(window, "vad-end", _START + 16_000, final=True)
    await _wait_until(lambda: bool(window.provider.prepared), timeout=2.0)
    elapsed = time.monotonic() - started

    assert window.provider.prepared == [_HEAD]
    assert 0.45 <= elapsed < 0.7  # 0.5 s grace with the 0.3 s finalize inside it, not after it


@pytest.mark.parametrize("resume_after_s", [0.1, 0.4])
@pytest.mark.asyncio
async def test_a_resumed_utterance_still_cancels_the_pending_commit(
    device_media_session: Any,
    resume_after_s: float,
) -> None:
    """A child who speaks again inside the grace never gets the first half committed alone.

    0.1 s resumes while the provider task is still being finalized, 0.4 s after it and still inside
    the 0.5 s grace, which now runs from the VAD end rather than from the end of the finalize.
    """

    window = await _open(device_media_session, f"commit-grace-resume-{resume_after_s}", grace_s=0.5)
    registry, context = window.registry, window.context
    registry.turn_endpoint_grace_complete_s = 0.6
    registry.turn_endpoint_grace_incomplete_s = 0.6
    await _device_vad(window, "vad-start", _START, final=False)
    await _accept_media_asr_final(
        registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=_START + 16_000,
        text=_HEAD,
    )

    async def slow_finalize(_identity: Any) -> tuple[Any, ...]:
        await asyncio.sleep(0.3)
        return ()

    window.provider.finalize_speech_segment = slow_finalize
    context.asr.last_sent_sample = _START + 16_000

    pending_end = asyncio.create_task(_device_vad(window, "vad-end", _START + 16_000, final=True))
    await asyncio.sleep(resume_after_s)
    assert window.provider.prepared == []
    await _device_vad(window, "vad-start-2", _START + 20_000, final=False)
    await pending_end
    await asyncio.sleep(0.6)

    assert window.provider.prepared == []
    assert context.pending.turn_endpoint_sample is None


@pytest.mark.asyncio
async def test_continuous_voiced_uplink_without_new_partial_keeps_open_vad_turn_open(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "commit-continuous-voiced-open-vad", grace_s=0.01)
    window.provider.finalize_speech_segment = _no_finals
    await _device_vad(window, "vad-start", _START, final=False)
    sample = await _send_uplink_frames(window, start_sample=_START, count=25, pcm=_VOICED_PCM)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=sample,
        text=_HEAD,
    )

    tail_start = sample
    sample = await _send_uplink_frames(
        window,
        start_sample=sample,
        count=40,
        pcm=_VOICED_PCM,
        pace_s=0.02,
    )
    assert window.provider.prepared == []
    assert window.context.pending.turn_endpoint_sample is None

    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-2",
        start_sample=tail_start,
        end_sample=sample,
        text=_TAIL,
    )
    await _device_vad(window, "vad-end", sample, final=True)
    await _wait_until(lambda: bool(window.provider.prepared), timeout=1.0)
    assert window.provider.prepared == [f"{_HEAD} {_TAIL}"]


@pytest.mark.asyncio
async def test_no_vad_start_holds_for_live_partial_and_commits_under_quiet_room_noise_floor(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "commit-no-vad-start-partial-noise-floor")
    sample = await _send_uplink_frames(window, start_sample=_START, count=25, pcm=_VOICED_PCM)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=sample,
        text=_HEAD,
    )

    tail_start = sample
    await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="f-2",
        start_sample=tail_start,
        end_sample=tail_start + 3_200,
        text="动物园",
        is_final=False,
    )
    sample = await _send_uplink_frames(
        window,
        start_sample=sample,
        count=25,
        pcm=_VOICED_PCM,
        pace_s=0.02,
    )
    assert window.provider.prepared == []

    await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="f-2",
        start_sample=tail_start,
        end_sample=sample,
        text=_TAIL,
        is_final=True,
        revision=2,
    )
    await _send_uplink_frames(window, start_sample=sample, count=16, pcm=_QUIET_ROOM_PCM)
    await _wait_until(lambda: bool(window.provider.prepared), timeout=1.5)
    assert window.provider.prepared == [f"{_HEAD} {_TAIL}"]


@pytest.mark.asyncio
async def test_short_comma_silence_inside_open_vad_does_not_preempt_firmware_hangover(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "commit-comma-silence-open-vad", grace_s=0.01)
    window.provider.finalize_speech_segment = _no_finals
    await _device_vad(window, "vad-start", _START, final=False)
    sample = await _send_uplink_frames(window, start_sample=_START, count=25, pcm=_VOICED_PCM)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=sample,
        text=_HEAD,
    )

    sample = await _send_uplink_frames(
        window,
        start_sample=sample,
        count=22,
        pcm=_SILENT_PCM,
        pace_s=0.02,
    )
    assert window.provider.prepared == []

    tail_start = sample
    sample = await _send_uplink_frames(
        window,
        start_sample=sample,
        count=20,
        pcm=_VOICED_PCM,
        pace_s=0.02,
    )
    assert window.provider.prepared == []

    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-2",
        start_sample=tail_start,
        end_sample=sample,
        text=_TAIL,
    )
    await _device_vad(window, "vad-end", sample, final=True)
    await _wait_until(lambda: bool(window.provider.prepared), timeout=1.0)
    assert window.provider.prepared == [f"{_HEAD} {_TAIL}"]


@pytest.mark.asyncio
async def test_no_vad_start_weak_onset_commits_boundedly_on_post_final_silence(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "commit-weak-onset-no-vad-start")
    sample = await _send_uplink_frames(window, start_sample=_START, count=20, pcm=_VOICED_PCM)
    started = time.monotonic()
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=sample,
        text=_HEAD,
    )
    await _send_uplink_frames(window, start_sample=sample, count=16, pcm=_SILENT_PCM)

    await _wait_until(lambda: bool(window.provider.prepared), timeout=1.5)
    assert window.provider.prepared == [_HEAD]
    assert 0.2 <= time.monotonic() - started < 0.8


@pytest.mark.asyncio
async def test_missing_vad_end_silent_recovery_is_idempotent_against_late_vad_end(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "commit-missing-vad-end-late-idempotent")
    finalize_calls = 0

    async def count_finalize(_identity: Any) -> tuple[Any, ...]:
        nonlocal finalize_calls
        finalize_calls += 1
        return ()

    window.provider.finalize_speech_segment = count_finalize
    await _device_vad(window, "vad-start", _START, final=False)
    sample = await _send_uplink_frames(window, start_sample=_START, count=25, pcm=_VOICED_PCM)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=sample,
        text=_HEAD,
    )

    sample = await _send_uplink_frames(
        window,
        start_sample=sample,
        count=20,
        pcm=_SILENT_PCM,
        pace_s=0.02,
    )
    assert window.provider.prepared == []

    sample = await _send_uplink_frames(
        window,
        start_sample=sample,
        count=45,
        pcm=_SILENT_PCM,
        pace_s=0.02,
    )
    await _wait_until(lambda: bool(window.provider.prepared), timeout=1.5)
    assert window.provider.prepared == [_HEAD]
    assert finalize_calls == 0

    await _device_vad(window, "late-vad-end", sample, final=True)
    await asyncio.sleep(0.1)
    assert window.provider.prepared == [_HEAD]
    assert finalize_calls == 0
    assert window.context.pending.turn_start_sample is None
    assert window.context.pending.turn_endpoint_sample is None


@pytest.mark.asyncio
async def test_silence_watermark_resets_across_cancel_reconnect_playback_and_capture_hold(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "commit-silence-watermark-resets")
    registry, context = window.registry, window.context
    sample = _START

    await _device_vad(window, "vad-start-cancel", sample, final=False)
    turn_end = await _send_uplink_frames(window, start_sample=sample, count=10, pcm=_VOICED_PCM)
    await _accept_media_asr_final(
        registry,
        window.identity,
        sentence_id="f-before-cancel",
        start_sample=sample,
        end_sample=turn_end,
        text=_HEAD,
    )
    sample = await _send_uplink_frames(window, start_sample=turn_end, count=50, pcm=_SILENT_PCM)
    await registry._audio_ingress.cancel(context)
    sample = await _send_uplink_frames(window, start_sample=sample, count=15, pcm=_SILENT_PCM)
    await asyncio.sleep(0.35)
    assert window.provider.prepared == []
    registry._clear_pending_turn_state(context)

    sample = await _send_uplink_frames(window, start_sample=sample, count=65, pcm=_SILENT_PCM)
    registry._record_playback_boundary(context)
    assert getattr(context.ingress, "silent_run_end_sample", None) is None
    context.last_playback_end_sample = None

    sample = await _send_uplink_frames(window, start_sample=sample, count=65, pcm=_SILENT_PCM)
    registry.resume_listening_capture(context, wake_sample=sample, cause="screen_tap")
    assert getattr(context.ingress, "silent_run_end_sample", None) is None

    sample += 320
    sample = await _send_uplink_frames(window, start_sample=sample, count=65, pcm=_SILENT_PCM)
    registry._audio_ingress.reset_for_reconnect(context)
    assert getattr(context.ingress, "silent_run_end_sample", None) is None

    sample = await _send_uplink_frames(window, start_sample=sample, count=50, pcm=_SILENT_PCM)
    discontinuity_sample = sample
    sample = await _send_uplink_frames(
        window,
        start_sample=sample,
        count=2,
        pcm=_SILENT_PCM,
        discontinuity_first=True,
    )
    await registry._audio_ingress._wait_until_idle(context)
    assert getattr(context.ingress, "silent_run_start_sample", None) == discontinuity_sample

    turn_start = sample
    await _device_vad(window, "vad-start-after-resets", turn_start, final=False)
    sample = await _send_uplink_frames(window, start_sample=turn_start, count=20, pcm=_VOICED_PCM)
    await _accept_media_asr_final(
        registry,
        window.identity,
        sentence_id="f-after-reset",
        start_sample=turn_start,
        end_sample=sample,
        text=_HEAD,
    )
    await _send_uplink_frames(
        window,
        start_sample=sample,
        count=35,
        pcm=_VOICED_PCM,
        pace_s=0.02,
    )
    assert window.provider.prepared == []


@pytest.mark.asyncio
async def test_paced_open_vad_missing_end_waits_full_hangover_and_logs_measured_elapsed(
    device_media_session: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    window = await _open(device_media_session, "commit-paced-open-vad-missing-end", grace_s=0.05)
    await _device_vad(window, "vad-start", _START, final=False)
    sample = await _send_uplink_frames(window, start_sample=_START, count=25, pcm=_VOICED_PCM)

    started = time.monotonic()
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=sample,
        text=_HEAD,
    )
    await _send_uplink_frames(
        window,
        start_sample=sample,
        count=60,
        pcm=_SILENT_PCM,
        pace_s=0.02,
    )
    await _wait_until(lambda: bool(window.provider.prepared), timeout=1.0)
    elapsed = time.monotonic() - started
    waited_s, threshold_s = _extract_missing_vad_end_log(caplog)

    assert window.provider.prepared == [_HEAD]
    assert 1.15 <= elapsed < 1.65
    assert threshold_s == 1.2
    assert 1.15 <= waited_s < 1.65


@pytest.mark.asyncio
async def test_pump_backlog_delays_post_final_commit_until_queued_clause_is_ingested(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "commit-pump-backlog-second-clause", grace_s=0.05)
    sample = await _send_uplink_frames(window, start_sample=_START, count=25, pcm=_VOICED_PCM)
    await window.registry._audio_ingress._wait_until_idle(window.context)

    clause2_start = sample
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=clause2_start,
        text=_HEAD,
    )

    async def delayed_ingest(identity: Any, frame: AudioFrame) -> tuple[Any, ...]:
        if frame.capture_start_sample == clause2_start:
            await asyncio.sleep(0.45)
        if frame.capture_start_sample == clause2_start + 24 * 320:
            return (
                ASRResult(
                    task_epoch=1,
                    sentence_id="f-2",
                    revision=1,
                    capture_start_sample=clause2_start,
                    capture_end_sample=clause2_start + 25 * 320,
                    text=_TAIL,
                    is_final=True,
                    confidence=0.95,
                    stream_epoch=identity.stream_epoch,
                ),
            )
        return ()

    window.provider.ingest_audio = delayed_ingest
    sample = await _send_uplink_frames(window, start_sample=sample, count=25, pcm=_VOICED_PCM)
    await _send_uplink_frames(window, start_sample=sample, count=16, pcm=_SILENT_PCM)

    await asyncio.sleep(0.6)
    assert window.provider.prepared == [f"{_HEAD} {_TAIL}"]


@pytest.mark.asyncio
async def test_open_vad_voiced_second_clause_with_delayed_final_is_not_skipped_by_later_silence(
    device_media_session: Any,
) -> None:
    window = await _open(
        device_media_session, "commit-open-vad-voiced-gap-delayed-final", grace_s=0.05
    )
    await _device_vad(window, "vad-start", _START, final=False)
    sample = await _send_uplink_frames(window, start_sample=_START, count=25, pcm=_VOICED_PCM)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=sample,
        text=_HEAD,
    )

    clause2_start = sample
    sample = await _send_uplink_frames(window, start_sample=sample, count=25, pcm=_VOICED_PCM)
    clause2_end = sample
    await _send_uplink_frames(window, start_sample=sample, count=65, pcm=_SILENT_PCM)
    await asyncio.sleep(0.45)
    assert window.provider.prepared == []

    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-2",
        start_sample=clause2_start,
        end_sample=clause2_end,
        text=_TAIL,
    )
    await _wait_until(lambda: bool(window.provider.prepared), timeout=1.0)
    assert window.provider.prepared == [f"{_HEAD} {_TAIL}"]


@pytest.mark.asyncio
async def test_no_vad_start_weak_onset_commits_under_quiet_room_noise_floor(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "commit-weak-onset-quiet-room-floor", grace_s=0.05)
    sample = await _send_uplink_frames(window, start_sample=_START, count=25, pcm=_VOICED_PCM)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=sample,
        text=_HEAD,
    )
    await _send_uplink_frames(window, start_sample=sample, count=75, pcm=_QUIET_ROOM_PCM)

    await asyncio.sleep(0.5)
    assert window.provider.prepared == [_HEAD]


@pytest.mark.asyncio
async def test_open_vad_quiet_room_noise_floor_commits_when_late_asr_arrives_after_vad_end(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "commit-open-vad-noise-floor-late-asr", grace_s=0.05)
    window.provider.finalize_speech_segment = _no_finals
    await _device_vad(window, "vad-start", _START, final=False)
    sample = await _send_uplink_frames(window, start_sample=_START, count=25, pcm=_VOICED_PCM)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=sample,
        text=_HEAD,
    )
    sample = await _send_uplink_frames(
        window,
        start_sample=sample,
        count=20,
        pcm=_QUIET_ROOM_PCM,
        pace_s=0.02,
    )
    assert window.provider.prepared == []

    clause2_start = sample
    sample = await _send_uplink_frames(window, start_sample=sample, count=60, pcm=_VOICED_PCM)
    clause2_end = sample
    sample = await _send_uplink_frames(window, start_sample=sample, count=10, pcm=_QUIET_ROOM_PCM)
    window.context.ingress.last_finalized_audio_watermark = sample
    await _device_vad(window, "vad-end", clause2_end, final=True)
    await asyncio.sleep(0.1)
    assert window.provider.prepared == []

    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-2",
        start_sample=clause2_start,
        end_sample=clause2_end,
        text=_TAIL,
    )
    await _wait_until(lambda: bool(window.provider.prepared), timeout=1.0)
    assert window.provider.prepared == [f"{_HEAD} {_TAIL}"]


@pytest.mark.asyncio
async def test_late_weak_onset_final_commits_after_listening_silence_drop_and_capture_hold(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "commit-late-final-after-capture-hold", grace_s=0.05)
    window.registry.listening_capture_window_s = 0.15
    window.context.runtime.set_interaction_phase(
        InteractionPhase.LISTENING,
        cause="test_listening",
        publish=False,
    )
    window.registry._restart_listening_capture(window.context)

    sample = await _send_uplink_frames(window, start_sample=_START, count=20, pcm=_VOICED_PCM)
    speech_end = sample
    await window.registry._audio_ingress._wait_until_idle(window.context)
    sample = await _send_uplink_frames(window, start_sample=sample, count=20, pcm=_SILENT_PCM)
    assert window.context.ingress.listening_silence_dropped_frames == 20

    await asyncio.sleep(0.2)
    await _send_uplink_frames(window, start_sample=sample, count=5, pcm=_SILENT_PCM)
    assert window.context.listening_capture_hold is True

    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=speech_end,
        text=_HEAD,
    )
    await asyncio.sleep(0.45)
    assert window.provider.prepared == [_HEAD]


@pytest.mark.asyncio
async def test_missing_vad_end_recovery_does_not_swallow_next_weak_onset_turn_vad_end(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "commit-next-weak-onset-vad-end", grace_s=0.05)
    finalize_calls = 0

    async def finalize_turn2(identity: Any) -> tuple[Any, ...]:
        nonlocal finalize_calls
        finalize_calls += 1
        return (
            ASRResult(
                task_epoch=1,
                sentence_id="f-2",
                revision=1,
                capture_start_sample=90_000,
                capture_end_sample=106_000,
                text=_TAIL,
                is_final=True,
                confidence=0.95,
                stream_epoch=identity.stream_epoch,
            ),
        )

    window.provider.finalize_speech_segment = finalize_turn2
    await _device_vad(window, "vad-start-1", _START, final=False)
    sample = await _send_uplink_frames(window, start_sample=_START, count=20, pcm=_VOICED_PCM)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=sample,
        text=_HEAD,
    )
    await _send_uplink_frames(window, start_sample=sample, count=65, pcm=_SILENT_PCM)
    await _wait_until(lambda: window.provider.prepared == [_HEAD], timeout=1.0)

    sample = await _send_uplink_frames(window, start_sample=90_000, count=50, pcm=_VOICED_PCM)
    await window.registry._audio_ingress._wait_until_idle(window.context)
    await _device_vad(window, "vad-end-2", sample, final=True)
    await _wait_until(lambda: len(window.provider.prepared) == 2, timeout=1.0)

    assert finalize_calls == 1
    assert window.provider.prepared == [_HEAD, _TAIL]


@pytest.mark.asyncio
async def test_playback_boundary_keeps_ignoring_prior_turn_late_vad_end(
    device_media_session: Any,
) -> None:
    window = await _open(
        device_media_session, "commit-playback-boundary-late-vad-end", grace_s=0.05
    )
    finalize_calls = 0

    async def count_finalize(_identity: Any) -> tuple[Any, ...]:
        nonlocal finalize_calls
        finalize_calls += 1
        return ()

    window.provider.finalize_speech_segment = count_finalize
    await _device_vad(window, "vad-start-1", _START, final=False)
    sample = await _send_uplink_frames(window, start_sample=_START, count=20, pcm=_VOICED_PCM)
    speech_end = sample
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=speech_end,
        text=_HEAD,
    )
    await _send_uplink_frames(window, start_sample=sample, count=65, pcm=_SILENT_PCM)
    await _wait_until(lambda: window.provider.prepared == [_HEAD], timeout=1.0)

    window.registry._record_playback_boundary(window.context)
    await _device_vad(window, "vad-end-1-late", speech_end, final=True)

    assert finalize_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("turn2_mode", ["normal", "end_before_final", "weak_onset"])
async def test_post_commit_synthesis_gap_final_stays_separate_from_next_turn(
    device_media_session: Any, turn2_mode: str,
) -> None:
    window = await _open(device_media_session, "commit-unrotated-pre-playback-tail", grace_s=0.05)
    task_epoch = 1

    async def rotating_finalize(_identity: Any) -> tuple[Any, ...]:
        nonlocal task_epoch
        task_epoch += 1
        return ()

    window.provider.finalize_speech_segment = rotating_finalize
    await _device_vad(window, "vad-start-1", _START, final=False)
    sample = await _send_uplink_frames(window, start_sample=_START, count=25, pcm=_VOICED_PCM)
    speech_end = sample
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=speech_end,
        text=_HEAD,
    )
    sample = await _send_uplink_frames(window, start_sample=sample, count=65, pcm=_SILENT_PCM)
    await _wait_until(lambda: window.provider.prepared == [_HEAD], timeout=1.0)

    await _device_vad(window, "vad-end-1-late", speech_end, final=True)
    assert task_epoch == 1

    synth_gap_start = sample
    sample = await _send_uplink_frames(window, start_sample=sample, count=10, pcm=_QUIET_ROOM_PCM)
    await window.registry._audio_ingress._wait_until_idle(window.context)

    fence = window.context.runtime.fence
    window.context.output.playback_started = (fence, sample)
    window.context.output.playback.start(fence)
    assert window.context.output.playback.register_audio(fence, 0, 0, 1_600)
    window.context.output.provider_complete = True
    window.context.runtime.set_interaction_phase(
        InteractionPhase.SPEAKING,
        cause="test_reply_speaking",
        publish=False,
    )

    retired_tail = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="f-1-retired-tail",
        start_sample=speech_end,
        end_sample=speech_end + 8_000,
        text=_TAIL,
        is_final=True,
        revision=1,
    )
    assert retired_tail.reason is ASRDecisionReason.FULLY_COMMITTED

    await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="f-1-synth-tail",
        start_sample=synth_gap_start,
        end_sample=sample - 320,
        text=_TAIL,
        is_final=True,
        revision=1,
    )

    await window.registry.on_playback_progress(
        window.session,
        PlaybackProgress(
            identity=window.identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=1_600,
            client_monotonic_ms=100,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            session_epoch=fence.session_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )
    _finish_previous_reply(window.context)
    window.registry.endpoint_overlapped_pending_after_playback(window.context)
    await asyncio.sleep(0.25)

    await _wait_until(lambda: window.provider.prepared == [_HEAD, _TAIL], timeout=1.5)
    _finish_previous_reply(window.context)
    assert window.context.pending.pending_turn_playback_overlap is False
    assert window.context.pending.turn_endpoint_sample is None
    start2 = sample + 16_000
    if turn2_mode != "weak_onset":
        await _device_vad(window, "vad-start-2", start2, final=False)
    end2 = await _send_uplink_frames(window, start_sample=start2, count=25, pcm=_VOICED_PCM)
    if turn2_mode == "end_before_final":
        await _device_vad(window, "vad-end-2", end2, final=True)
    await _accept_media_asr_final(
        window.registry, window.identity, sentence_id="f-2",
        start_sample=start2, end_sample=end2, text="今天天气怎么样",
    )
    if turn2_mode == "normal":
        await _device_vad(window, "vad-end-2", end2, final=True)
    elif turn2_mode == "weak_onset":
        await _send_uplink_frames(window, start_sample=end2, count=20, pcm=_SILENT_PCM)
    await _wait_until(lambda: len(window.provider.prepared) == 3, timeout=1.5)
    assert window.provider.prepared == [_HEAD, _TAIL, "今天天气怎么样"]
    await _wait_until(
        lambda: any(
            result.fence.matches(window.context.runtime.fence)
            for result in window.context.output.output_results
        ),
        timeout=1.5,
    )


@pytest.mark.asyncio
async def test_stale_silent_pump_frames_preserve_newer_listening_silence(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "stale-silent-pump-progress")
    state = window.context.ingress
    for start in range(40_640, 42_240, 320):
        state.observe_frame_silence(
            stream_epoch=window.context.stream_epoch,
            start_sample=start,
            frame_samples=320,
            silent=True,
        )
    for start in (40_000, 40_320):
        state.observe_frame_silence(
            stream_epoch=window.context.stream_epoch,
            start_sample=start,
            frame_samples=320,
            silent=True,
        )
    assert state.silent_run_end_sample == 42_240
    state.observe_frame_silence(
        stream_epoch=window.context.stream_epoch,
        start_sample=42_240,
        frame_samples=320,
        silent=True,
    )
    assert state.silent_run_end_sample == 42_560


@pytest.mark.asyncio
@pytest.mark.parametrize("open_vad", [True, False])
async def test_unaligned_final_silence_retires_late_vad(device_media_session: Any, open_vad: bool) -> None:
    window = await _open(device_media_session, f"unaligned-{open_vad}", grace_s=0.01)
    if open_vad:
        await _device_vad(window, "start", 321000, final=False)
    await _send_uplink_frames(window, start_sample=321000, count=1, pcm=_VOICED_PCM)
    await window.registry._audio_ingress._wait_until_idle(window.context)
    await _accept_media_asr_final(window.registry, window.identity, sentence_id="unaligned",
        start_sample=321000, end_sample=321152, text=_HEAD)
    end = await _send_uplink_frames(window, start_sample=321320, count=70, pcm=_SILENT_PCM)
    await _wait_until(lambda: bool(window.provider.prepared), timeout=1.0)
    assert window.provider.prepared == [_HEAD]
    assert window.context.asr.last_committed_sample >= 321320 + 4800
    await _device_vad(window, "late-end", end, final=True)
    assert window.context.pending.turn_endpoint_sample is None


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [0.019, 0.021])
@pytest.mark.parametrize("pending_partial", [False, True])
async def test_live_silent_pump_commits_processed_window(
    device_media_session: Any, delay: float, pending_partial: bool
) -> None:
    window = await _open(device_media_session, f"live-pump-{delay}", grace_s=0.01)
    await _device_vad(window, "start", _START, final=False)
    end = await _send_uplink_frames(window, start_sample=_START, count=1, pcm=_VOICED_PCM)
    await window.registry._audio_ingress._wait_until_idle(window.context)
    await _accept_media_asr_final(window.registry, window.identity, sentence_id="head",
        start_sample=_START, end_sample=end, text=_HEAD)
    async def slow_ingest(_identity: Any, _frame: AudioFrame) -> tuple[Any, ...]:
        await asyncio.sleep(delay)
        return ()
    window.provider.ingest_audio = slow_ingest
    if pending_partial:
        await _accept_media_asr_decision(
            window.registry, window.identity, sentence_id="slow-tail",
            start_sample=end, end_sample=end + 320, text=_TAIL, is_final=False,
        )
    await _send_uplink_frames(window, start_sample=end, count=110, pcm=_SILENT_PCM, pace_s=0.02)
    if pending_partial:
        assert window.provider.prepared == []
        await _accept_media_asr_decision(
            window.registry, window.identity, sentence_id="slow-tail", revision=2,
            start_sample=end, end_sample=end + 320, text=_TAIL, is_final=True,
        )
        await _wait_until(lambda: bool(window.provider.prepared), timeout=1.0)
        assert window.provider.prepared == [f"{_HEAD} {_TAIL}"]
    else:
        assert window.provider.prepared == [_HEAD]


@pytest.mark.asyncio
async def test_unaligned_final_cannot_skip_a_later_whole_voiced_frame(device_media_session: Any) -> None:
    window = await _open(device_media_session, "unaligned-whole-clause", grace_s=0.01)
    await _device_vad(window, "start", 321000, final=False)
    await _send_uplink_frames(window, start_sample=321000, count=2, pcm=_VOICED_PCM)
    await _accept_media_asr_final(window.registry, window.identity, sentence_id="head",
        start_sample=321000, end_sample=321152, text=_HEAD)
    await _send_uplink_frames(window, start_sample=321640, count=70, pcm=_SILENT_PCM)
    await asyncio.sleep(0.5)
    assert window.provider.prepared == []


@pytest.mark.asyncio
async def test_silent_window_does_not_skip_admitted_unprocessed_voice(device_media_session: Any) -> None:
    window = await _open(device_media_session, "silent-window-queued-voice", grace_s=0.01)
    await _device_vad(window, "start", _START, final=False)
    end = await _send_uplink_frames(window, start_sample=_START, count=1, pcm=_VOICED_PCM)
    sample = await _send_uplink_frames(window, start_sample=end, count=65, pcm=_SILENT_PCM)
    await window.registry._audio_ingress._wait_until_idle(window.context)
    release = asyncio.Event()
    async def held_ingest(_identity: Any, _frame: AudioFrame) -> tuple[Any, ...]:
        await release.wait()
        return ()
    window.provider.ingest_audio = held_ingest
    await _send_uplink_frames(window, start_sample=sample, count=1, pcm=_VOICED_PCM)
    await _accept_media_asr_final(window.registry, window.identity, sentence_id="head",
        start_sample=_START, end_sample=end, text=_HEAD)
    try:
        await asyncio.sleep(0.4)
        assert window.provider.prepared == []
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["none", "partial", "vad"])
@pytest.mark.parametrize("text", ["今天天气怎么样", "等一下", "停"])
async def test_new_synthesis_gap_question_or_control_is_preserved(device_media_session, evidence, text):
    window = await _open(device_media_session, "commit-unrotated-pre-playback-tail", grace_s=0.05)
    task_epoch = 1

    async def rotating_finalize(_identity: Any) -> tuple[Any, ...]:
        nonlocal task_epoch
        task_epoch += 1
        return ()

    window.provider.finalize_speech_segment = rotating_finalize
    await _device_vad(window, "vad-start-1", _START, final=False)
    sample = await _send_uplink_frames(window, start_sample=_START, count=25, pcm=_VOICED_PCM)
    speech_end = sample
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="f-1",
        start_sample=_START,
        end_sample=speech_end,
        text=_HEAD,
    )
    sample = await _send_uplink_frames(window, start_sample=sample, count=65, pcm=_SILENT_PCM)
    await _wait_until(lambda: window.provider.prepared == [_HEAD], timeout=1.0)

    await _device_vad(window, "vad-end-1-late", speech_end, final=True)
    assert task_epoch == 1

    synth_gap_start = sample
    sample = await _send_uplink_frames(window, start_sample=sample, count=25, pcm=_VOICED_PCM)
    await window.registry._audio_ingress._wait_until_idle(window.context)

    fence = window.context.runtime.fence
    window.context.output.playback_started = (fence, sample)
    window.context.output.playback.start(fence)
    assert window.context.output.playback.register_audio(fence, 0, 0, 1_600)
    window.context.output.provider_complete = True
    window.context.runtime.set_interaction_phase(
        InteractionPhase.SPEAKING,
        cause="test_reply_speaking",
        publish=False,
    )

    if evidence == "partial":
        partial = await _accept_media_asr_decision(window.registry, window.identity,
            sentence_id="new-question", start_sample=synth_gap_start,
            end_sample=sample - 320, text="今天天气", is_final=False, revision=1)
        assert partial.accepted is not None
    elif evidence == "vad":
        await _device_vad(window, "new-vad", synth_gap_start, final=False)
    decision = await _accept_media_asr_decision(window.registry, window.identity,
        sentence_id="new-question", start_sample=synth_gap_start,
        end_sample=sample - 320, text=text, is_final=True, revision=2)
    assert decision.accepted is not None



@pytest.mark.asyncio
async def test_discontinuity_preserves_admission_during_provider_reset(device_media_session):
    import asyncio
    w = await _open(device_media_session, "review-reset-queued-voice", grace_s=0.01)
    w.context.ingress.queue = asyncio.Queue(maxsize=256)
    resetting, release_reset, held_voice, release_voice = (asyncio.Event() for _ in range(4))
    async def reset(*args, **kwargs):
        resetting.set()
        await release_reset.wait()
    unsafe_start = _START + 320 * 66
    async def ingest(identity, frame):
        if frame.capture_start_sample == unsafe_start:
            held_voice.set()
            await release_voice.wait()
        return ()
    w.provider.reset_after_discontinuity = reset
    w.provider.ingest_audio = ingest
    await _send_uplink_frames(w, start_sample=_START, count=1, pcm=_VOICED_PCM, discontinuity_first=True)
    await resetting.wait()
    sample = await _send_uplink_frames(w, start_sample=_START + 320, count=65, pcm=_SILENT_PCM)
    sample = await _send_uplink_frames(w, start_sample=sample, count=1, pcm=_VOICED_PCM)
    release_reset.set()
    await held_voice.wait()
    await _send_uplink_frames(w, start_sample=sample, count=1, pcm=_SILENT_PCM)
    await _device_vad(w, "new-start", _START, final=False)
    await _accept_media_asr_final(w.registry, w.identity, sentence_id="head",
        start_sample=_START, end_sample=_START + 320, text=_HEAD)
    try:
        await asyncio.sleep(0.4)
        assert w.provider.prepared == []
    finally:
        release_voice.set()
