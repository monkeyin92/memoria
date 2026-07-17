from __future__ import annotations

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
    assert sidecar._queue.get_nowait() == b"middle"
    assert sidecar._queue.get_nowait() == b"newest"


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

    class Events:
        def __aiter__(self) -> Events:
            self._events = iter(
                (
                    '{"type":"input_audio_buffer.speech_started"}',
                    '{"type":"conversation.item.input_audio_transcription.completed",'
                    '"emotion":"sad","transcript":"有点累"}',
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
