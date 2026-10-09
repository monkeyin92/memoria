"""Golden characterization of the media voice main chain.

Each scenario drives the real ``MediaVoiceCoreRegistry`` + ``DuplexRuntime``
through the public gRPC ``connect`` stream of ``MediaBridgeGrpcServer`` (no
network, no private seams) with a scripted ``FakeMediaProvider``.  The test
records a normalized trace and compares it with a JSON golden file under
``golden/``:

- ``core_to_media``: every ``CoreToMedia`` message the bridge would send.
- ``state_history``: ``ConversationState`` transitions of the state machine.
- ``interaction_phases``: interaction-phase changes with their cause.
- ``reply_delivery``: reply-delivery projection events.

Regenerate after an intentional behavior change with::

    MEMORIA_UPDATE_GOLDEN=1 uv run pytest --no-cov -q \
        services/agent/tests/unit/test_media_session_golden.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.json_format import MessageToDict
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    MediaEnvelope,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaSessionResources,
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.speech_timeline import ASRResult
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _bind_verified_owner_classifier,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy

GOLDEN_DIR = Path(__file__).parent / "golden"
UPDATE_ENV = "MEMORIA_UPDATE_GOLDEN"
_REPLY_PCM = b"\x02\x00\x03\x00" * 80  # 160 samples


class _StreamContext:
    """The only ``grpc.aio.ServicerContext`` method ``connect`` uses."""

    async def abort(self, code: object, details: str) -> None:
        raise AssertionError(f"media bridge aborted the stream: {code} {details}")


class _ScriptedProvider(FakeMediaProvider):
    """``FakeMediaProvider`` with scripted ASR finals and reply text.

    The harness registers each utterance's final before sending its audio; the
    final is returned on the utterance's last frame and covers all of it.
    """

    def __init__(self, replies: Sequence[str] = ("你好。",)) -> None:
        super().__init__()
        self.finals: dict[int, tuple[str, int]] = {}
        self.replies = list(replies)
        self.reply_calls = 0

    async def ingest_audio(
        self,
        _identity: SessionIdentity,
        frame: AudioFrame,
    ) -> Sequence[ASRResult]:
        self.audio_calls.append(frame.sequence)
        scripted = self.finals.pop(frame.sequence, None)
        if scripted is None:
            return ()
        text, start_sample = scripted
        return (
            ASRResult(
                task_epoch=1,
                sentence_id=f"sentence-{frame.sequence}",
                revision=1,
                capture_start_sample=start_sample,
                capture_end_sample=frame.capture_end_sample,
                text=text,
                is_final=True,
                confidence=0.99,
                stream_epoch=frame.identity.stream_epoch,
            ),
        )

    def generate_reply(
        self,
        _identity: SessionIdentity,
        _user_text: str,
        _fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        text = self.replies[min(self.reply_calls, len(self.replies) - 1)]
        self.reply_calls += 1

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=_REPLY_PCM,
                source_start_sample=0,
                text=text,
                first=True,
                final=True,
            )

        return chunks()


class _PhaseLog(logging.Handler):
    _PATTERN = re.compile(r"^interaction_phase from=(\S+) to=(\S+) cause=(\S+) session_id=(\S+)")

    def __init__(self, session_id: str) -> None:
        super().__init__(level=logging.INFO)
        self.session_id = session_id
        self.phases: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        match = self._PATTERN.match(record.getMessage())
        if match is None or match.group(4) != self.session_id:
            return
        self.phases.append({"from": match.group(1), "to": match.group(2), "cause": match.group(3)})


def _proto_identity(identity: SessionIdentity) -> Any:
    return media_pb2.SessionIdentity(
        session_id=identity.session_id,
        account_id=identity.account_id,
        participant_id=identity.participant_id,
        device_id=identity.device_id,
        client_type=identity.client_type,
        stream_epoch=identity.stream_epoch,
        subject_id=identity.subject_id,
        binding_id=identity.binding_id,
        binding_version=identity.binding_version,
        runtime_profile_version=identity.runtime_profile_version,
    )


class _Harness:
    """One wire-level media session over the real bridge + registry."""

    def __init__(
        self,
        identity: SessionIdentity,
        provider: _ScriptedProvider,
        *,
        verified_owner: bool = False,
        **registry_options: Any,
    ) -> None:
        self.identity = identity
        self.wire_identity = _proto_identity(identity)
        self.provider = provider
        self.runtime = DuplexRuntime.create(session_id=identity.session_id)
        if verified_owner:
            bind_owner_policy(self.runtime)
            _bind_verified_owner_classifier(self.runtime)
        self.bridge = MediaBridgeGrpcServer()
        self.deliveries: list[dict[str, Any]] = []
        self.registry = MediaVoiceCoreRegistry(
            bridge=self.bridge,
            session_factory=lambda _identity: MediaSessionResources(self.runtime, provider),
            turn_endpoint_grace_s=0.01,
            reply_delivery_publisher=self._record_delivery,
            **registry_options,
        )
        self.registry.install()
        self.outputs: list[Any] = []
        self._requests: asyncio.Queue[Any] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._audio_sequence = 0
        self.sample = 0
        self._phase_log = _PhaseLog(identity.session_id)
        self._logger = logging.getLogger("services.agent")
        self._previous_level = self._logger.level

    @property
    def phases(self) -> list[dict[str, str]]:
        return list(self._phase_log.phases)

    def _record_delivery(self, payload: dict[str, Any]) -> bool:
        self.deliveries.append(payload)
        return True

    async def _request_stream(self) -> AsyncIterator[Any]:
        while True:
            message = await self._requests.get()
            if message is None:
                return
            yield message

    async def _read(self) -> None:
        async for message in self.bridge.connect(self._request_stream(), _StreamContext()):  # type: ignore[arg-type]
            self.outputs.append(message)

    async def start(self) -> None:
        self._logger.addHandler(self._phase_log)
        self._logger.setLevel(logging.INFO)
        self._reader = asyncio.create_task(self._read(), name="golden-reader")
        await self.send(
            media_pb2.MediaToCore(
                hello=media_pb2.SessionHello(
                    identity=self.wire_identity,
                    interaction_authority=media_pb2.INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE,
                )
            )
        )
        await self.wait_for(lambda: self.count("accepted") == 1)

    async def close(self) -> None:
        try:
            await self.registry.finalize_session(self.identity.session_id)
            await self._requests.put(None)
            if self._reader is not None:
                await asyncio.wait_for(self._reader, timeout=2)
            await self.bridge.stop(grace_s=0)
        finally:
            self._logger.removeHandler(self._phase_log)
            self._logger.setLevel(self._previous_level)

    async def send(self, message: Any) -> None:
        await self._requests.put(message)
        await self.settle()

    async def settle(self, *, quiet_ticks: int = 5, tick_s: float = 0.01) -> None:
        """Wait until no recorded channel changes for ``quiet_ticks`` ticks."""

        previous = None
        quiet = 0
        for _ in range(400):
            await asyncio.sleep(tick_s)
            current = self._progress()
            if current == previous and self._requests.empty():
                quiet += 1
                if quiet >= quiet_ticks:
                    return
            else:
                quiet = 0
            previous = current
        raise AssertionError("media session did not settle")

    def _progress(self) -> tuple[int, ...]:
        return (
            len(self.outputs),
            len(self.runtime.orchestrator.state_machine.history),
            len(self._phase_log.phases),
            len(self.deliveries),
        )

    async def wait_for(self, predicate: Callable[[], bool], *, timeout: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() >= deadline:
                raise AssertionError("scenario condition not met before timeout")
            await asyncio.sleep(0.005)
        await self.settle()

    def count(self, kind: str, predicate: Callable[[Any], bool] | None = None) -> int:
        return sum(
            1
            for message in self.outputs
            if message.WhichOneof("event") == kind
            and (predicate is None or predicate(getattr(message, kind)))
        )

    def last(self, kind: str) -> Any:
        for message in reversed(self.outputs):
            if message.WhichOneof("event") == kind:
                return getattr(message, kind)
        raise AssertionError(f"no {kind} message was emitted")

    # -- wire inputs -------------------------------------------------------

    async def vad_start(self, sample: int) -> None:
        await self.send(
            media_pb2.MediaToCore(
                vad=media_pb2.VadEvent(
                    identity=self.wire_identity,
                    type=media_pb2.VAD_EVENT_SPEECH_START,
                    sample_position=sample,
                    probability=0.99,
                    rms=0.05,
                )
            )
        )

    async def vad_end(self, sample: int) -> None:
        await self.send(
            media_pb2.MediaToCore(
                vad=media_pb2.VadEvent(
                    identity=self.wire_identity,
                    type=media_pb2.VAD_EVENT_SPEECH_END,
                    sample_position=sample,
                    probability=0.99,
                    rms=0.05,
                    voiced_end_sample=sample,
                )
            )
        )

    async def audio_frames(self, count: int, *, final_text: str | None = None) -> None:
        """Send ``count`` contiguous 20 ms frames; ``final_text`` ends on the last."""

        start_sample = self.sample
        if final_text is not None:
            self.provider.finals[self._audio_sequence + count - 1] = (final_text, start_sample)
        for _ in range(count):
            await self._requests.put(
                media_pb2.MediaToCore(
                    audio=media_pb2.AudioFrame(
                        identity=self.wire_identity,
                        sequence=self._audio_sequence,
                        capture_start_sample=self.sample,
                        frame_samples=320,
                        payload=b"\x00\x10" * 320,
                    )
                )
            )
            self._audio_sequence += 1
            self.sample += 320
        await self.settle()

    async def utterance(self, text: str, *, frames: int = 1) -> None:
        """VAD start, ``frames`` audio frames carrying one ASR final, VAD end."""

        await self.vad_start(self.sample)
        await self.audio_frames(frames, final_text=text)
        await self.vad_end(self.sample)

    async def playback_ended(self) -> None:
        """Report the last emitted assistant frame as fully rendered."""

        await self._report_playback(media_pb2.PLAYBACK_EVENT_TYPE_ENDED)

    async def playback_progress(self) -> None:
        """Report the last emitted assistant frame as rendered: the child hears the reply from here on."""

        await self._report_playback(media_pb2.PLAYBACK_EVENT_TYPE_PROGRESS)

    async def _report_playback(self, event_type: Any) -> None:
        frame = self.last("audio")
        await self.send(
            media_pb2.MediaToCore(
                playback=media_pb2.PlaybackProgress(
                    identity=self.wire_identity,
                    generation_id=frame.generation_id,
                    received_sequence=frame.sequence,
                    rendered_sample_end=frame.source_start_sample + frame.frame_samples,
                    client_monotonic_ms=1,
                    turn_id=frame.turn_id,
                    tool_epoch=frame.tool_epoch,
                    session_epoch=frame.session_epoch,
                    event_type=event_type,
                )
            )
        )


_TIME_KEYS = frozenset({"at", "server_monotonic_ms", "expires_at_ms", "monotonic_ms"})
_ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class _Normalizer:
    """Replace wall-clock values and random ids with stable placeholders."""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}

    def _stable_id(self, match: re.Match[str]) -> str:
        return self._ids.setdefault(match.group(0), f"<uuid-{len(self._ids) + 1}>")

    def value(self, key: str | None, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self.value(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(None, v) for v in value]
        if key in _TIME_KEYS:
            return "<time>"
        if isinstance(value, str):
            if _ISO_TIME.match(value):
                return "<time>"
            return _UUID.sub(self._stable_id, value)
        return value

    def message(self, message: Any) -> dict[str, Any]:
        kind = message.WhichOneof("event")
        body = MessageToDict(getattr(message, kind), preserving_proto_field_name=True)
        body.pop("identity", None)
        raw = body.pop("json_payload", None)
        if raw is not None:
            envelope = json.loads(base64.b64decode(raw))
            body["json"] = envelope
        if kind == "audio" and "payload" in body:
            body["payload_bytes"] = len(base64.b64decode(body.pop("payload")))
        return {kind: self.value(None, body)}


def normalize_trace(harness: _Harness) -> dict[str, Any]:
    normalizer = _Normalizer()
    return {
        "core_to_media": [normalizer.message(m) for m in harness.outputs],
        "state_history": normalizer.value(
            None,
            [
                {
                    "from": record.from_state,
                    "to": record.to_state,
                    "event": record.event,
                    "cause": record.cause,
                    "turn_id": record.turn_id,
                    "generation_id": record.generation_id,
                    "tool_epoch": record.tool_epoch,
                }
                for record in harness.runtime.orchestrator.state_machine.history
            ],
        ),
        "interaction_phases": normalizer.value(None, harness.phases),
        "reply_delivery": normalizer.value(None, harness.deliveries),
    }


def _h5_identity(session_id: str) -> SessionIdentity:
    return SessionIdentity(
        session_id,
        account_id="account",
        participant_id="participant",
        device_id="h5",
    )


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
    )


def _is_action(action: int) -> Callable[[Any], bool]:
    return lambda generation: generation.action == action


async def _run(harness: _Harness, script: Callable[[_Harness], Any]) -> dict[str, Any]:
    await harness.start()
    try:
        await script(harness)
        return normalize_trace(harness)
    finally:
        await harness.close()


async def _speak_first_reply(harness: _Harness) -> None:
    """Turn 1: the user speaks, the provider reply is fully sent, not yet heard."""

    await harness.utterance("你好", frames=25)
    await harness.wait_for(
        lambda: harness.count("generation", _is_action(media_pb2.GENERATION_ACTION_COMPLETE)) >= 1
        and harness.count("audio") >= 1
    )


async def _scenario_normal_turn() -> dict[str, Any]:
    async def script(harness: _Harness) -> None:
        await _speak_first_reply(harness)
        await harness.playback_ended()

    return await _run(
        _Harness(_h5_identity("golden-normal-turn"), _ScriptedProvider()),
        script,
    )


async def _scenario_barge_in() -> dict[str, Any]:
    async def script(harness: _Harness) -> None:
        await _speak_first_reply(harness)
        # The verified owner talks over the still-playing reply.
        await harness.utterance("给我讲个故事吧", frames=25)
        await harness.wait_for(
            lambda: harness.count("generation", _is_action(media_pb2.GENERATION_ACTION_COMPLETE))
            >= 2
        )
        await harness.playback_ended()

    return await _run(
        _Harness(
            _h5_identity("golden-barge-in"),
            _ScriptedProvider(replies=("你好。", "从前有座山。")),
            verified_owner=True,
        ),
        script,
    )


async def _scenario_client_stop_assistant() -> dict[str, Any]:
    async def script(harness: _Harness) -> None:
        await _speak_first_reply(harness)
        started = harness.last("generation")
        stop = MediaEnvelope.create(
            type="client.stop_assistant",
            event_id="golden-stop-1",
            session_id=harness.identity.session_id,
            stream_epoch=harness.identity.stream_epoch,
            sequence=1,
            turn_id=started.turn_id,
            generation_id=started.generation_id,
            tool_epoch=started.tool_epoch,
            payload={"idempotency_key": "golden-stop-1", "reason": "user_tap"},
        )
        await harness.send(
            media_pb2.MediaToCore(
                device=media_pb2.DeviceEvent(
                    identity=harness.wire_identity,
                    event_type="client.stop_assistant",
                    json_payload=stop.encode(),
                )
            )
        )
        await harness.wait_for(
            lambda: harness.count("generation", _is_action(media_pb2.GENERATION_ACTION_CANCEL)) >= 1
        )

    return await _run(
        _Harness(_h5_identity("golden-client-stop"), _ScriptedProvider()),
        script,
    )


async def _scenario_kws_stop() -> dict[str, Any]:
    async def script(harness: _Harness) -> None:
        await _speak_first_reply(harness)
        await harness.send(
            media_pb2.MediaToCore(
                keyword=media_pb2.KeywordEvent(
                    identity=harness.wire_identity,
                    keyword="停一下",
                    confidence=0.95,
                    start_sample=harness.sample,
                    end_sample=harness.sample + 320,
                    hard_stop=True,
                )
            )
        )
        await harness.wait_for(
            lambda: harness.count(
                "realtime_effect",
                lambda effect: effect.effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
            )
            >= 1
        )

    return await _run(
        _Harness(_h5_identity("golden-kws-stop"), _ScriptedProvider()),
        script,
    )


class _DelegatingProvider(_ScriptedProvider):
    """Scripted provider with the fenced delegation/output-intent seams."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.output_kinds: list[int] = []

    async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
        await self.release.wait()
        return "南京今天多云，气温二十二度。"

    @staticmethod
    def accept_output_intent(intent: Any) -> Any:
        return intent

    def generate_output(
        self,
        _identity: SessionIdentity,
        intent: Any,
        _fence: GenerationFence,
        *,
        work_id: str,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        _ = work_id
        self.output_kinds.append(int(intent.kind))

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=_REPLY_PCM,
                source_start_sample=source_start_sample,
                text=str(intent.tts_source),
                first=True,
                final=True,
            )

        return chunks()


