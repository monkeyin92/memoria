"""Continuous uplink audio must not commit intermediate ASR finals before speech ends."""

from __future__ import annotations

import asyncio

import pytest
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.tests.unit.media_session_support import _user_turn_texts
from services.agent.tests.unit.test_media_session_playback_stop import (
    _device_identity,
    _run_story,
    _StoryHarness,
)

_SILENT_PCM = b"\x00\x00" * 320


async def _silent_frames(harness: _StoryHarness, count: int) -> None:
    for _ in range(count):
        await harness._requests.put(
            media_pb2.MediaToCore(
                audio=media_pb2.AudioFrame(
                    identity=harness.wire_identity,
                    sequence=harness._audio_sequence,
                    capture_start_sample=harness.sample,
                    frame_samples=320,
                    payload=_SILENT_PCM,
                )
            )
        )
        harness._audio_sequence += 1
        harness.sample += 320
    await harness.settle()


@pytest.mark.asyncio
async def test_continuous_speech_frames_inside_open_vad_do_not_commit_first_clause_early() -> None:
    async def script(harness: _StoryHarness) -> None:
        await harness.utterance("你好", frames=25)
        await harness.wait_for(lambda: harness.count("audio") >= 1)
        await harness.audio_frames(60)
        await harness.playback_ended()
        await _silent_frames(harness, 60)

        await harness.vad_start(harness.sample)
        await harness.audio_frames(25, final_text="前半句，")

        for _ in range(40):
            await harness.audio_frames(1)
            await asyncio.sleep(0.02)
            assert _user_turn_texts(harness.context) == ["你好"]

        await harness.audio_frames(25, final_text="后半句。")
        await harness.vad_end(harness.sample)
        await harness.wait_for(lambda: harness.story.reply_calls >= 2, timeout=5.0)
        assert _user_turn_texts(harness.context) == ["你好", "前半句， 后半句。"]

    await _run_story(_device_identity("continuous-speech-open-vad"), script, story_call=0)


@pytest.mark.asyncio
async def test_comma_silence_shorter_than_firmware_vad_hangover_merges_clauses() -> None:
    async def script(harness: _StoryHarness) -> None:
        await harness.utterance("你好", frames=25)
        await harness.wait_for(lambda: harness.count("audio") >= 1)
        await harness.audio_frames(60)
        await harness.playback_ended()
        await _silent_frames(harness, 60)

        await harness.vad_start(harness.sample)
        await harness.audio_frames(25, final_text="我又回来啦，")

        for _ in range(12):
            await _silent_frames(harness, 1)
            await asyncio.sleep(0.02)
            assert _user_turn_texts(harness.context) == ["你好"]

        for _ in range(20):
            await harness.audio_frames(1)
            await asyncio.sleep(0.02)
            assert _user_turn_texts(harness.context) == ["你好"]

        await harness.audio_frames(5, final_text="你还记得我今天画的什么吗？")
        await harness.vad_end(harness.sample)
        await harness.wait_for(lambda: harness.story.reply_calls >= 2, timeout=5.0)
        assert _user_turn_texts(harness.context) == [
            "你好",
            "我又回来啦， 你还记得我今天画的什么吗？",
        ]

    await _run_story(_device_identity("comma-silence-open-vad"), script, story_call=0)
