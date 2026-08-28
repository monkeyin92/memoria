from __future__ import annotations

from services.agent.src.providers.funasr_protocol import (
    build_continue_task_context,
    build_finish_task,
    build_run_task,
    conversation_item_to_funasr_context,
    parse_server_message,
    result_trace_metrics,
    sentence_to_asr_result,
    timestamps_monotonic,
    words_to_seconds,
)
from services.agent.src.voice_core.speech_timeline import ASRResult, ASRTimingCoverage


def test_run_task_shape() -> None:
    msg = build_run_task(task_id="t1")
    assert msg["header"]["action"] == "run-task"
    assert msg["payload"]["model"] == "fun-asr-realtime"
    assert msg["payload"]["parameters"]["sample_rate"] == 16000
    assert msg["payload"]["parameters"]["semantic_punctuation_enabled"] is False
    assert "vocabulary_id" not in msg["payload"]["parameters"]
    assert "speech_noise_threshold" not in msg["payload"]["parameters"]
    assert msg["payload"]["input"] == {}


def test_run_task_adds_optional_vocabulary_and_noise_threshold() -> None:
    msg = build_run_task(
        task_id="t1",
        vocabulary_id="vocab-control-commands",
        speech_noise_threshold=-0.1,
    )

    assert msg["payload"]["parameters"]["vocabulary_id"] == "vocab-control-commands"
    assert msg["payload"]["parameters"]["speech_noise_threshold"] == -0.1


def test_parse_result_generated() -> None:
    raw = {
        "header": {"event": "result-generated", "task_id": "t1"},
        "payload": {
            "output": {
                "sentence": {
                    "begin_time": 170,
                    "end_time": 920,
                    "text": "好的，我明白了。",
                    "heartbeat": False,
                    "sentence_end": True,
                    "sentence_id": 1,
                    "words": [
                        {"begin_time": 170, "end_time": 295, "text": "好", "punctuation": ""}
                    ],
                }
            }
        },
    }
    ev = parse_server_message(raw)
    assert ev.event == "result-generated"
    assert ev.sentence is not None
    assert ev.sentence.sentence_end is True
    assert timestamps_monotonic(ev.sentence.words)
    secs = words_to_seconds(ev.sentence.words)
    assert abs(secs[0][1] - 0.17) < 1e-9


def test_sentence_to_asr_result_uses_sample_clock() -> None:
    sentence = parse_server_message(
        {
            "header": {"event": "result-generated", "task_id": "t1"},
            "payload": {
                "output": {
                    "sentence": {
                        "sentence_id": 3,
                        "begin_time": 170,
                        "end_time": 920,
                        "text": "你好",
                        "sentence_end": True,
                    }
                }
            },
        }
    ).sentence
    assert sentence is not None

    result = sentence_to_asr_result(sentence, task_epoch=2)

    assert isinstance(result, ASRResult)
    assert result.task_epoch == 2
    assert result.sentence_id == "3"
    assert result.capture_start_sample == 2720
    assert result.capture_end_sample == 14720
    assert result.is_final is True

    offset = sentence_to_asr_result(sentence, task_epoch=2, sample_offset=10_000)
    assert offset.capture_start_sample == 12_720
    assert offset.capture_end_sample == 24_720


def test_sentence_to_asr_result_downgrades_malformed_word_timing() -> None:
    sentence = parse_server_message(
        {
            "header": {"event": "result-generated", "task_id": "t1"},
            "payload": {
                "sentence": {
                    "sentence_id": 4,
                    "begin_time": 0,
                    "end_time": 100,
                    "text": "你好",
                    "sentence_end": True,
                    "words": [
                        {"begin_ms": 60, "end_ms": 80, "text": "好"},
                        {"begin_ms": 10, "end_ms": 30, "text": "你"},
                    ],
                }
            },
        }
    ).sentence
    assert sentence is not None

    result = sentence_to_asr_result(sentence, task_epoch=1)
    assert result.text == "你好"
    assert result.timing_evidence is not None
    assert result.timing_evidence.coverage is ASRTimingCoverage.INVALID


def test_negative_provider_word_timing_does_not_fail_sentence_parsing() -> None:
    sentence = parse_server_message(
        {
            "header": {"event": "result-generated", "task_id": "t1"},
            "payload": {
                "sentence": {
                    "sentence_id": 5,
                    "begin_time": 0,
                    "end_time": 100,
                    "text": "你好",
                    "sentence_end": True,
                    "words": [
                        {"begin_time": -10, "end_time": 30, "text": "你"},
                        {"begin_time": 30, "end_time": 60, "text": "好"},
                    ],
                }
            },
        }
    ).sentence
    assert sentence is not None
    assert not sentence.word_timing_valid

    result = sentence_to_asr_result(sentence, task_epoch=1)
    assert result.text == "你好"
    assert result.timing_evidence.coverage is ASRTimingCoverage.INVALID


