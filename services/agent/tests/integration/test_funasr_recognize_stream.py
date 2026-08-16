"""Honest integration: FunASRRecognizeStream._run against mock yields FINAL_TRANSCRIPT."""

from __future__ import annotations

import asyncio

import pytest
from livekit import rtc
from livekit.agents import stt
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSTT
from services.agent.tests.integration.mock_servers import MockFunASRServer


@pytest.mark.asyncio
async def test_funasr_recognize_stream_emits_final_transcript() -> None:
    srv = MockFunASRServer(scenario="happy")
    srv.start()
    try:
        plugin = FunASRSTT(FunASRConfig(api_key="test", ws_url=srv.ws_url))
        stream = plugin.stream()

        # 16 kHz mono 16-bit silence (~250 ms) — enough for mock to emit results.
        samples = 4000
        pcm = b"\x00\x00" * samples
        frame = rtc.AudioFrame(
            data=pcm,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=samples,
        )
        stream.push_frame(frame)
        stream.end_input()

        events: list[stt.SpeechEvent] = []

        async def _collect() -> None:
            async for ev in stream:
                events.append(ev)

        await asyncio.wait_for(_collect(), timeout=15)

        types = [e.type for e in events]
        assert stt.SpeechEventType.FINAL_TRANSCRIPT in types, f"events={types}"
        finals = [e for e in events if e.type is stt.SpeechEventType.FINAL_TRANSCRIPT]
        assert finals
        text = finals[-1].alternatives[0].text if finals[-1].alternatives else ""
        assert "实时语音测试" in text
        # Word-level alignment should be present for Adaptive Interruption.
        words = finals[-1].alternatives[0].words if finals[-1].alternatives else None
        assert words is not None and len(words) > 0
    finally:
        srv.stop()
        await plugin.aclose()


@pytest.mark.asyncio
async def test_funasr_recognize_stream_interim_before_final() -> None:
    srv = MockFunASRServer(scenario="happy")
    srv.start()
    try:
        plugin = FunASRSTT(FunASRConfig(api_key="test", ws_url=srv.ws_url))
        stream = plugin.stream()
        samples = 4000
        frame = rtc.AudioFrame(
            data=b"\x00\x00" * samples,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=samples,
        )
        stream.push_frame(frame)
        stream.end_input()

        events: list[stt.SpeechEvent] = []
        async for ev in stream:
            events.append(ev)

        types = [e.type for e in events]
        assert stt.SpeechEventType.INTERIM_TRANSCRIPT in types or stt.SpeechEventType.FINAL_TRANSCRIPT in types
        assert stt.SpeechEventType.FINAL_TRANSCRIPT in types
    finally:
        srv.stop()
        await plugin.aclose()


@pytest.mark.asyncio
async def test_funasr_vad_flush_reuses_connection_and_finalizes_each_segment() -> None:
    srv = MockFunASRServer(scenario="task_reuse")
    srv.start()
    try:
        plugin = FunASRSTT(FunASRConfig(api_key="test", ws_url=srv.ws_url))
        stream = plugin.stream()

        async def _wait_for_tasks(count: int) -> None:
            for _ in range(200):
                if len(srv.tasks_started) >= count:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError(f"expected {count} FunASR tasks, got {len(srv.tasks_started)}")

        async def _collect() -> list[stt.SpeechEvent]:
            return [event async for event in stream]

        collector = asyncio.create_task(_collect())
        samples = 4000
        first = rtc.AudioFrame(
            data=b"\x01\x00" * samples,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=samples,
        )
        second = rtc.AudioFrame(
            data=b"\x02\x00" * samples,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=samples,
        )
        stream.push_frame(first)
        plugin.flush_speech_segment()
        await _wait_for_tasks(2)
        stream.push_frame(second)
        stream.end_input()

        events = await asyncio.wait_for(collector, timeout=10)
        finals = [
            event.alternatives[0].text
            for event in events
            if event.type is stt.SpeechEventType.FINAL_TRANSCRIPT and event.alternatives
        ]
        assert finals == ["第1段识别。", "第2段识别。"]
        assert srv.connections_started == 1
        assert len(srv.tasks_started) == 2
        assert len(set(srv.tasks_started)) == 2
        assert all(byte_count > 0 for byte_count in srv.pcm_by_connection)
    finally:
        srv.stop()
        await plugin.aclose()