async def _scenario_delegation_filler() -> dict[str, Any]:
    provider = _DelegatingProvider()

    async def script(harness: _Harness) -> None:
        await harness.utterance("今天南京天气怎么样", frames=25)
        # The live-lookup filler (FAST_ACKNOWLEDGEMENT) plays while the
        # delegated lookup is still held open.
        await harness.wait_for(lambda: harness.count("audio") >= 1)
        await harness.playback_ended()
        provider.release.set()
        await harness.wait_for(lambda: harness.count("audio") >= 2)
        await harness.playback_ended()

    return await _run(_Harness(_h5_identity("golden-delegation"), provider), script)


async def _scenario_farewell_standby() -> dict[str, Any]:
    async def script(harness: _Harness) -> None:
        await harness.utterance("好的，再见", frames=25)
        await harness.wait_for(lambda: harness.count("state") >= 1)

    return await _run(
        _Harness(_device_identity("golden-farewell"), _ScriptedProvider()),
        script,
    )


async def _scenario_owner_silence_close() -> dict[str, Any]:
    async def script(harness: _Harness) -> None:
        await _speak_first_reply(harness)
        await harness.playback_ended()
        # Back in LISTENING the owner-silence window runs out.
        await harness.wait_for(lambda: harness.count("state") >= 1)

    return await _run(
        _Harness(
            _device_identity("golden-owner-silence"),
            _ScriptedProvider(),
            owner_silence_timeout_s=0.2,
        ),
        script,
    )


