"""Pre-synthesized fixed-safety audio: manifest, fallback, AIGC and interruption."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.prompts import MINOR_DAILY_LIMIT_PHRASE
from services.agent.src.providers.fixed_safety_audio import (
    DEFAULT_FIXED_SAFETY_AUDIO_DIR,
    FIXED_SAFETY_AUDIO_ARTIST,
    FIXED_SAFETY_AUDIO_SOFTWARE,
    FixedSafetyAudioCatalog,
    FixedSafetyTTS,
    approved_templates,
    heard_complete_by_sentence,
    parse_wav_bytes,
    sentence_spans,
    spoken_sha256,
)
from services.agent.src.voice_core.aigc_notice import AigcNoticeConfig
from services.agent.src.voice_core.crisis_script_interrupt import (
    crisis_self_interrupt_event_id,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_protocol import MediaEnvelope, SessionIdentity
from services.agent.src.voice_core.media_session import MediaReplyChunk
from services.agent.src.voice_core.media_session_aigc_notice import (
    MediaSessionAigcNoticeMixin,
)
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.tests.unit.test_media_session_golden import (
    _Harness,
    _ScriptedProvider,
)
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    FixedSafetyScript,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_AUDIO_DIR = _REPO_ROOT / DEFAULT_FIXED_SAFETY_AUDIO_DIR
_TEMPLATE_IDS = (
    "crisis-self-v1",
    "victim-support-v1",
    "support-other-v1",
    "daily-limit",
)
# Exactly the four approved clips' PCM hashes from the reviewed manifest.
_APPROVED_PCM_SHA256 = {
    "crisis-self-v1": ("f28af42f21ab1efc8c641c58bc4aba86cf9d7353812af6d3e90eac039f1fb545"),
    "victim-support-v1": ("6660d1446cd9fbcb6019cc86fe0cd0bc2f1333aa7cde427432008f20edcf5e3e"),
    "support-other-v1": ("67d919eca0a4d30089b171cfc9caa806b3788ac60180964cf2e0014b0c2a5a3c"),
    "daily-limit": ("c1fb628b85151b9588d49cc1663c516fcfc890e9e2953663e2b2bfade0b8bc93"),
}


def _script_for(template_id: str) -> FixedSafetyScript:
    return {
        CRISIS_SELF_SCRIPT.template_id: CRISIS_SELF_SCRIPT,
        SUPPORT_VICTIM_SCRIPT.template_id: SUPPORT_VICTIM_SCRIPT,
        SUPPORT_RELAY_SCRIPT.template_id: SUPPORT_RELAY_SCRIPT,
    }[template_id]


class _InnerStream:
    def __init__(self, owner: _InnerTTS) -> None:
        self._owner = owner
        self.ended = False

    def push_text(self, text: str) -> None:
        self._owner.pushed.append(text)

    def end_input(self) -> None:
        self.ended = True

    def __aiter__(self) -> _InnerStream:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        return None


class _InnerTTS:
    """A live provider double that records every fallback call."""

    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.streams = 0

    def stream(self, **kwargs: Any) -> _InnerStream:
        _ = kwargs
        self.streams += 1
        return _InnerStream(self)


class _RuntimePcmStream:
    def __init__(self, pcm: bytes) -> None:
        self._pcm = pcm
        self._emitted = False

    def push_text(self, text: str) -> None:
        _ = text

    def end_input(self) -> None:
        return None

    def __aiter__(self) -> _RuntimePcmStream:
        return self

    async def __anext__(self) -> bytes:
        if self._emitted:
            raise StopAsyncIteration
        self._emitted = True
        return self._pcm

    async def aclose(self) -> None:
        return None


class _RuntimePcmTTS:
    """A live provider that produces a take distinguishable from the clips."""

    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm
        self.pushed: list[str] = []
        self.streams = 0

    def stream(self, **kwargs: Any) -> _RuntimePcmStream:
        _ = kwargs
        self.streams += 1
        return _RuntimePcmStream(self.pcm)


class _DoubaoRealtimeTTS:
    """A Doubao double that fails the test if a valid clip opens it."""

    provider = "volcengine_doubao"

    def __init__(self) -> None:
        self.streams = 0

    def stream(self, **kwargs: Any) -> Any:
        _ = kwargs
        self.streams += 1
        raise AssertionError("Doubao must not synthesize an approved fixed-safety clip")


def _load(directory: Path = _AUDIO_DIR) -> FixedSafetyAudioCatalog:
    return FixedSafetyAudioCatalog.load(directory)


def _wrapper(directory: Path = _AUDIO_DIR) -> tuple[FixedSafetyTTS, _InnerTTS]:
    inner = _InnerTTS()
    return FixedSafetyTTS(inner, _load(directory)), inner


async def _serve(wrapper: FixedSafetyTTS, text: str) -> bytes:
    stream = wrapper.stream()
    stream.push_text(text)
    stream.end_input()
    return b"".join([chunk async for chunk in stream])


def _tampered_copy(
    tmp_path: Path,
    template_id: str = "crisis-self-v1",
    **changes: str,
) -> Path:
    directory = tmp_path / template_id
    shutil.copytree(_AUDIO_DIR, directory)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["templates"]:
        if entry["template_id"] == template_id:
            entry.update(changes)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return directory


class _ScriptLLM:
    def __init__(self, script: FixedSafetyScript) -> None:
        self.script = script

    def stream(self, request: LanguageModelRequest) -> AsyncIterator[FixedSafetyScript]:
        _ = request

        async def items() -> AsyncIterator[FixedSafetyScript]:
            yield self.script

        return items()


class _TtsIntent:
    intent_id = "ack-intent"

    def __init__(self, text: str) -> None:
        self.tts_source = text

    def WhichOneof(self, name: str) -> str | None:
        return "tts_source" if name == "source" else None


def _adapter(
    script: FixedSafetyScript,
    wrapper: FixedSafetyTTS,
) -> ExistingVoiceProviderAdapter:
    return ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: None),
        language_model=_ScriptLLM(script),
        speech_synthesis=cast(Any, wrapper),
    )


def _identity(session_id: str) -> SessionIdentity:
    return SessionIdentity(session_id, stream_epoch=1)


def _fence(identity: SessionIdentity) -> GenerationFence:
    return GenerationFence(identity.session_id, 1, 1, 0)


async def _reply_pcm(
    adapter: ExistingVoiceProviderAdapter,
    identity: SessionIdentity,
    fence: GenerationFence,
    script: FixedSafetyScript,
) -> bytes:
    chunks = [chunk async for chunk in adapter.generate_reply(identity, "测试", fence)]
    _ = script
    return b"".join(chunk.pcm_s16le for chunk in chunks)


@pytest.mark.asyncio
async def test_fixed_safety_audio_crisis_tiers_and_daily_limit_play_prerecorded_without_realtime_tts() -> (
    None
):
    catalog = _load()
    for template_id in _TEMPLATE_IDS:
        template = catalog.get(template_id)
        assert template is not None and template.pcm
        if template_id == "daily-limit":
            inner = _InnerTTS()
            wrapper = FixedSafetyTTS(inner, catalog)
            identity = _identity(f"daily-{template_id}")
            adapter = ExistingVoiceProviderAdapter(
                asr_session_factory=cast(Any, lambda: None),
                language_model=_ScriptLLM(CRISIS_SELF_SCRIPT),
                speech_synthesis=cast(Any, wrapper),
            )
            chunks = [
                chunk
                async for chunk in adapter.generate_output(
                    identity,
                    _TtsIntent(MINOR_DAILY_LIMIT_PHRASE),
                    _fence(identity),
                    work_id="daily-limit",
                    source_start_sample=0,
                )
            ]
            pcm = b"".join(chunk.pcm_s16le for chunk in chunks)
        else:
            script = _script_for(template_id)
            inner = _InnerTTS()
            wrapper = FixedSafetyTTS(inner, catalog)
            identity = _identity(f"crisis-{template_id}")
            fence = _fence(identity)
            adapter = _adapter(script, wrapper)
            pcm = await _reply_pcm(adapter, identity, fence, script)
        assert inner.streams == 0
        assert pcm.startswith(template.pcm)
        assert set(pcm[len(template.pcm) :]) <= {0}


@pytest.mark.asyncio
async def test_fixed_safety_prerecorded_plays_before_any_realtime_tts_including_doubao() -> None:
    catalog = _load()
    for template_id in _TEMPLATE_IDS:
        template = catalog.get(template_id)
        assert template is not None and template.pcm
        inner = _DoubaoRealtimeTTS()
        wrapper = FixedSafetyTTS(cast(Any, inner), catalog)
        if template_id == "daily-limit":
            identity = _identity(f"precedes-live-daily-{template_id}")
            adapter = ExistingVoiceProviderAdapter(
                asr_session_factory=cast(Any, lambda: None),
                language_model=_ScriptLLM(CRISIS_SELF_SCRIPT),
                speech_synthesis=cast(Any, wrapper),
            )
            chunks = [
                chunk
                async for chunk in adapter.generate_output(
                    identity,
                    _TtsIntent(MINOR_DAILY_LIMIT_PHRASE),
                    _fence(identity),
                    work_id="precedes-live-daily-limit",
                    source_start_sample=0,
                )
            ]
            pcm = b"".join(chunk.pcm_s16le for chunk in chunks)
        else:
            script = _script_for(template_id)
            identity = _identity(f"precedes-live-crisis-{template_id}")
            adapter = _adapter(script, wrapper)
            pcm = await _reply_pcm(adapter, identity, _fence(identity), script)
        assert inner.streams == 0
        assert pcm.startswith(template.pcm)


@pytest.mark.asyncio
async def test_doubao_only_provider_never_speaks_a_crisis_turn(tmp_path: Path) -> None:
    """A broken configured clip goes to the shipped approved clip, not Doubao."""

    broken_directory = _tampered_copy(tmp_path, pcm_sha256="0" * 64)
    inner = _DoubaoRealtimeTTS()
    wrapper = FixedSafetyTTS(inner, _load(broken_directory))
    identity = _identity("doubao-never-crisis")
    adapter = _adapter(CRISIS_SELF_SCRIPT, wrapper)

    pcm = await _reply_pcm(adapter, identity, _fence(identity), CRISIS_SELF_SCRIPT)

    assert inner.streams == 0
    assert pcm.startswith(_load().get("crisis-self-v1").pcm or b"")


@pytest.mark.asyncio
async def test_crisis_turn_plays_only_approved_prerecorded_pcm_sha256() -> None:
    """Every crisis tier and daily-limit turn emits exactly one approved PCM."""

    catalog = _load()
    for template_id in _TEMPLATE_IDS:
        template = catalog.get(template_id)
        assert template is not None and template.pcm
        assert template.pcm_sha256 == _APPROVED_PCM_SHA256[template_id]
        inner = _InnerTTS()
        wrapper = FixedSafetyTTS(inner, catalog)
        if template_id == "daily-limit":
            identity = _identity(f"sha256-daily-{template_id}")
            adapter = ExistingVoiceProviderAdapter(
                asr_session_factory=cast(Any, lambda: None),
                language_model=_ScriptLLM(CRISIS_SELF_SCRIPT),
                speech_synthesis=cast(Any, wrapper),
            )
            chunks = [
                chunk
                async for chunk in adapter.generate_output(
                    identity,
                    _TtsIntent(MINOR_DAILY_LIMIT_PHRASE),
                    _fence(identity),
                    work_id="daily-limit-sha256",
                    source_start_sample=0,
                )
            ]
            pcm = b"".join(chunk.pcm_s16le for chunk in chunks)
        else:
            script = _script_for(template_id)
            identity = _identity(f"sha256-crisis-{template_id}")
            adapter = _adapter(script, wrapper)
            pcm = await _reply_pcm(adapter, identity, _fence(identity), script)
        assert inner.streams == 0
        assert hashlib.sha256(pcm).hexdigest() == _APPROVED_PCM_SHA256[template_id]


@pytest.mark.asyncio
async def test_runtime_synthesized_crisis_audio_is_never_cached_as_approved(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A live fallback take must not become a later turn's approved audio."""

    runtime_pcm = b"\x42\x00" * 960
    broken_directory = _tampered_copy(tmp_path, pcm_sha256="0" * 64)
    runtime_inner = _RuntimePcmTTS(runtime_pcm)
    broken_wrapper = FixedSafetyTTS(runtime_inner, _load(broken_directory))
    identity = _identity("runtime-synth-fallback")
    broken_fence = _fence(identity)
    broken_adapter = _adapter(CRISIS_SELF_SCRIPT, broken_wrapper)

    with caplog.at_level("WARNING"):
        first_pcm = await _reply_pcm(broken_adapter, identity, broken_fence, CRISIS_SELF_SCRIPT)

    assert first_pcm == runtime_pcm
    assert runtime_inner.streams == 1
    assert broken_adapter.fixed_safety_heard_complete(broken_fence, len(runtime_pcm) // 2) is None
    assert any(
        "template_id=crisis-self-v1" in record.getMessage()
        and "not_playable" in record.getMessage()
        for record in caplog.records
    )

    approved_inner = _InnerTTS()
    approved_wrapper = FixedSafetyTTS(approved_inner, _load())
    approved_identity = _identity("approved-after-runtime-synth")
    approved_pcm = await _reply_pcm(
        _adapter(CRISIS_SELF_SCRIPT, approved_wrapper),
        approved_identity,
        _fence(approved_identity),
        CRISIS_SELF_SCRIPT,
    )

    assert approved_inner.streams == 0
    assert hashlib.sha256(approved_pcm).hexdigest() == _APPROVED_PCM_SHA256["crisis-self-v1"]
    assert approved_pcm != runtime_pcm


@pytest.mark.asyncio
async def test_fixed_safety_audio_hash_mismatch_falls_back_to_realtime_tts_and_alerts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = _tampered_copy(tmp_path, audio_sha256="0" * 64)
    wrapper, inner = _wrapper(directory)

    with caplog.at_level("ERROR"):
        pcm = await _serve(wrapper, CRISIS_SELF_SCRIPT.spoken)

    assert pcm == b""
    assert inner.pushed == [CRISIS_SELF_SCRIPT.spoken]
    assert inner.streams == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "template_id=crisis-self-v1" in message and "audio_sha256 mismatch" in message
        for message in messages
    )
    assert not any("12356" in message or "不想活" in message for message in messages)


