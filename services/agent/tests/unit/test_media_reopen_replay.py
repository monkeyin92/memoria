"""Replay recorded t038 ranges with explicit controls for missing interim ASR.

The transcript and initial runtime authority are test controls. The old log
does not identify its pending partial or timer guards, so neither branch
reconstructs the actual field transcript.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
)

FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "docs/acceptance/run-20261010-verify/t038-replay.json"
)


async def _replay(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    continued: bool,
) -> dict[str, Any]:
    caplog.set_level(
        logging.INFO, logger="services.agent.src.voice_core.media_session_input"
    )
    data = json.loads(FIXTURE.read_text())
    window = await device_media_session("t038-replay", during_playback=False)
    registry, context = window.registry, window.context
    initial = data["initial_recorded_ranges"]
    context.last_playback_end_sample = initial["playback_end"]
    context.pending.turn_start_sample = initial["pending_start"]
    context.pending.turn_end_sample = initial["pending_end"]
    context.pending.pending_turn_playback_overlap = True
    clock: dict[str, Any] = {"at_ms": 0.0, "deadline_ms": None}
    attempts: list[tuple[float, int | None]] = []
    expiries: list[dict[str, Any]] = []
    armed: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    def schedule(ctx: Any) -> None:
        attempts.append((clock["at_ms"], ctx.pending.turn_endpoint_sample))

    monkeypatch.setattr(registry, "_schedule_turn_commit", schedule)
    original_arm = registry._arm_reopen_evidence_window

    def arm(ctx: Any, endpoint: int) -> None:
        previous = ctx.pending.reopen_evidence_handle
        original_arm(ctx, endpoint)
        handle = ctx.pending.reopen_evidence_handle
        checks.append({
            "at_ms": clock["at_ms"],
            "endpoint": endpoint,
            "text": registry._pending_turn_has_text_evidence(ctx),
            "end": ctx.pending.turn_end_sample,
            "watermark": ctx.ingress.last_finalized_audio_watermark,
            "in_flight": registry._reply_in_flight(ctx),
        })
        if handle is not None and handle is not previous:
            # Fire the real callback on the recorded event clock, without
            # waiting 30 s or allowing a wall-clock timer to race the replay.
            handle.cancel()
            clock["deadline_ms"] = clock["at_ms"] + 2000
            armed.append({"at_ms": clock["at_ms"], "endpoint": endpoint})

    monkeypatch.setattr(registry, "_arm_reopen_evidence_window", arm)

    def advance(at_ms: float) -> None:
        deadline = clock["deadline_ms"]
        if deadline is not None and deadline <= at_ms:
            clock["at_ms"] = deadline
            clock["deadline_ms"] = None
            pending = context.pending
            endpoint = pending.reopen_evidence_endpoint
            before = len(attempts)
            registry._expire_reopen_evidence_window(
                window.identity.session_id, context.stream_epoch, endpoint
            )
            expiries.append({
                "at_ms": deadline,
                "endpoint": endpoint,
                "scheduled": len(attempts) > before,
            })
        clock["at_ms"] = at_ms

    injected = False
    for event in data["events"]:
        advance(event["at_ms"])
        if expiries and not continued:
            break
        if event["kind"] == "final":
            result = ASRResult(
                task_epoch=event["task_epoch"],
                sentence_id=f"recorded-range-{event['task_epoch']}",
                revision=1,
                capture_start_sample=event["start_sample"],
                capture_end_sample=event["end_sample"],
                text="xx",
                is_final=True,
            )
            assert await registry.accept_asr_result(window.identity.session_id, result)
        else:
            final = event["kind"] == "vad_end"
            if final:
                context.asr.last_sent_sample = event["audio_watermark"]
            await registry.on_speech_segment(
                window.session,
                SpeechSegment(
                    session_id=window.identity.session_id,
                    stream_epoch=context.stream_epoch,
                    provider_task_epoch=0,
                    segment_id=f"recorded-vad-{event['bridge_line']}",
                    revision=1,
                    kind=SegmentKind.VAD,
                    capture_start_sample=event["sample"],
                    capture_end_sample=event["sample"] + 1,
                    final=final,
                    voiced_end_sample=event.get("voiced_end_sample"),
                    near_end_rms=0.01,
                ),
            )
            if final:
                # The source records successful provider finalization. The
                # fake provider has no PCM pump, so restore that known receipt.
                context.ingress.last_finalized_audio_watermark = event["audio_watermark"]
            if continued and not final and not injected:
                # A possible, unlogged interim after the short first final.
                # This is a differential control, never a historical claim.
                assert await registry.accept_asr_result(
                    window.identity.session_id,
                    ASRResult(
                        task_epoch=51,
                        sentence_id="synthetic-continuation",
                        revision=1,
                        capture_start_sample=9729000,
                        capture_end_sample=9750000,
                        text="xxxx",
                        is_final=False,
                    ),
                )
                injected = True
    advance(data["observed_reopen_commit"]["at_ms"] + 1)
    return {
        "armed": armed, "expiries": expiries, "attempts": attempts,
        "checks": checks,
        "diagnostics": [
            record.getMessage()
            for record in caplog.records
            if record.name == "services.agent.src.voice_core.media_session_input"
            and (
                "reopen evidence window" in record.getMessage()
                or "media reopened turn committing" in record.getMessage()
            )
        ],
        "data": data,
    }


@pytest.mark.asyncio
async def test_recorded_first_final_without_new_text_expires_before_long_wait(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    replay = await _replay(device_media_session, monkeypatch, caplog, continued=False)

    assert replay["armed"] == [{"at_ms": 664.016, "endpoint": 9728640}]
    assert replay["expiries"] == [{
        "at_ms": 2664.016, "endpoint": 9728640, "scheduled": True,
    }]
    assert replay["attempts"][-1] == (2664.016, 9728640)
    assert any("reason=no_new_text" in line for line in replay["diagnostics"])
    assert all("xx" not in line for line in replay["diagnostics"])


@pytest.mark.asyncio
async def test_missing_interim_control_can_explain_recorded_late_reopen_commit(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    replay = await _replay(device_media_session, monkeypatch, caplog, continued=True)

    assert replay["expiries"][0] == {
        "at_ms": 2664.016, "endpoint": 9728640, "scheduled": False,
    }
    assert any("reason=partial_after_endpoint" in line for line in replay["diagnostics"])
    assert any("reason=endpoint_uncovered" in line for line in replay["diagnostics"])
    # Later VAD endpoints are not covered by the first short final. The next
    # recorded final restores coverage, and the final reopen commits after 2 s.
    assert replay["armed"][-1] == {
        "at_ms": 28108.949, "endpoint": 10170240,
    }, json.dumps(replay["checks"])
    expiry = replay["expiries"][-1]
    assert expiry["scheduled"] is True and expiry["endpoint"] == 10170240
    observed = replay["data"]["observed_reopen_commit"]
    assert abs(expiry["at_ms"] - observed["at_ms"]) < 1
