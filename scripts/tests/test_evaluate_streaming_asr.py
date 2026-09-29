"""Offline tests for the streaming ASR A/B harness.

Every provider client runs against a fake WebSocket server on 127.0.0.1, so
the wire framing (DashScope task JSON + binary PCM, Qwen-ASR-Realtime events,
Doubao binary header + gzip) is checked without any real network.  The
segmenter, aligner, CER and report privacy are tested as pure functions.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import json
import struct
import time
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scripts import evaluate_streaming_asr as asr
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

PCM = (np.arange(16_000 // 2, dtype=np.int16) % 200 - 100).astype("<i2").tobytes()  # 0.5 s
FAST = {"pace": 0.0, "connect_timeout_s": 5.0, "result_timeout_s": 5.0}


@contextlib.asynccontextmanager
async def fake_server(
    handler: Callable[[ServerConnection], Awaitable[None]],
    process_response: Callable[[ServerConnection, Request, Response], Response | None] | None = None,
) -> AsyncIterator[str]:
    async with serve(handler, "127.0.0.1", 0, process_response=process_response) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


def spec(protocol: asr.WireProtocol, url: str, **options: Any) -> asr.EngineSpec:
    headers = (("Authorization", "Bearer test-key"),)
    if protocol == "dashscope-realtime":
        headers = (*headers, ("OpenAI-Beta", "realtime=v1"))
    if protocol == "doubao-sauc":
        headers = (
            ("X-Api-App-Key", "app"),
            ("X-Api-Access-Key", "token"),
            ("X-Api-Resource-Id", "volc.bigasr.sauc.duration"),
            ("X-Api-Connect-Id", "c1"),
        )
    model = options.pop("model", "fun-asr-realtime")
    return asr.EngineSpec("e", protocol, model, url, 100, headers, options)


# --------------------------------------------------------------------------- DashScope task protocol


async def test_dashscope_task_protocol_sequence_and_multi_sentence_text() -> None:
    seen: dict[str, Any] = {"audio_bytes": 0}

    async def handler(ws: ServerConnection) -> None:
        assert ws.request is not None
        seen["auth"] = ws.request.headers["Authorization"]
        run = json.loads(await ws.recv())
        seen["run"] = run
        task_id = run["header"]["task_id"]
        await ws.send(json.dumps({"header": {"task_id": task_id, "event": "task-started", "attributes": {}}, "payload": {}}))
        async for message in ws:
            if isinstance(message, bytes):
                seen["audio_bytes"] += len(message)
                continue
            finish = json.loads(message)
            seen["finish"] = finish
            break

        def result(sid: int, text: str, end: bool, heartbeat: bool = False) -> str:
            sentence = {"sentence_id": sid, "text": text, "sentence_end": end, "heartbeat": heartbeat,
                        "begin_time": 0, "end_time": None, "words": []}
            return json.dumps({"header": {"task_id": task_id, "event": "result-generated"},
                               "payload": {"output": {"sentence": sentence}}})

        await ws.send(result(0, "", False, heartbeat=True))
        await ws.send(result(1, "", False))
        await ws.send(result(1, "今天天气", False))
        await ws.send(result(1, "今天天气怎么样？", True))
        await ws.send(result(2, "现在几点了？", True))
        await ws.send(json.dumps({"header": {"task_id": task_id, "event": "task-finished"}, "payload": {}}))

    async with fake_server(handler) as url:
        res = await asr.run_dashscope_task(
            spec("dashscope-task", url, model="qwen-audio-3.1-asr-flash-streaming",
                 language_hints=["zh"], max_sentence_silence=550, vad_model="near_meeting_16k"),
            PCM, **FAST,
        )

    assert res.error is None and not res.timed_out
    assert res.text == "今天天气怎么样？现在几点了？"
    assert res.first_partial_s is not None and res.final_latency_s is not None
    assert res.finish_latency_s is not None and res.finish_latency_s >= res.final_latency_s
    assert seen["auth"] == "Bearer test-key"
    assert seen["audio_bytes"] == len(PCM)
    header, payload = seen["run"]["header"], seen["run"]["payload"]
    assert header == {"action": "run-task", "task_id": header["task_id"], "streaming": "duplex"}
    assert (payload["task_group"], payload["task"], payload["function"]) == ("audio", "asr", "recognition")
    assert payload["model"] == "qwen-audio-3.1-asr-flash-streaming"
    assert payload["parameters"] == {
        "format": "pcm", "sample_rate": 16_000, "language_hints": ["zh"],
        "max_sentence_silence": 550, "vad_model": "near_meeting_16k",
    }
    assert seen["finish"]["header"] == {"action": "finish-task", "task_id": header["task_id"], "streaming": "duplex"}


async def test_dashscope_task_failed_before_start_is_reported() -> None:
    async def handler(ws: ServerConnection) -> None:
        run = json.loads(await ws.recv())
        await ws.send(json.dumps({"header": {"task_id": run["header"]["task_id"], "event": "task-failed",
                                             "error_code": "InvalidParameter", "error_message": "bad model"},
                                  "payload": {}}))

    async with fake_server(handler) as url:
        res = await asr.run_dashscope_task(spec("dashscope-task", url), PCM, **FAST)

    assert res.error is not None and "InvalidParameter" in res.error
    assert res.text == ""


async def test_dashscope_empty_audio_is_an_empty_result_not_an_error() -> None:
    async def handler(ws: ServerConnection) -> None:
        run = json.loads(await ws.recv())
        task_id = run["header"]["task_id"]
        await ws.send(json.dumps({"header": {"task_id": task_id, "event": "task-started"}, "payload": {}}))
        async for message in ws:
            if isinstance(message, str):
                break
        await ws.send(json.dumps({"header": {"task_id": task_id, "event": "task-failed",
                                             "error_code": "EmptyAudio", "error_message": "no speech"},
                                  "payload": {}}))

    async with fake_server(handler) as url:
        res = await asr.run_dashscope_task(spec("dashscope-task", url), PCM, **FAST)

    assert res.error is None and res.provider_empty and res.text == ""


async def test_dashscope_task_timeout_keeps_partial_and_flags_it() -> None:
    async def handler(ws: ServerConnection) -> None:
        run = json.loads(await ws.recv())
        task_id = run["header"]["task_id"]
        await ws.send(json.dumps({"header": {"task_id": task_id, "event": "task-started"}, "payload": {}}))
        sentence = {"sentence_id": 1, "text": "我有点", "sentence_end": False}
        await ws.send(json.dumps({"header": {"task_id": task_id, "event": "result-generated"},
                                  "payload": {"output": {"sentence": sentence}}}))
        async for _ in ws:  # never finishes
            pass

    async with fake_server(handler) as url:
        res = await asr.run_dashscope_task(
            spec("dashscope-task", url), PCM, pace=0.0, connect_timeout_s=5.0, result_timeout_s=0.3
        )

    assert res.timed_out and res.error == "timeout"
    assert res.text == "我有点"


# --------------------------------------------------------------------------- Qwen-ASR-Realtime


@pytest.mark.parametrize("server_vad", [False, True])
async def test_realtime_event_sequence(server_vad: bool) -> None:
    seen: dict[str, Any] = {"audio": b"", "types": []}

    async def handler(ws: ServerConnection) -> None:
        assert ws.request is not None
        seen["beta"] = ws.request.headers["OpenAI-Beta"]
        await ws.send(json.dumps({"type": "session.created", "session": {"id": "s"}}))
        update = json.loads(await ws.recv())
        seen["update"] = update
        await ws.send(json.dumps({"type": "session.updated", "session": update["session"]}))
        async for message in ws:
            event = json.loads(message)
            seen["types"].append(event["type"])
            assert event["event_id"]
            if event["type"] == "input_audio_buffer.append":
                seen["audio"] += base64.b64decode(event["audio"])
            elif event["type"] == "session.finish":
                break
        item = {"item_id": "item_1", "content_index": 0}
        await ws.send(json.dumps({**item, "type": "conversation.item.input_audio_transcription.text",
                                  "text": "", "stash": "晚"}))
        await ws.send(json.dumps({**item, "type": "conversation.item.input_audio_transcription.text",
                                  "text": "晚安", "stash": "。"}))
        await ws.send(json.dumps({**item, "type": "conversation.item.input_audio_transcription.completed",
                                  "transcript": "晚安。"}))
        await ws.send(json.dumps({"type": "session.finished"}))

    options: dict[str, Any] = {"language": "zh", "model": "qwen3-asr-flash-realtime"}
    if server_vad:
        options.update(server_vad=True, vad_threshold=0.0, silence_duration_ms=400)
    async with fake_server(handler) as url:
        res = await asr.run_dashscope_realtime(spec("dashscope-realtime", url, **options), PCM, **FAST)

    assert res.error is None, res.error
    assert res.text == "晚安。"
    assert res.first_partial_s is not None and res.final_latency_s is not None
    assert seen["beta"] == "realtime=v1"
    assert seen["audio"] == PCM
    session = seen["update"]["session"]
    assert seen["update"]["type"] == "session.update"
    assert session["input_audio_format"] == "pcm" and session["sample_rate"] == 16_000
    assert session["input_audio_transcription"] == {"language": "zh"}
    tail = [t for t in seen["types"] if t != "input_audio_buffer.append"]
    if server_vad:
        assert session["turn_detection"] == {"type": "server_vad", "threshold": 0.0, "silence_duration_ms": 400}
        assert tail == ["session.finish"]
    else:
        assert session["turn_detection"] is None
        assert tail == ["input_audio_buffer.commit", "session.finish"]


async def test_realtime_error_event_is_reported() -> None:
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                              "code": "invalid_value", "message": "nope"}}))

    async with fake_server(handler) as url:
        res = await asr.run_dashscope_realtime(spec("dashscope-realtime", url), PCM, **FAST)

    assert res.error is not None and res.error.startswith("invalid_value")


# --------------------------------------------------------------------------- Doubao binary protocol


def server_frame(payload: dict[str, Any], *, sequence: int, last: bool = False) -> bytes:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode())
    flags = asr.FLAG_LAST_NEGATIVE_SEQUENCE if last else asr.FLAG_POSITIVE_SEQUENCE
    header = asr.doubao_header(asr.MSG_FULL_SERVER_RESPONSE, flags, asr.SERIAL_JSON, asr.COMPRESS_GZIP)
    return header + struct.pack(">i", sequence) + struct.pack(">I", len(body)) + body


def parse_client_frame(data: bytes) -> tuple[int, int, int, int, bytes]:
    assert data[0] == 0x11  # version 1, header size 1 x 4 bytes
    assert data[3] == 0x00
    (size,) = struct.unpack(">I", data[4:8])
    assert len(data) == 8 + size  # no sequence number: flags 0b0000 / 0b0010
    return data[1] >> 4, data[1] & 0x0F, data[2] >> 4, data[2] & 0x0F, gzip.decompress(data[8:])


def test_doubao_client_frames_match_documented_layout() -> None:
    full = asr.doubao_full_client_request(asr.doubao_request_payload({}))
    assert full[:4] == bytes([0x11, 0x10, 0x11, 0x00])
    kind, flags, serial, compress, body = parse_client_frame(full)
    assert (kind, flags, serial, compress) == (0b0001, 0b0000, 0b0001, 0b0001)
    payload = json.loads(body)
    assert payload["audio"] == {"format": "pcm", "codec": "raw", "rate": 16_000, "bits": 16, "channel": 1}
    assert payload["request"]["model_name"] == "bigmodel"
    assert "enable_nonstream" not in payload["request"]
    assert json.loads(parse_client_frame(asr.doubao_full_client_request(
        asr.doubao_request_payload({"enable_nonstream": True})))[4])["request"]["enable_nonstream"] is True

    audio = asr.doubao_audio_request(b"\x01\x02" * 10, last=False)
    assert audio[:4] == bytes([0x11, 0x20, 0x01, 0x00])
    assert parse_client_frame(audio)[4] == b"\x01\x02" * 10
    last = asr.doubao_audio_request(b"\x03\x04", last=True)
    assert last[:4] == bytes([0x11, 0x22, 0x01, 0x00])


def test_doubao_server_frame_parsing() -> None:
    frame = asr.doubao_parse_frame(server_frame({"result": {"text": "你好"}}, sequence=2))
    assert (frame.message_type, frame.sequence, frame.is_last) == (asr.MSG_FULL_SERVER_RESPONSE, 2, False)
    assert asr.doubao_result_text(frame.payload) == "你好"
    final = asr.doubao_parse_frame(server_frame({"result": [{"text": "你好。"}]}, sequence=-3, last=True))
    assert final.sequence == -3 and final.is_last
    assert asr.doubao_result_text(final.payload) == "你好。"

    message = json.dumps({"error": "invalid resource"}).encode()
    header = asr.doubao_header(asr.MSG_SERVER_ERROR, 0, asr.SERIAL_JSON, asr.COMPRESS_NONE)
    error = asr.doubao_parse_frame(header + struct.pack(">II", 45000001, len(message)) + message)
    assert error.error_code == 45000001 and error.payload == {"error": "invalid resource"}
    with pytest.raises(ValueError):
        asr.doubao_parse_frame(b"\x11")


async def test_doubao_stream_end_to_end() -> None:
    seen: dict[str, Any] = {"audio": b"", "flags": []}

    async def handler(ws: ServerConnection) -> None:
        assert ws.request is not None
        seen["headers"] = {k: ws.request.headers[k] for k in
                           ("X-Api-App-Key", "X-Api-Access-Key", "X-Api-Resource-Id", "X-Api-Connect-Id")}
        kind, _, _, _, body = parse_client_frame(await ws.recv())
        assert kind == asr.MSG_FULL_CLIENT_REQUEST
        seen["request"] = json.loads(body)
        sequence = 1
        await ws.send(server_frame({"result": {"text": ""}}, sequence=sequence))
        async for message in ws:
            assert isinstance(message, bytes)
            kind, flags, serial, _, audio = parse_client_frame(message)
            assert (kind, serial) == (asr.MSG_AUDIO_ONLY_REQUEST, asr.SERIAL_NONE)
            seen["audio"] += audio
            seen["flags"].append(flags)
            sequence += 1
            if flags == asr.FLAG_LAST_NO_SEQUENCE:
                await ws.send(server_frame({"result": {"text": "把声音调大一点。"}}, sequence=-sequence, last=True))
                break
            await ws.send(server_frame({"result": {"text": "把声音"}}, sequence=sequence))

    def add_logid(_ws: ServerConnection, _request: Request, response: Response) -> Response:
        response.headers["X-Tt-Logid"] = "log-123"
        return response

    async with fake_server(handler, add_logid) as url:
        res = await asr.run_doubao(spec("doubao-sauc", url), PCM, **FAST)

    assert res.error is None, res.error
    assert res.text == "把声音调大一点。"
    assert res.log_id == "log-123"
    assert res.first_partial_s is not None and res.final_latency_s is not None
    assert seen["audio"] == PCM
    assert seen["flags"][-1] == asr.FLAG_LAST_NO_SEQUENCE
    assert set(seen["flags"][:-1]) == {asr.FLAG_NO_SEQUENCE}
    assert seen["headers"]["X-Api-Resource-Id"] == "volc.bigasr.sauc.duration"


async def test_doubao_error_frame_stops_the_session() -> None:
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        message = b"requested resource not granted"
        header = asr.doubao_header(asr.MSG_SERVER_ERROR, 0, asr.SERIAL_NONE, asr.COMPRESS_NONE)
        await ws.send(header + struct.pack(">II", 45000001, len(message)) + message)
        async for _ in ws:
            pass

    async with fake_server(handler) as url:
        res = await asr.run_doubao(spec("doubao-sauc", url), PCM, pace=1.0,
                                   connect_timeout_s=5.0, result_timeout_s=5.0)

    assert res.error is not None and res.error.startswith("45000001")
    assert not res.timed_out


async def test_connection_refused_is_an_error_not_a_crash() -> None:
    async with fake_server(lambda ws: ws.close()) as url:
        pass  # server gone: the port is closed now
    res = await asr.run_doubao(spec("doubao-sauc", url), PCM, **FAST)
    assert res.error is not None and res.text == ""


# --------------------------------------------------------------------------- pacing


async def test_send_paced_real_time_schedule() -> None:
    sent: list[tuple[int, bool]] = []

    async def send(chunk: bytes, last: bool) -> None:
        sent.append((len(chunk), last))

    timing = asr._Timing()
    pcm = b"\x00" * (asr.BYTES_PER_MS * 50)  # 50 ms -> 20 + 20 + 10 ms chunks
    started = time.monotonic()
    await asr.send_paced(pcm, chunk_ms=20, pace=1.0, send=send, timing=timing)
    elapsed = time.monotonic() - started
    assert sent == [(640, False), (640, False), (320, True)]
    assert 0.035 <= elapsed < 0.5
    assert timing.first_chunk is not None and timing.last_chunk is not None
    assert timing.last_chunk - timing.first_chunk >= 0.035


# --------------------------------------------------------------------------- audio & segmentation


def tone(seconds: float, amplitude: float = 3000.0) -> np.ndarray:
    t = np.arange(int(seconds * 16_000)) / 16_000
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


def quiet(seconds: float) -> np.ndarray:
    return np.random.default_rng(0).normal(0, 20, int(seconds * 16_000)).astype(np.int16)


def test_segments_merge_short_gaps_drop_clicks_and_pad() -> None:
    samples = np.concatenate([
        quiet(1.0), tone(0.8), quiet(1.5),
        tone(0.15), quiet(1.5),  # click shorter than 0.3 s: dropped
        tone(0.6), quiet(0.4), tone(0.6),  # 0.4 s pause inside one utterance: merged
        quiet(1.0),
    ])
    spans = asr.segments(samples)
    assert len(spans) == 2
    (s1, e1), (s2, e2) = spans
    assert abs(s1 / 16_000 - (1.0 - 0.3)) < 0.04
    assert abs(e1 / 16_000 - (1.8 + 0.3)) < 0.04
    assert abs((e2 - s2) / 16_000 - (1.6 + 0.6)) < 0.08
    assert len(asr.segments(samples, merge_gap_ms=300)) == 3  # 0.4 s pause now splits


def test_segments_cap_long_utterances() -> None:
    spans = asr.segments(np.concatenate([quiet(1.0), tone(5.0), quiet(1.0)]), max_s=2.0)
    assert len(spans) == 1 and (spans[0][1] - spans[0][0]) / 16_000 <= 2.0


def write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(samples.astype("<i2").tobytes())


def test_read_wav_pcm_handles_unfinalized_tap_header(tmp_path: Path) -> None:
    samples = tone(0.5)
    good = tmp_path / "good.wav"
    write_wav(good, samples)
    assert np.array_equal(asr.read_wav_pcm(good), samples)

    raw = bytearray(good.read_bytes())
    data_at = raw.index(b"data")
    raw[data_at + 4 : data_at + 8] = b"\x00\x00\x00\x00"  # tap killed before close()
    broken = tmp_path / "broken.wav"
    broken.write_bytes(bytes(raw))
    assert np.array_equal(asr.read_wav_pcm(broken), samples)

    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(b"\x00" * 64)
    with pytest.raises(ValueError, match="16 kHz mono"):
        asr.read_wav_pcm(stereo)


# --------------------------------------------------------------------------- CER & normalization


def test_numeral_normalization_and_cer() -> None:
    assert asr.strip_text("你好， 世界！Hi") == "你好世界hi"
    assert asr.int_reading(10) == "十" and asr.int_reading(105) == "一百零五"
    assert asr.int_reading(20_260) == "二万零二百六十"
    assert asr.digit_reading("2026") == "二零二六"
    # ITN output of the reading script's numeric lines scores as exact.
    for ref, hyp in [
        ("我的手机尾号是三七二九。", "我的手机尾号是3729。"),
        ("二零二六年十月一号是国庆节。", "2026年10月1号是国庆节"),
        ("明天早上七点叫我起床。", "明天早上7:00叫我起床。"),
        ("我女儿今年八岁，上二年级。", "我女儿今年8岁，上2年级。"),
        ("每天练两个小时", "每天练2个小时"),
    ]:
        result = asr.score(ref, hyp)
        assert result.exact and result.edits == 0, (ref, hyp)
        assert result.raw_edits > 0
    miss = asr.score("停停。", "停。")
    assert (miss.edits, miss.ref_chars, miss.exact) == (1, 2, False)
    empty = asr.score("晚安。", "")
    assert empty.empty and empty.cer == 1.0
    assert asr.edit_distance("abc", "axc") == 1 and asr.edit_distance("", "ab") == 2


# --------------------------------------------------------------------------- alignment


def refs(*texts: str) -> list[asr.Reference]:
    return [asr.Reference(i + 1, "c", t) for i, t in enumerate(texts)]


def test_ordered_alignment_skips_echo_and_missing_lines_and_marks_retakes() -> None:
    references = refs("今天天气怎么样？", "现在几点了？", "讲一个短一点的故事。", "晚安。")
    texts = [
        ["今天天气怎么样", "今天天气怎么样？"],
        ["好的我记住了", ""],  # robot echo: no line
        ["讲一个短", "讲个短"],  # false start of line 3 (line 2 never read)
        ["讲一个短一点的故事。", "讲个短点的故事"],  # re-read: the better take is scored
        ["", "晚安"],  # only one engine heard it
    ]
    alignment = asr.align(asr.similarity_matrix(texts, references))
    assert alignment.assigned == [0, None, None, 2, 3]
    assert alignment.kind == ["matched", "unmatched", "retake", "matched", "matched"]
    assert alignment.best[2][0] == 2

    best = asr.align(asr.similarity_matrix(texts, references), mode="best")
    assert best.assigned == [0, None, 2, 2, 3]


def test_ordered_alignment_prefers_reading_order_for_similar_short_lines() -> None:
    references = refs("停。", "停停。", "等一下。")
    texts = [["停"], ["停停"], ["等一下"]]
    assert asr.align(asr.similarity_matrix(texts, references)).assigned == [0, 1, 2]


# --------------------------------------------------------------------------- report & privacy


def _results(text_a: str, text_b: str) -> dict[str, asr.StreamResult]:
    return {
        "funasr": asr.StreamResult("funasr", text_a, 0.4, 0.3, 0.35),
        "doubao": asr.StreamResult("doubao", text_b, 0.3, 0.2, 0.2, error=None),
    }


def test_report_metrics_and_text_privacy() -> None:
    references = [asr.Reference(1, "daily", "现在几点了？"), asr.Reference(2, "stop", "停。")]
    segs = [asr.Segment(i, "tap.wav", float(i), i + 1.0, b"") for i in range(3)]
    results = [
        _results("现在几点了？", "现在几点啦"),
        _results("秘密回声内容", ""),  # echo: false-trigger text on one engine
        _results("", "停。"),
    ]
    results[2]["funasr"].error = "timeout"
    engines = ["funasr", "doubao"]
    texts = [[r[e].text for e in engines] for r in results]
    alignment = asr.align(asr.similarity_matrix(texts, references))
    report = asr.build_report(segs, references, engines, results, alignment, show_text=False)

    overall = {m["engine"]: m for m in report["overall"]}
    assert report["matched"] == 2 and report["unmatched"] == 1
    assert overall["funasr"]["cer"] == round(1 / 6, 4)  # 5 + 1 chars, "停" missing
    assert overall["funasr"]["empty_rate"] == 0.5 and overall["funasr"]["errors"] == 1
    assert overall["doubao"]["cer"] == round(1 / 6, 4) and overall["doubao"]["exact_rate"] == 0.5
    assert report["false_trigger_text"]["funasr"] == {"segments_with_text": 1, "chars": 6}
    assert report["false_trigger_text"]["doubao"]["segments_with_text"] == 0
    assert set(report["per_category"]) == {"daily", "stop"}

    markdown = asr.render_markdown(report, engines, show_text=False)
    dumped = json.dumps(report, ensure_ascii=False)
    for secret in ("秘密回声内容", "现在几点啦", "现在几点了"):
        assert secret not in markdown and secret not in dumped
    assert "| funasr |" in markdown

    shown = asr.build_report(segs, references, engines, results, alignment, show_text=True)
    assert "秘密回声内容" in json.dumps(shown, ensure_ascii=False)
    assert "现在几点啦" in asr.render_markdown(shown, engines, show_text=True)


def test_build_specs_reads_env_only_and_never_exposes_secrets() -> None:
    args = asr.parse_args(["--reference", "r.txt", "a.wav"])
    specs, notes = asr.build_specs(args, {})
    assert specs == [] and len(notes) == 4

    env = {"DASHSCOPE_API_KEY": "sk-secret", "DOUBAO_ASR_APP_ID": "app-secret",
           "DOUBAO_ASR_ACCESS_TOKEN": "tok-secret"}
    specs, notes = asr.build_specs(args, env)
    by_name = {s.name: s for s in specs}
    assert list(by_name) == list(asr.ENGINES) and notes == []
    assert by_name["funasr"].url == "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    assert by_name["qwen-realtime"].url.endswith("/api-ws/v1/realtime?model=qwen3-asr-flash-realtime")
    assert by_name["doubao"].url == "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    assert dict(by_name["doubao"].headers)["X-Api-Resource-Id"] == asr.DOUBAO_DEFAULT_RESOURCE_ID
    public = json.dumps({n: s.public() for n, s in by_name.items()})
    for secret in env.values():
        assert secret not in public and secret not in repr(specs)

    subset = asr.parse_args(["--reference", "r.txt", "--engines", "doubao", "a.wav"])
    specs, _ = asr.build_specs(subset, {"DOUBAO_ASR_API_KEY": "k", "DOUBAO_ASR_RESOURCE_ID": "volc.seedasr.sauc.duration"})
    assert [s.name for s in specs] == ["doubao"]
    assert dict(specs[0].headers)["X-Api-Key"] == "k"
    assert dict(specs[0].headers)["X-Api-Resource-Id"] == "volc.seedasr.sauc.duration"
    with pytest.raises(SystemExit):
        asr.parse_args(["--reference", "r.txt", "--engines", "whisper", "a.wav"])


async def test_run_all_bounds_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    active = peak = 0

    async def fake(spec: asr.EngineSpec, pcm: bytes, **_: Any) -> asr.StreamResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return asr.StreamResult(spec.name, text=str(len(pcm)))

    monkeypatch.setitem(asr.RUNNERS, "dashscope-task", fake)
    segs = [asr.Segment(i, "t.wav", 0.0, 1.0, b"\x00" * (i + 1)) for i in range(5)]
    specs = [asr.EngineSpec(n, "dashscope-task", "m", "ws://x", 100) for n in ("a", "b", "c")]
    results = await asr.run_all(segs, specs, pace=0.0, concurrency=2, connect_timeout_s=1, result_timeout_s=1)
    assert peak == 2
    assert [r["b"].text for r in results] == ["1", "2", "3", "4", "5"]


def test_main_dry_run_segments_without_network(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wav = tmp_path / "tap.wav"
    write_wav(wav, np.concatenate([quiet(1.0), tone(0.8), quiet(1.5), tone(0.8), quiet(1.0)]))
    script = tmp_path / "script.txt"
    script.write_text("stop|停。\n\n# comment\n好了好了。\n", encoding="utf-8")
    assert [(r.category, r.text) for r in asr.read_reference(script)] == [("stop", "停。"), ("all", "好了好了。")]
    assert asr.main(["--reference", str(script), "--dry-run", str(wav)]) == 0
    err = capsys.readouterr().err
    assert "2 segment(s)" in err and "2 reference line(s)" in err