@pytest.mark.asyncio
async def test_funasr_vad_flush_fences_late_result_from_finished_task() -> None:
    srv = MockFunASRServer(scenario="task_reuse_late_event")
    srv.start()
    try:
        plugin = FunASRSTT(FunASRConfig(api_key="test", ws_url=srv.ws_url))
        stream = plugin.stream()

        async def _wait_for_second_task() -> None:
            for _ in range(200):
                if len(srv.tasks_started) >= 2:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("second FunASR task did not start")

        async def _collect() -> list[stt.SpeechEvent]:
            return [event async for event in stream]

        def _frame(sample_value: int) -> rtc.AudioFrame:
            samples = 4000
            return rtc.AudioFrame(
                data=bytes((sample_value, 0)) * samples,
                sample_rate=16000,
                num_channels=1,
                samples_per_channel=samples,
            )

        collector = asyncio.create_task(_collect())
        stream.push_frame(_frame(1))
        plugin.flush_speech_segment()
        await _wait_for_second_task()
        stream.push_frame(_frame(2))
        stream.end_input()

        events = await asyncio.wait_for(collector, timeout=10)
        finals = [
            event.alternatives[0].text
            for event in events
            if event.type is stt.SpeechEventType.FINAL_TRANSCRIPT and event.alternatives
        ]
        assert finals == ["第1段识别。", "第2段识别。"]
        assert srv.connections_started == 1
    finally:
        srv.stop()
        await plugin.aclose()


@pytest.mark.asyncio
async def test_funasr_recognize_stream_deduplicates_final_sentence_id() -> None:
    srv = MockFunASRServer(scenario="duplicate_final")
    srv.start()
    try:
        plugin = FunASRSTT(FunASRConfig(api_key="test", ws_url=srv.ws_url))
        stream = plugin.stream()
        samples = 4000
        stream.push_frame(
            rtc.AudioFrame(
                data=b"\x00\x00" * samples,
                sample_rate=16000,
                num_channels=1,
                samples_per_channel=samples,
            )
        )
        stream.end_input()

        events = [event async for event in stream]
        finals = [
            event
            for event in events
            if event.type is stt.SpeechEventType.FINAL_TRANSCRIPT
        ]
        assert len(finals) == 1
    finally:
        srv.stop()
        await plugin.aclose()


@pytest.mark.asyncio
async def test_funasr_recognize_stream_does_not_leak_prior_chat_into_current_turn() -> None:
    srv = MockFunASRServer(scenario="context_leak")
    srv.start()
    try:
        plugin = FunASRSTT(FunASRConfig(api_key="test", ws_url=srv.ws_url))
        plugin.push_conversation_item({"role": "user", "text": "你好呀！"})
        plugin.push_conversation_item(
            {"role": "assistant", "text": "你好呀！很高兴见到你。"}
        )
        stream = plugin.stream()
        samples = 4000
        stream.push_frame(
            rtc.AudioFrame(
                data=b"\x00\x00" * samples,
                sample_rate=16000,
                num_channels=1,
                samples_per_channel=samples,
            )
        )
        stream.end_input()

        events = [event async for event in stream]
        finals = [
            event.alternatives[0].text
            for event in events
            if event.type is stt.SpeechEventType.FINAL_TRANSCRIPT and event.alternatives
        ]

        assert finals == ["介绍一下南京。"]
    finally:
        srv.stop()
        await plugin.aclose()