def test_sentence_to_asr_result_downgrades_word_outside_sentence_range() -> None:
    sentence = parse_server_message(
        {
            "header": {"event": "result-generated", "task_id": "t1"},
            "payload": {
                "sentence": {
                    "sentence_id": 5,
                    "begin_time": 0,
                    "end_time": 20,
                    "text": "你好",
                    "sentence_end": True,
                    "words": [{"begin_ms": 0, "end_ms": 40, "text": "你好"}],
                }
            },
        }
    ).sentence
    assert sentence is not None

    result = sentence_to_asr_result(sentence, task_epoch=1)
    assert result.capture_end_sample == 320
    assert result.timing_evidence is not None
    assert result.timing_evidence.coverage is ASRTimingCoverage.INVALID


def test_context_redaction() -> None:
    item = conversation_item_to_funasr_context(
        {"role": "user", "text": "我的手机是13812345678，住北京市朝阳区建国路88号"}
    )
    assert item is not None
    assert "13812345678" not in str(item)
    assert item["role"] == "user"
    assert item["content"] == [{"type": "input_text", "text": "我的手机是[手机号]，[地址]"}]


def test_control_messages_and_server_event_branches() -> None:
    finish = build_finish_task("t")
    assert finish["header"]["action"] == "finish-task"
    assert finish["payload"]["input"] == {}
    assert build_continue_task_context("t", [{"role": "user", "text": "x"}])["payload"]["input"][
        "context"
    ]
    for event in ("task-started", "task-finished", "task-failed", "future-event"):
        parsed = parse_server_message(
            {
                "header": {"event": event, "task_id": "t"},
                "payload": {"error": "bad"},
            }
        )
        assert parsed.event == (event if event != "future-event" else "unknown")
        if event == "task-failed":
            assert parsed.error_message == "bad"
    parsed_bytes = parse_server_message(b'{"header":{"event":"task-started","task_id":"t"}}')
    assert parsed_bytes.event == "task-started"


def test_missing_sentence_invalid_words_and_non_monotonic_timestamps() -> None:
    missing = parse_server_message(
        {"header": {"event": "result-generated", "task_id": "t"}, "payload": {}}
    )
    assert missing.sentence is None
    parsed = parse_server_message(
        {
            "header": {"event": "result-generated", "task_id": "t"},
            "payload": {
                "sentence": {
                    "text": "乱序",
                    "words": [
                        None,
                        {"begin_ms": 100, "end_ms": 120, "text": "乱"},
                        {"begin_ms": 50, "end_ms": 40, "text": "序"},
                    ],
                }
            },
        }
    )
    assert parsed.sentence is not None
    assert not timestamps_monotonic(parsed.sentence.words)


def test_context_filters_invalid_role_and_redacts_id_card() -> None:
    assert conversation_item_to_funasr_context({"role": "system", "text": "x"}) is None
    item = conversation_item_to_funasr_context(
        {"role": "assistant", "content": "身份证11010119900101001X"}
    )
    assert item is not None
    assert "11010119900101001X" not in str(item)
    assert item["content"] == [{"type": "text", "text": "身份证[身份证]"}]


def test_task_failed_reads_header_error_message() -> None:
    parsed = parse_server_message(
        {
            "header": {
                "event": "task-failed",
                "task_id": "t",
                "error_code": "InvalidParameter",
                "error_message": "Missing required parameter payload.input",
            },
            "payload": {},
        }
    )

    assert parsed.error_code == "InvalidParameter"
    assert parsed.error_message == "Missing required parameter payload.input"


def test_task_failed_diagnostics_are_bounded_and_redacted() -> None:
    parsed = parse_server_message(
        {
            "header": {"event": "task-failed", "task_id": "t"},
            "payload": {
                "code": "InvalidParameter",
                "message": "api_key=sk_live_123456789012 for 13812345678\n",
            },
        }
    )

    assert parsed.error_code == "InvalidParameter"
    assert parsed.error_message == "api_key=[REDACTED] for [手机号]"
    assert "sk_live_123456789012" not in parsed.error_message
    assert "\n" not in parsed.error_message


def test_result_trace_metrics_expose_timing_without_transcript_text() -> None:
    event = parse_server_message(
        {
            "header": {"event": "result-generated", "task_id": "provider-secret-id"},
            "payload": {
                "output": {
                    "sentence": {
                        "sentence_id": 7,
                        "begin_time": 120,
                        "end_time": 980,
                        "text": "这是不能进入诊断事件的原文",
                        "sentence_end": True,
                    }
                }
            },
        }
    )
    assert event.sentence is not None

    metrics = result_trace_metrics(event.sentence, task_epoch=2)

    assert metrics == {
        "task_epoch": 2,
        "sentence_id": 7,
        "begin_ms": 120,
        "end_ms": 980,
        "duration_ms": 860,
        "text_len": 13,
    }
    assert "text" not in metrics
    assert "task_id" not in metrics