async def _scenario_session_epoch_rotation() -> dict[str, Any]:
    async def script(harness: _Harness) -> None:
        await _speak_first_reply(harness)
        # Authority loss rotates the identity epoch while the reply plays; the
        # next turn drains the old output before it may speak.
        harness.runtime.degrade_runtime_profile()
        await harness.settle()
        await harness.utterance("我们接着聊", frames=25)
        await harness.wait_for(
            lambda: harness.count("generation", _is_action(media_pb2.GENERATION_ACTION_COMPLETE))
            >= 2
        )
        await harness.playback_ended()

    return await _run(
        _Harness(
            _h5_identity("golden-epoch-rotation"),
            _ScriptedProvider(replies=("你好。", "好的。")),
            verified_owner=True,
        ),
        script,
    )


SCENARIOS: dict[str, Callable[[], Any]] = {
    "normal_turn": _scenario_normal_turn,
    "barge_in": _scenario_barge_in,
    "client_stop_assistant": _scenario_client_stop_assistant,
    "kws_stop": _scenario_kws_stop,
    "delegation_filler": _scenario_delegation_filler,
    "farewell_standby": _scenario_farewell_standby,
    "owner_silence_close": _scenario_owner_silence_close,
    "session_epoch_rotation": _scenario_session_epoch_rotation,
}


def _dump(trace: dict[str, Any]) -> str:
    return json.dumps(trace, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_media_session_matches_golden(name: str) -> None:
    actual = _dump(await SCENARIOS[name]())
    path = GOLDEN_DIR / f"media_session_{name}.json"
    if os.environ.get(UPDATE_ENV) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    assert path.exists(), f"missing golden {path.name}; run with {UPDATE_ENV}=1 to create it"
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{name} diverged from {path.name}; if the change is intended, "
        f"regenerate with {UPDATE_ENV}=1"
    )
