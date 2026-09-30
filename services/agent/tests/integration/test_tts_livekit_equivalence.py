"""Transitional: new TTS stream base against the verbatim livekit-based adapters.

``legacy_livekit/`` holds the Doubao and CosyVoice modules exactly as they
were before the switch (still on ``livekit.agents.tts``). Every scenario runs
once against each implementation on a fresh mock server, and the observable
results must be identical: the PCM bytes and their frame cut, timed
transcripts and alignment, errors, retries, pool and server side effects,
traces, and the media provider adapter's reply chunks. Deleted together with
the livekit dependency.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from types import ModuleType
from typing import Any

import pytest
from livekit.agents import APIConnectOptions as LKConnectOptions
from livekit.agents.types import USERDATA_TIMED_TRANSCRIPT as LK_USERDATA
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers import cosyvoice_tts as new_cosy
from services.agent.src.providers import doubao_tts as new_doubao
from services.agent.src.providers.doubao_voice_catalog import catalog_by_id
from services.agent.src.providers.provider_errors import APIConnectOptions
from services.agent.src.providers.tts_stream import USERDATA_TIMED_TRANSCRIPT
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.provider_adapter import (
    ExistingVoiceProviderAdapter,
    ExistingVoiceProviderConfig,
)
from services.agent.tests.integration.legacy_livekit import cosyvoice_tts as old_cosy
from services.agent.tests.integration.legacy_livekit import doubao_tts as old_doubao
from services.agent.tests.integration.mock_servers import MockCosyVoiceServer, MockDoubaoServer

_VOICES = catalog_by_id()


def _options(module: ModuleType, max_retry: int, retry_interval: float = 0.01) -> Any:
    if module in (old_doubao, old_cosy):
        return LKConnectOptions(max_retry=max_retry, retry_interval=retry_interval)
    return APIConnectOptions(max_retry=max_retry, retry_interval=retry_interval)


def _userdata_key(module: ModuleType) -> str:
    return LK_USERDATA if module in (old_doubao, old_cosy) else USERDATA_TIMED_TRANSCRIPT


def _error(exc: BaseException | None) -> Any:
    if exc is None:
        return None
    return (
        type(exc).__name__,
        str(exc),
        getattr(exc, "retryable", None),
        type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
    )


def _words(items: Any) -> list[tuple[str, float, float]]:
    return [(str(item), item.start_time, item.end_time) for item in items]


class _Recorder:
    def __init__(self) -> None:
        self.traces: list[tuple[str, str, Any]] = []
        self.alignments: list[str] = []
        self.fallbacks: list[tuple[str, str, str, str]] = []
        self._utterances: dict[str, int] = {}

    def attach(self, tts: Any) -> None:
        tts.set_trace_callback(lambda name, status, detail: self.traces.append((name, status, detail)))
        tts.set_alignment_callback(self._alignment)
        if hasattr(tts, "set_voice_fallback_callback"):
            tts.set_voice_fallback_callback(
                lambda _fence, profile, resource, speaker, kind: self.fallbacks.append(
                    (profile, resource, speaker, kind)
                )
            )

    def _alignment(self, _fence: GenerationFence, utterance_id: str, status: str) -> None:
        # Utterance ids are random; keep only their identity pattern.
        index = self._utterances.setdefault(utterance_id, len(self._utterances))
        self.alignments.append(f"{index}:{status}")


# --------------------------------------------------------------------- Doubao


def _doubao_config(module: ModuleType, server: MockDoubaoServer, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "api_key": "test",
        "ws_url": server.ws_url,
        "speaker": _VOICES["warm_companion"].speaker_id,
        "pool_size": 1,
    }
    values.update(overrides)
    return module.DoubaoTTSConfig(**values)


def _make_personal(module: ModuleType, tts: Any) -> None:
    tts.apply_voice_profile(
        model=module.DOUBAO_PERSONAL_VOICE_MODEL,
        resource_id=module.DOUBAO_PERSONAL_VOICE_MODEL,
        voice="S_equivalence_personal",
        profile_id="equivalence-personal",
        provider="volcengine_doubao",
        voice_kind="personal",
    )


async def _doubao_stream(
    module: ModuleType,
    *,
    server_kwargs: dict[str, Any],
    config: dict[str, Any] | None = None,
    texts: tuple[str, ...] = ("你好，今天天气不错。",),
    max_retry: int = 0,
    prime: bool = False,
    personal: bool = False,
    personal_fallback: bool = False,
    failing_personal_pool: bool = False,
    push_delay_s: float = 0.0,
    close_after_first_event: bool = False,
) -> dict[str, Any]:
    server = MockDoubaoServer(**server_kwargs)
    server.start()
    tts = module.DoubaoTTS(_doubao_config(module, server, **(config or {})))
    recorder = _Recorder()
    recorder.attach(tts)
    events: list[Any] = []
    error: BaseException | None = None
    stream: Any = None
    try:
        if prime:
            await tts.pool.warm(1)
            await tts.synthesize_stream_text(["先建立一个可复用连接"], fence=GenerationFence("prime", 9, 9, 0))
            await asyncio.sleep(0.05)
        if personal_fallback:
            tts.configure_personal_fallback(
                profile_id="low_magnetic",
                provider="volcengine_doubao",
                model="seed-tts-2.0",
                resource_id="seed-tts-2.0",
                voice=_VOICES["low_magnetic"].speaker_id,
            )
        if personal:
            _make_personal(module, tts)
        if failing_personal_pool:
            original = tts._pools.for_config

            class FailingPool:
                async def acquire(self, *, wait_s: float = 0.3) -> object:
                    del wait_s
                    raise RuntimeError("clone pool unavailable")

            tts._pools.for_config = lambda cfg: (
                FailingPool()
                if cfg.resource_id == module.DOUBAO_PERSONAL_VOICE_MODEL
                else original(cfg)
            )
        tts.bind_fence(GenerationFence("equivalence", 1, 1, 0))
        stream = tts.stream(conn_options=_options(module, max_retry))
        try:
            async with stream:
                for index, text in enumerate(texts):
                    if index and push_delay_s:
                        await asyncio.sleep(push_delay_s)
                    stream.push_text(text)
                stream.end_input()
                async for event in stream:
                    events.append(event)
                    if close_after_first_event:
                        break
        except Exception as exc:
            error = exc
        if close_after_first_event:
            for _ in range(100):
                if server.canceled_sessions:
                    break
                await asyncio.sleep(0.01)
    finally:
        await tts.aclose()
        server.stop()
    key = _userdata_key(module)
    return {
        "pcm_sha256": hashlib.sha256(b"".join(bytes(e.frame.data) for e in events)).hexdigest(),
        "pcm_matches_server": b"".join(bytes(e.frame.data) for e in events) == server.pcm,
        "frames": [(len(bytes(e.frame.data)), e.frame.sample_rate, e.is_final) for e in events],
        "userdata_words": [_words(e.frame.userdata.get(key, [])) for e in events],
        "timed": _words(stream.timed_transcript()) if stream is not None else None,
        "alignment": stream.timed_transcript_alignment() if stream is not None else None,
        "error": _error(error),
        "server": {
            "connections": server.connections,
            "sessions": server.sessions,
            "task_requests": server.task_requests,
            "speakers": server.speakers,
            "canceled": len(server.canceled_sessions),
        },
        "discarded": tts.pool.discarded_count,
        "traces": recorder.traces,
        "alignments": recorder.alignments,
        "fallbacks": recorder.fallbacks,
    }


DOUBAO_SCENARIOS: dict[str, dict[str, Any]] = {
    "happy_ramp": {"server_kwargs": {"pcm_ramp": True}},
    "phrases_ramp": {
        "server_kwargs": {"pcm_ramp": True},
        "texts": ("你好，", "今天天气不错，", "我们出去走走吧。"),
        "push_delay_s": 0.02,
    },
    "split_pcm": {"server_kwargs": {"scenario": "split_pcm", "pcm_ramp": True}},
    "split_pcm_odd": {"server_kwargs": {"scenario": "split_pcm_odd", "pcm_ramp": True}},
    "many_chunks": {
        "server_kwargs": {"pcm_chunks": 9, "pcm_ramp": True, "chunk_delay_s": 0.01},
        "texts": ("这是一段比较长的回答，用来驱动多个音频分片的流式输出。",),
    },
    "scaled_ts": {"server_kwargs": {"scenario": "scaled_ts"}},
    "degraded_ts": {"server_kwargs": {"scenario": "degraded_ts"}},
    "empty_ts": {"server_kwargs": {"scenario": "empty_ts"}, "max_retry": 1},
    "odd_pcm": {"server_kwargs": {"scenario": "odd_pcm"}},
    "stall_after_first_pcm": {
        "server_kwargs": {"scenario": "stall_after_first_pcm", "pcm_chunks": 4},
        "config": {"total_timeout_s": 0.2},
        "max_retry": 2,
    },
    "first_audio_timeout_exhausts_retries": {
        "server_kwargs": {"scenario": "slow"},
        "config": {"first_audio_timeout_s": 0.05},
        "max_retry": 2,
    },
    "first_audio_timeout_then_retry": {
        "server_kwargs": {"scenario": "slow_once"},
        "config": {"first_audio_timeout_s": 0.05},
        "max_retry": 1,
    },
    "expired_pooled_connection": {
        "server_kwargs": {"scenario": "expire_after_first"},
        "max_retry": 1,
        "prime": True,
    },
    "empty_input": {"server_kwargs": {}, "texts": ()},
    "blank_input": {"server_kwargs": {}, "texts": ("   ",)},
    "personal_first_audio_failure_falls_back_once": {
        "server_kwargs": {"scenario": "slow_once"},
        "config": {"first_audio_timeout_s": 0.05},
        "texts": ("只", "播一次"),
        "personal": True,
        "personal_fallback": True,
    },
    "personal_sustained_failure_falls_back_once": {
        "server_kwargs": {"scenario": "slow"},
        "config": {"first_audio_timeout_s": 0.05},
        "personal": True,
        "max_retry": 3,
    },
    "personal_pool_failure_falls_back": {
        "server_kwargs": {},
        "personal": True,
        "personal_fallback": True,
        "failing_personal_pool": True,
    },
    "consumer_closes_early": {
        "server_kwargs": {"pcm_chunks": 20, "chunk_delay_s": 0.02},
        "close_after_first_event": True,
    },
}


@pytest.mark.parametrize("scenario", sorted(DOUBAO_SCENARIOS))
async def test_doubao_stream_matches_the_livekit_adapter(scenario: str) -> None:
    kwargs = DOUBAO_SCENARIOS[scenario]
    old = await _doubao_stream(old_doubao, **kwargs)
    new = await _doubao_stream(new_doubao, **kwargs)
    assert new == old


# ------------------------------------------------------------------ CosyVoice


async def _cosy_stream(
    module: ModuleType,
    *,
    scenario: str,
    config: dict[str, Any] | None = None,
    max_retry: int = 0,
    clone: bool = False,
) -> dict[str, Any]:
    server = MockCosyVoiceServer(scenario=scenario)
    server.start()
    values: dict[str, Any] = {"api_key": "test", "ws_url": server.ws_url, "pool_size": 1}
    values.update(config or {})
    tts = module.CosyVoiceTTS(module.CosyVoiceConfig(**values))
    recorder = _Recorder()
    recorder.attach(tts)
    if clone:
        tts.apply_voice_profile(
            model="cosyvoice-v3.5-plus",
            voice="cosyvoice-v3.5-plus-clone-equivalence",
            voice_kind="personal",
        )
    tts.bind_fence(GenerationFence("cosy-equivalence", 1, 1, 0))
    events: list[Any] = []
    error: BaseException | None = None
    try:
        async with tts.stream(conn_options=_options(module, max_retry)) as stream:
            stream.push_text("你好，这是流式测试。")
            stream.end_input()
            async for event in stream:
                events.append(event)
    except Exception as exc:
        error = exc
    finally:
        await tts.aclose()
        server.stop()
    key = _userdata_key(module)
    return {
        "pcm": b"".join(bytes(e.frame.data) for e in events),
        "frames": [(len(bytes(e.frame.data)), e.frame.sample_rate, e.is_final) for e in events],
        "userdata_words": [_words(e.frame.userdata.get(key, [])) for e in events],
        "error": _error(error),
        "traces": recorder.traces,
        "alignments": recorder.alignments,
    }


COSY_SCENARIOS: dict[str, dict[str, Any]] = {
    "happy": {"scenario": "happy"},
    "late_ts": {"scenario": "late_ts"},
    "split_pcm": {"scenario": "split_pcm"},
    "fail": {"scenario": "fail", "max_retry": 1},
    "clone_fail_falls_back_to_baseline": {"scenario": "fail", "max_retry": 1, "clone": True},
    "slow_once_retry": {"scenario": "slow_once", "config": {"first_audio_timeout_s": 0.05}, "max_retry": 1},
    "slow_exhausts": {"scenario": "slow", "config": {"first_audio_timeout_s": 0.05}, "max_retry": 1},
    "empty_ts_once": {"scenario": "empty_ts_once", "max_retry": 1},
    "stall_after_pcm": {"scenario": "stall_after_pcm", "config": {"total_timeout_s": 0.2}, "max_retry": 1},
}


@pytest.mark.parametrize("scenario", sorted(COSY_SCENARIOS))
async def test_cosyvoice_stream_matches_the_livekit_adapter(scenario: str) -> None:
    kwargs = COSY_SCENARIOS[scenario]
    old = await _cosy_stream(old_cosy, **kwargs)
    new = await _cosy_stream(new_cosy, **kwargs)
    assert new == old


# ------------------------------------------- media provider adapter (the production seam)


class _ScriptedLLM:
    def __init__(self, tokens: tuple[str, ...], delay_s: float) -> None:
        self._tokens = tokens
        self._delay_s = delay_s

    async def stream(self, _request: Any) -> AsyncIterator[str]:
        for token in self._tokens:
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            yield token


async def _adapter_reply(module: ModuleType, *, server_kwargs: dict[str, Any]) -> Any:
    server = MockDoubaoServer(**server_kwargs)
    server.start()
    tts = module.DoubaoTTS(_doubao_config(module, server))
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=lambda: None,  # type: ignore[arg-type,return-value]
        language_model=_ScriptedLLM(
            ("你好，", "今天", "天气不错。", "我们", "出去走走吧！", "好不好？"),
            delay_s=0.01,
        ),  # type: ignore[arg-type]
        speech_synthesis=tts,  # type: ignore[arg-type]
        config=ExistingVoiceProviderConfig(output_sample_rate=24_000),
    )
    identity = SessionIdentity("adapter-equivalence", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    chunks: list[Any] = []
    error: BaseException | None = None
    try:
        async for chunk in adapter.generate_reply(identity, "今天天气怎么样", fence):
            chunks.append(chunk)
    except Exception as exc:
        error = exc
    finally:
        await tts.aclose()
        server.stop()
    return {
        "chunks": chunks,
        "error": _error(error),
        "task_requests": server.task_requests,
    }


@pytest.mark.parametrize(
    "server_kwargs",
    [
        {"pcm_ramp": True},
        {"pcm_ramp": True, "pcm_chunks": 7, "chunk_delay_s": 0.01},
        {"scenario": "split_pcm_odd", "pcm_ramp": True},
        {"scenario": "degraded_ts", "pcm_ramp": True},
        {"scenario": "empty_ts"},
    ],
    ids=["single_chunk", "many_chunks", "odd_split", "degraded_alignment", "no_timestamps"],
)
async def test_provider_adapter_reply_chunks_match(server_kwargs: dict[str, Any]) -> None:
    old = await _adapter_reply(old_doubao, server_kwargs=server_kwargs)
    new = await _adapter_reply(new_doubao, server_kwargs=server_kwargs)
    assert new == old
    if new["error"] is None:
        assert new["chunks"]
