"""Content-free evidence for the 2026-10-10 ASR/endpoint investigation.

The probe's sample ranges are recorded facts. Overlap blockers and close
transcripts below are synthetic controls, not reconstructions of missing logs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, replace

import pytest
import pytest_asyncio
from services.agent.src.conversation_close_router import (
    conversation_close_cache_key,
    rule_conversation_close_only,
)
from services.agent.src.providers.funasr_protocol import (
    FunASRSentence,
    sentence_to_asr_result,
)
from services.agent.src.voice_core.asr_stream_supervisor import (
    ASRDecisionReason,
    ASRStreamSupervisor,
)
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.src.voice_core.speech_timeline import ASRResult, ASRWordTiming
from services.agent.tests.unit.media_session_support import FakeMediaProvider

COMMIT_LOGGER = "services.agent.src.voice_core.media_session_commit"
CLOSE_LOGGER = "services.agent.src.voice_core.media_session_playback_stop"


def _final(
    sentence: str,
    start: int,
    end: int,
    *,
    text: str = "private transcript",
    rescue: bool = False,
) -> ASRResult:
    return ASRResult(
        task_epoch=1,
        sentence_id=sentence,
        revision=1,
        capture_start_sample=start,
        capture_end_sample=end,
        text=text,
        is_final=True,
        rescue_synthesized=rescue,
    )


@pytest_asyncio.fixture
async def scene():
    bridge = MediaBridgeGrpcServer()
    provider = FakeMediaProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge, provider_factory=lambda _identity: provider
    )
    identity = SessionIdentity("asr-diagnostics")
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    try:
        yield registry, context
    finally:
        session.close()
        await registry.finalize_session(identity.session_id)


@pytest.mark.parametrize("rescue,end", [(False, 350), (True, 800)])
def test_overlap_evidence_names_blocker_without_text_or_changing_arbitration(
    rescue: bool, end: int
) -> None:
    supervisor = ASRStreamSupervisor()
    first = _final("private sentence id", 200, 400)
    assert supervisor.accept_result(first, session_id="s")
    before = supervisor.timeline.pending

    decision = supervisor.accept_result(
        _final("other", 100, end, text="private rejected text", rescue=rescue),
        session_id="s",
    )

    assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
    assert decision.accepted is None
    assert supervisor.timeline.pending == before
    assert decision.overlap is not None
    assert asdict(decision.overlap) == {
        "capture_start_sample": 100,
        "capture_end_sample": end,
        "conflict_count": 1,
        "conflicts": (
            {
                "task_epoch": 1,
                "capture_start_sample": 200,
                "capture_end_sample": 400,
                "text_len": len(first.text),
                "rescue_synthesized": False,
            },
        ),
    }
    assert "private" not in json.dumps(asdict(decision))


def test_overlap_evidence_is_bounded_but_keeps_total_count() -> None:
    supervisor = ASRStreamSupervisor()
    for index in range(6):
        assert supervisor.accept_result(
            _final(f"s{index}", index * 100, index * 100 + 80), session_id="s"
        )
    decision = supervisor.accept_result(
        _final("rescue", 0, 700, rescue=True), session_id="s"
    )

    assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
    assert decision.overlap is not None
    assert decision.overlap.conflict_count == 6
    assert len(decision.overlap.conflicts) == 4
    assert len(supervisor.timeline.pending) == 6


def test_overlap_evidence_uses_normalized_uncommitted_range() -> None:
    supervisor = ASRStreamSupervisor()
    assert supervisor.accept_result(_final("blocker", 200, 300), session_id="s")
    supervisor.mark_committed(100)
    result = replace(
        _final("rescue", 0, 400, text="abcd", rescue=True),
        word_timings=tuple(
            ASRWordTiming(char, index * 100, (index + 1) * 100)
            for index, char in enumerate("abcd")
        ),
    )

    decision = supervisor.accept_result(result, session_id="s")

    assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
    assert decision.overlap is not None
    assert decision.overlap.capture_start_sample == 100
    assert decision.overlap.capture_end_sample == 400
    assert result.capture_start_sample == 0


def test_real_provider_still_supersedes_rescue_without_rejection_evidence() -> None:
    supervisor = ASRStreamSupervisor()
    assert supervisor.accept_result(
        _final("rescue", 0, 800, rescue=True), session_id="s"
    )
    result = _final("provider", 200, 400)

    decision = supervisor.accept_result(result, session_id="s")

    assert decision.accepted == result
    assert decision.evicted_sentence_ids == ("rescue",)
    assert decision.overlap is None


async def test_registry_logs_supervisor_overlap_snapshot(scene, caplog) -> None:
    registry, context = scene
    session_id = context.identity.session_id
    assert await registry.accept_asr_result(session_id, _final("provider", 200, 400))
    with caplog.at_level(logging.WARNING, logger=COMMIT_LOGGER):
        accepted = await registry.accept_asr_result(
            session_id, _final("rescue", 100, 800, text="hidden content", rescue=True)
        )

    assert accepted is False
    [line] = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("media ASR result rejected")
    ]
    assert "reason=cross_sentence_overlap" in line
    assert "rescue_synthesized=True" in line
    evidence = json.loads(line.split(" overlap=", 1)[1])
    assert evidence["capture_start_sample"] == 100
    assert evidence["conflict_count"] == 1
    assert evidence["conflicts"][0]["capture_start_sample"] == 200
    assert "private transcript" not in line and "hidden content" not in line


async def test_probe_final_outside_old_endpoint_is_diagnosed_not_force_committed(
    scene, caplog
) -> None:
    registry, context = scene
    pending = context.pending
    pending.turn_start_sample = 320
    pending.turn_end_sample = 8960
    pending.turn_endpoint_sample = 8960
    pending.turn_endpoint_grace_deadline = time.monotonic() + 60
    # p01: the final's task-relative range has an absolute origin at 23360.
    result = sentence_to_asr_result(
        FunASRSentence(2, "xx", 4810, 4930, True, False, ()),
        task_epoch=3,
        sample_offset=23360,
    )
    assert (result.capture_start_sample, result.capture_end_sample) == (100320, 102240)
    assert await registry.accept_asr_result(context.identity.session_id, result)

    with caplog.at_level(logging.INFO, logger=COMMIT_LOGGER):
        fence, reason = await registry.commit_user_turn(
            context.identity.session_id, stream_epoch=1, start_sample=320, end_sample=8960
        )

    assert fence is None and reason == "empty_media_turn"
    assert pending.turn_endpoint_sample == 8960
    assert pending.turn_end_sample == 102240
    [line] = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("media turn has no text")
    ]
    assert "pending_text_ranges=[(100320, 102240)]" in line
    assert "text_after_endpoint=1" in line
    assert "pending_end=102240" in line
    assert "active_vad_start=None" in line
    assert "xx" not in line


@pytest.mark.parametrize(
    "text,cached,rule_match",
    [("bye", None, True), ("sing", True, False)],
)
async def test_final_close_logs_rule_match_without_exposing_or_changing_cache(
    scene, caplog, text: str, cached: bool | None, rule_match: bool
) -> None:
    registry, context = scene
    context.identity = replace(
        context.identity,
        client_type="device",
        account_id="account",
        device_id="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
        audio_mode="interrupt_assist",
    )
    if cached is not None:
        context.runtime._conversation_close_cache[conversation_close_cache_key(text)] = cached
    cache_before = dict(context.runtime._conversation_close_cache)
    result = _final("final", 200, 400, text=text)

    with caplog.at_level(logging.INFO, logger=CLOSE_LOGGER):
        registry._maybe_early_commit_conversation_close(context, result)

    assert context.pending.conversation_close_endpoint_pinned == 400
    assert context.runtime._conversation_close_cache == cache_before
    [line] = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("media early conversation-close endpoint")
    ]
    assert "source=final" in line
    assert f"rule_match={rule_match}" in line
    assert "asr_samples=200-400" in line
    assert "asr_task_epoch=1" in line
    assert "asr_rescue=False" in line
    assert text not in line


@pytest.mark.parametrize(
    "text",
    [
        "\u4f60\u4f1a\u5531\u6b4c\u5417\uff1f",
        "\u4e3a\u4ec0\u4e48\u4f1a\u4e0b\u96e8\u5440\uff1f",
        "\u5929\u7a7a\u4e3a\u4ec0\u4e48\u662f\u84dd\u8272\u7684\uff1f",
    ],
)
def test_original_question_text_is_not_a_lexical_farewell(text: str) -> None:
    assert rule_conversation_close_only(text) is False