@pytest.mark.asyncio
async def test_fixed_safety_audio_missing_file_falls_back_to_realtime_tts_and_alerts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "fixed_safety"
    shutil.copytree(_AUDIO_DIR, directory)
    (directory / "crisis-self-v1.wav").unlink()
    wrapper, inner = _wrapper(directory)

    with caplog.at_level("ERROR"):
        pcm = await _serve(wrapper, CRISIS_SELF_SCRIPT.spoken)

    assert pcm == b""
    assert inner.pushed == [CRISIS_SELF_SCRIPT.spoken]
    assert any("template_id=crisis-self-v1" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_fixed_safety_audio_fallback_sends_hotline_numbers_as_yao_text(
    tmp_path: Path,
) -> None:
    directory = _tampered_copy(tmp_path, audio_sha256="0" * 64)
    wrapper, inner = _wrapper(directory)

    await _serve(wrapper, CRISIS_SELF_SCRIPT.display)

    assert inner.pushed == [CRISIS_SELF_SCRIPT.spoken]
    assert all(not character.isdigit() for character in inner.pushed[0])
    assert "幺二零" in inner.pushed[0]
    assert "幺二三五六" in inner.pushed[0]

    victim_directory = _tampered_copy(tmp_path, "victim-support-v1", audio_sha256="1" * 64)
    victim_wrapper, victim_inner = _wrapper(victim_directory)
    await _serve(victim_wrapper, SUPPORT_VICTIM_SCRIPT.display)
    assert victim_inner.pushed == [SUPPORT_VICTIM_SCRIPT.spoken]
    assert all(not character.isdigit() for character in victim_inner.pushed[0])
    assert "幺幺零" in victim_inner.pushed[0]


class _AigcHost(MediaSessionAigcNoticeMixin):
    def __init__(self, config: AigcNoticeConfig) -> None:
        self.aigc_notice_config = config
        self.aigc_round_logger = None
        self.opening = 0
        self.closing = 0

    async def _play_aigc_opening_marker(self, context: Any, pcm: bytes) -> None:
        _ = context, pcm
        self.opening += 1

    async def _play_aigc_closing_marker(self, context: Any) -> bool:
        if context.aigc_closing_marker_state != "idle":
            return False
        context.aigc_closing_marker_state = "done"
        self.closing += 1
        return True


def _notice_context() -> Any:
    return SimpleNamespace(
        aigc_round=None,
        aigc_round_active=False,
        aigc_opening_marker_pending=False,
        aigc_opening_marker_ready_at=None,
        aigc_opening_marker_started_at=None,
        aigc_closing_marker_state="idle",
        closed=False,
        standby_requested=False,
        identity=SimpleNamespace(session_id="prerecorded-safety", client_type="device"),
    )


@pytest.mark.asyncio
async def test_aigc_notice_plays_once_at_start_and_end_on_prerecorded_safety_turn() -> None:
    host = _AigcHost(AigcNoticeConfig())
    context = _notice_context()
    wrapper, inner = _wrapper()

    host._open_aigc_round(context)
    await host._speak_aigc_opening_marker(context)
    await host._speak_aigc_opening_marker(context)
    pcm = await _serve(wrapper, CRISIS_SELF_SCRIPT.spoken)
    await host._play_aigc_closing_marker(context)
    await host._play_aigc_closing_marker(context)

    assert pcm.startswith(_load().get("crisis-self-v1").pcm or b"")
    assert inner.streams == 0
    assert host.opening == 1
    assert host.closing == 1


@pytest.mark.asyncio
async def test_fixed_safety_audio_interrupt_reports_heard_complete_by_sentence() -> None:
    template = _load().get("crisis-self-v1")
    assert template is not None and template.pcm
    total = len(template.pcm) // 2
    spans = sentence_spans(template.spoken, total)
    assert len(spans) >= 3

    assert (
        heard_complete_by_sentence(
            template.spoken,
            played_samples=spans[-2].end_sample,
            total_samples=total,
        )
        is False
    )
    assert (
        heard_complete_by_sentence(
            template.spoken,
            played_samples=total,
            total_samples=total,
        )
        is True
    )

    identity = _identity("heard-complete")
    fence = _fence(identity)
    wrapper, inner = _wrapper()
    adapter = _adapter(CRISIS_SELF_SCRIPT, wrapper)
    await _reply_pcm(adapter, identity, fence, CRISIS_SELF_SCRIPT)

    assert inner.streams == 0
    assert adapter.fixed_safety_heard_complete(fence, total - 1) is False
    assert adapter.fixed_safety_heard_complete(fence, total) is True
    base = {
        "announced_text": CRISIS_SELF_SCRIPT.display,
        "minor_user_id": "minor-1",
        "session_id": identity.session_id,
        "turn_id": fence.turn_id,
        "generation_id": fence.generation_id,
        "tool_epoch": fence.tool_epoch,
    }
    assert (
        crisis_self_interrupt_event_id(
            **base,
            crisis_reply_heard=bool(adapter.fixed_safety_heard_complete(fence, total - 1)),
        )
        is not None
    )
    assert (
        crisis_self_interrupt_event_id(
            **base,
            crisis_reply_heard=bool(adapter.fixed_safety_heard_complete(fence, total)),
        )
        is None
    )


class _HeldCrisisProvider(_ScriptedProvider):
    """A crisis reply that holds the floor until released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.safety_fences: set[GenerationFence] = set()
        source = _load().get("crisis-self-v1")
        assert source is not None and source.pcm
        self.prerecorded = source.pcm[:960]

    def generate_reply(
        self,
        _identity: SessionIdentity,
        _user_text: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.reply_calls += 1
        self.safety_fences.add(fence)

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=self.prerecorded,
                source_start_sample=0,
                text=CRISIS_SELF_SCRIPT.display,
                assistant_text_delta=CRISIS_SELF_SCRIPT.display,
                first=True,
                final=False,
            )
            await self.release.wait()
            yield MediaReplyChunk(
                pcm_s16le=self.prerecorded,
                source_start_sample=480,
                final=True,
            )

        return chunks()

    def is_fixed_safety_reply(self, fence: GenerationFence) -> bool:
        return fence in self.safety_fences


def _device_identity(session_id: str) -> SessionIdentity:
    return SessionIdentity(
        session_id,
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
        audio_mode="interrupt_assist",
    )


def _cancel_effects(harness: _Harness) -> list[Any]:
    return [
        message.realtime_effect
        for message in harness.outputs
        if message.WhichOneof("event") == "realtime_effect"
        and message.realtime_effect.effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
    ]


@pytest.mark.asyncio
async def test_crisis_prerecorded_turn_not_interruptible_by_head_pat_or_screen_tap() -> None:
    identity = _device_identity("crisis-gesture")
    provider = _HeldCrisisProvider()
    harness = _Harness(identity, provider)
    await harness.start()
    try:
        await harness.utterance("给我讲个故事", frames=25)
        await harness.wait_for(lambda: harness.count("audio") >= 1)
        assert harness.runtime.floor.fixed_safety_audible is True
        frame = harness.last("audio")
        for sequence, reason in enumerate(("device_pat", "user_tap"), start=1):
            stop = MediaEnvelope.create(
                type="client.stop_assistant",
                event_id=f"gesture-{sequence}",
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                sequence=sequence,
                turn_id=frame.turn_id,
                generation_id=frame.generation_id,
                tool_epoch=frame.tool_epoch,
                payload={
                    "idempotency_key": f"gesture-{sequence}",
                    "reason": reason,
                },
            )
            await harness.send(
                media_pb2.MediaToCore(
                    device=media_pb2.DeviceEvent(
                        identity=harness.wire_identity,
                        event_type="client.stop_assistant",
                        json_payload=stop.encode(),
                        monotonic_ms=1,
                    )
                )
            )
        await harness.settle()
        assert _cancel_effects(harness) == []
        assert harness.runtime.floor.fixed_safety_audible is True
    finally:
        provider.release.set()
        await harness.close()


def test_fixed_safety_wav_implicit_aigc_label_metadata_present_and_pcm_unchanged() -> None:
    manifest = json.loads((_AUDIO_DIR / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["templates"]:
        raw = (_AUDIO_DIR / entry["file"]).read_bytes()
        parsed = parse_wav_bytes(raw)
        assert parsed.info["IART"] == FIXED_SAFETY_AUDIO_ARTIST
        assert parsed.info["ISFT"] == FIXED_SAFETY_AUDIO_SOFTWARE
        assert parsed.info["ICMT"] == entry["content_id"]
        assert parsed.info["ICRD"] == "2026-10-21"
        assert hashlib.sha256(parsed.pcm).hexdigest() == entry["pcm_sha256"]
        assert hashlib.sha256(raw).hexdigest() == entry["audio_sha256"]


def test_fixed_safety_manifest_spoken_hash_matches_main_scripts() -> None:
    manifest = json.loads((_AUDIO_DIR / "manifest.json").read_text(encoding="utf-8"))
    entries = {entry["template_id"]: entry for entry in manifest["templates"]}
    assert set(entries) == set(_TEMPLATE_IDS)
    for template in approved_templates():
        assert entries[template.template_id]["spoken_sha256"] == spoken_sha256(template.spoken)
    assert entries["daily-limit"]["spoken_sha256"] == spoken_sha256(MINOR_DAILY_LIMIT_PHRASE)
