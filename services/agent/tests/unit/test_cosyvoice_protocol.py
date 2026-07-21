from __future__ import annotations

import json

import pytest
from services.agent.src.contracts.events import TimedWord
from services.agent.src.providers.cosyvoice_protocol import (
    build_continue_text,
    build_finish_task,
    build_run_task,
    parse_server_message,
    pcm_duration_ms,
    scale_word_timestamps,
)


def test_cosyvoice_request_shapes() -> None:
    run = build_run_task()
    assert run["header"]["task_id"]
    assert run["payload"]["parameters"]["word_timestamp_enabled"] is True
    assert "instruction" not in run["payload"]["parameters"]
    instructed = build_run_task(instruction="你说话的情感是 happy。")
    assert instructed["payload"]["parameters"]["instruction"] == "你说话的情感是 happy。"
    assert build_continue_text("t", "你好")["payload"]["input"]["text"] == "你好"
    finish = build_finish_task("t")
    assert finish["header"]["action"] == "finish-task"
    assert finish["payload"]["input"] == {}


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("task-started", {}),
        ("task-finished", {}),
        ("task-failed", {"message": "failed"}),
        ("sentence-begin", {"output": {"sentence_index": 2, "text": "你"}}),
        ("sentence-synthesis", {"output": {"index": 2}}),
        (
            "sentence-end",
            {
                "output": {
                    "index": 2,
                    "text": "你好",
                    "words": [
                        None,
                        {"begin_ms": 0, "end_ms": 80, "text": "你"},
                        {"begin_time": 80, "end_time": 160, "text": "好"},
                    ],
                }
            },
        ),
        ("future-event", {}),
    ],
)
def test_parse_all_server_events(event: str, payload: dict[str, object]) -> None:
    raw = {"header": {"event": event, "task_id": "t"}, "payload": payload}
    parsed = parse_server_message(json.dumps(raw))
    expected = event if event != "future-event" else "unknown"
    assert parsed.event == expected
    assert parsed.task_id == "t"
    if event == "task-failed":
        assert parsed.error_message == "failed"
    if event == "sentence-end":
        assert len(parsed.words) == 2


def test_binary_pcm_is_not_parsed_as_json() -> None:
    with pytest.raises(TypeError):
        parse_server_message(b"\x00\x00")


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

    assert parsed.error_message == "Missing required parameter payload.input"


@pytest.mark.parametrize(
    ("subtype", "expected_text", "expected_words"),
    [
        ("sentence-begin", "你好", 0),
        ("sentence-synthesis", None, 0),
        ("sentence-end", "你好", 2),
    ],
)
def test_parse_current_result_generated_shape(
    subtype: str,
    expected_text: str | None,
    expected_words: int,
) -> None:
    parsed = parse_server_message(
        {
            "header": {"event": "result-generated", "task_id": "t"},
            "payload": {
                "output": {
                    "type": subtype,
                    "original_text": "你好" if subtype != "sentence-synthesis" else None,
                    "sentence": {
                        "index": 3,
                        "words": [
                            {"text": "你", "begin_time": 0, "end_time": 80},
                            {"text": "好", "begin_time": 80, "end_time": 160},
                        ]
                        if subtype == "sentence-end"
                        else [],
                    },
                }
            },
        }
    )

    assert parsed.event == subtype
    assert parsed.sentence_index == 3
    assert parsed.text == expected_text
    assert len(parsed.words) == expected_words


def test_pcm_channels_and_degraded_timestamp_edges() -> None:
    assert pcm_duration_ms(b"\x00" * 960, sample_rate=24000, num_channels=2) == 10
    assert scale_word_timestamps((), pcm_duration_ms_value=100) == ((), "degraded")
    zero = (TimedWord(text="你", begin_ms=0, end_ms=0),)
    assert scale_word_timestamps(zero, pcm_duration_ms_value=100) == ((), "degraded")
