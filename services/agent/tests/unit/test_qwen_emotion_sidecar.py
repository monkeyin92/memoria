from __future__ import annotations

import asyncio

import pytest
from services.agent.src.providers.qwen_emotion_asr import (
    QwenEmotionConfig,
    QwenEmotionSidecar,
    parse_emotion_event,
)


def test_qwen_emotion_config_uses_a_separate_realtime_endpoint() -> None:
    config = QwenEmotionConfig.from_env(
        {
            "DASHSCOPE_API_KEY": "server-only",
            "DASHSCOPE_WS_URL": "wss://workspace.example/api-ws/v1/inference",
        }
    )

    assert config.ws_url == (
        "wss://workspace.example/api-ws/v1/realtime?model=qwen3-asr-flash-realtime"
    )


def test_qwen_emotion_parser_keeps_missing_confidence_as_none() -> None:
    result = parse_emotion_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "emotion": "sad",
            "transcript": "我今天有点累",
        }
    )

    assert result is not None
    assert (result.label, result.text, result.turn_id, result.provider_confidence) == (
        "sad",
        "我今天有点累",
        None,
        None,
    )
    assert parse_emotion_event({"type": "error", "emotion": "happy"}) is None


def test_sidecar_drops_old_audio_instead_of_blocking_the_main_asr() -> None:
    sidecar = QwenEmotionSidecar(
        QwenEmotionConfig(api_key="key", ws_url="wss://example", queue_chunks=2),
        on_observation=lambda _result: None,
    )

    sidecar.feed_pcm(b"oldest")
    sidecar.feed_pcm(b"middle")
    sidecar.feed_pcm(b"newest")

    assert sidecar.dropped_chunks == 1
    assert sidecar._queue.get_nowait() == (None, b"middle")
    assert sidecar._queue.get_nowait() == (None, b"newest")


def test_emotion_result_keeps_the_main_turn_captured_at_provider_speech_start() -> None:
    result = parse_emotion_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "emotion": "happy",
            "transcript": "今天不错",
        },
        turn_id=7,
    )

    assert result is not None
    assert result.turn_id == 7


@pytest.mark.asyncio
async def test_sidecar_binds_completed_emotion_to_turn_at_provider_speech_start() -> None:
    observed = []
    sidecar = QwenEmotionSidecar(
        QwenEmotionConfig(api_key="key", ws_url="wss://example"),
        on_observation=observed.append,
    )
    sidecar.start_turn(7)
    sidecar._record_sent_audio(7, b"\x00\x00" * 1_600)

    class Events:
        def __aiter__(self) -> Events:
            self._events = iter(
                (
                    '{"type":"input_audio_buffer.speech_started",'
                    '"audio_start_ms":10,"item_id":"item-7"}',
                    '{"type":"conversation.item.input_audio_transcription.completed",'
                    '"item_id":"item-7","emotion":"sad","transcript":"有点累"}',
                )
            )
            return self

        async def __anext__(self) -> str:
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    await sidecar._receive_loop(Events())

    assert len(observed) == 1
    assert observed[0].turn_id == 7


@pytest.mark.asyncio
async def test_delayed_provider_start_uses_the_pcm_turn_epoch_not_the_latest_turn() -> None:
    observed = []
    sidecar = QwenEmotionSidecar(
        QwenEmotionConfig(api_key="key", ws_url="wss://example"),
        on_observation=observed.append,
    )

    class Sender:
        sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    sender = Sender()
    sidecar.start_turn(1)
    sidecar.feed_pcm(b"\x00\x00" * 16_000)
    sidecar.start_turn(2)
    sidecar.feed_pcm(b"\x01\x00" * 16_000)
    send_task = asyncio.create_task(sidecar._send_loop(sender))
    while len(sender.sent) < 2:
        await asyncio.sleep(0)
    send_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send_task

    class Events:
        def __aiter__(self) -> Events:
            self._events = iter(
                (
                    '{"type":"input_audio_buffer.speech_started",'
                    '"audio_start_ms":100,"item_id":"item-turn-one"}',
                    '{"type":"conversation.item.input_audio_transcription.completed",'
                    '"item_id":"item-turn-one","emotion":"sad",'
                    '"transcript":"第一轮迟到结果"}',
                )
            )
            return self

        async def __anext__(self) -> str:
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    await sidecar._receive_loop(Events())

    assert [result.turn_id for result in observed] == [1]


@pytest.mark.asyncio
async def test_multiple_provider_segments_inside_one_main_turn_keep_the_pcm_epoch() -> None:
    observed = []
    sidecar = QwenEmotionSidecar(
        QwenEmotionConfig(api_key="key", ws_url="wss://example"),
        on_observation=observed.append,
    )
    sidecar._record_sent_audio(1, b"\x00\x00" * 16_000)
    sidecar._record_sent_audio(2, b"\x01\x00" * 16_000)

    class Events:
        def __aiter__(self) -> Events:
            self._events = iter(
                (
                    '{"type":"input_audio_buffer.speech_started",'
                    '"audio_start_ms":100,"item_id":"segment-1"}',
                    '{"type":"conversation.item.input_audio_transcription.completed",'
                    '"item_id":"segment-1","emotion":"sad","transcript":"第一段"}',
                    '{"type":"input_audio_buffer.speech_started",'
                    '"audio_start_ms":500,"item_id":"segment-2"}',
                    '{"type":"conversation.item.input_audio_transcription.completed",'
                    '"item_id":"segment-2","emotion":"sad",'
                    '"transcript":"同一主话轮第二段"}',
                )
            )
            return self

        async def __anext__(self) -> str:
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    await sidecar._receive_loop(Events())

    assert [(result.turn_id, result.text) for result in observed] == [
        (1, "第一段"),
        (1, "同一主话轮第二段"),
    ]
