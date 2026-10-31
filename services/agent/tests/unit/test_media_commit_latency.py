"""The final-to-commit gate: a bounded wait for the device VAD end, and one verdict per turn.

Soak 20261029-200043: the slow turns spent 1.0-2.0 s between the ASR final and the commit waiting for the
device VAD end, and the two semantic classifiers answered twice for the same turn because the final's text
and the commit's projection differed.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.voice_core.media_session_commit import _resolve_media_turn_text
from services.agent.tests.unit.media_session_support import (
    _accept_media_asr_decision,
    _accept_media_asr_final,
    _device_vad,
    _wait_until,
)

_START = 40_000
_HEAD = "明天我们去"
_TAIL = "动物园好不好"


class _Verdict:
    """A semantic classifier of a test: it records the sentence it is asked and answers False at once."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.calls.append(text)
        return False


async def _open(
    device_media_session: Any,
    session_id: str,
    *,
    grace_s: float = 0.5,
) -> SimpleNamespace:
    window = await device_media_session(session_id, during_playback=False)
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


@pytest.mark.asyncio
async def test_a_final_commits_without_a_device_vad_end_after_the_cap(
    device_media_session: Any,
) -> None:
    """The final carries the endpoint evidence: 300 ms without the device's VAD end commits anyway."""

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
    assert window.provider.prepared == []  # the commit is still waiting for the VAD end
    # The device keeps streaming; its VAD end is what is late, not the uplink.
    window.context.asr.last_sent_sample = _START + 16_000 + 4_800

    await _wait_until(lambda: bool(window.provider.prepared), timeout=2.0)
    elapsed = time.monotonic() - started

    assert window.provider.prepared == [_HEAD]
    assert 0.2 <= elapsed < 0.8  # the 300 ms cap, not the 6 s endpoint tail


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

    window = await _open(
        device_media_session, f"commit-grace-resume-{resume_after_s}", grace_s=0.5
    )
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

    pending_end = asyncio.create_task(
        _device_vad(window, "vad-end", _START + 16_000, final=True)
    )
    await asyncio.sleep(resume_after_s)
    assert window.provider.prepared == []
    await _device_vad(window, "vad-start-2", _START + 20_000, final=False)
    await pending_end
    await asyncio.sleep(0.6)

    assert window.provider.prepared == []
    assert context.pending.turn_endpoint_sample is None
