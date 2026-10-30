"""The test-period ``reply_text_debug`` line: off by default, and only the test subject.

Soak measures how often two adjacent replies repeat, so it needs the reply text of
the durable test account and nothing else. These pin the switch
(``REPLY_TEXT_DEBUG_LOG``, off by default) and the second gate (the subject must be
the one in ``MINOR_LIMIT_BYPASS_TEST_SUBJECTS``), so a real child session never
writes its reply text.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.minor_limit_bypass import MinorLimitBypass, subject_id_hash
from services.agent.src.reply_text_debug_log import (
    REPLY_TEXT_DEBUG_LOG_ENV,
    REPLY_TEXT_DEBUG_LOG_PREFIX,
    ReplyTextDebugLog,
    log_reply_text,
    reply_text_debug_enabled,
)
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry

LOGGER = "services.agent.src.reply_text_debug_log"
SUBJECT = "ed2b4c0e-6f0a-4b21-9c1a-0d5f4b7c8e91"
UNLISTED = "subject-other-2"
TEXT = "我们来讲一个小熊的故事吧。\n它住在森林里，喜欢蜂蜜。"
FENCE = SimpleNamespace(turn_id=7, generation_id=2)
TEST_SUBJECTS = MinorLimitBypass(frozenset({SUBJECT}))
OFF = ReplyTextDebugLog()
ON = ReplyTextDebugLog(enabled=True)


def _context(*, text: str = TEXT, subject: str = SUBJECT, logging_on: bool = True):
    return SimpleNamespace(
        identity=SimpleNamespace(subject_id=subject),
        output=SimpleNamespace(assistant_text=text),
        reply_text_debug=logging_on,
    )


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.INFO
    ]


def _field(line: str, name: str) -> str:
    prefix = f" {name}="
    start = line.index(prefix) + len(prefix)
    if name == "text":
        return line[start:]
    return line[start:].split(" ", 1)[0]


def test_registry_switch_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REPLY_TEXT_DEBUG_LOG_ENV, raising=False)
    field_info = MediaVoiceCoreRegistry.__dataclass_fields__["reply_text_debug_log"]
    default: ReplyTextDebugLog = field_info.default_factory()  # type: ignore[misc]
    assert isinstance(default, ReplyTextDebugLog)
    assert not default.enabled
    assert not ReplyTextDebugLog.from_env({}).enabled
    assert not ReplyTextDebugLog.from_env({REPLY_TEXT_DEBUG_LOG_ENV: ""}).enabled
    assert not ReplyTextDebugLog.from_env({REPLY_TEXT_DEBUG_LOG_ENV: "0"}).enabled
    assert not ReplyTextDebugLog.from_env({REPLY_TEXT_DEBUG_LOG_ENV: "false"}).enabled
    assert ReplyTextDebugLog.from_env({REPLY_TEXT_DEBUG_LOG_ENV: "1"}).enabled
    assert ReplyTextDebugLog.from_env({REPLY_TEXT_DEBUG_LOG_ENV: " TRUE "}).enabled


def _registry(switch: ReplyTextDebugLog, bypass: MinorLimitBypass) -> Any:
    return SimpleNamespace(reply_text_debug_log=switch, minor_limit_bypass=bypass)


def test_switch_off_denies_the_test_subject() -> None:
    assert not reply_text_debug_enabled(_registry(OFF, TEST_SUBJECTS), SUBJECT)
    assert not reply_text_debug_enabled(SimpleNamespace(), SUBJECT)
    assert not reply_text_debug_enabled(_registry(ON, MinorLimitBypass()), SUBJECT)
    assert not reply_text_debug_enabled(_registry(ON, TEST_SUBJECTS), None)


def test_switch_on_denies_a_session_that_is_not_the_test_subject() -> None:
    assert not reply_text_debug_enabled(_registry(ON, TEST_SUBJECTS), UNLISTED)
    # Two whitelisted subjects disable the bypass, so there is no test subject either.
    assert not reply_text_debug_enabled(
        _registry(ON, MinorLimitBypass(frozenset({SUBJECT, UNLISTED}))), SUBJECT
    )


def test_switch_on_allows_the_test_subject() -> None:
    assert reply_text_debug_enabled(_registry(ON, TEST_SUBJECTS), SUBJECT)


def test_a_denied_session_writes_no_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    log_reply_text(_context(logging_on=False), FENCE)
    assert _lines(caplog) == []


def test_an_allowed_session_writes_turn_id_and_the_full_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    log_reply_text(_context(), FENCE)
    lines = _lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith(f"{REPLY_TEXT_DEBUG_LOG_PREFIX} ")
    assert _field(line, "turn_id") == "7"
    assert _field(line, "generation_id") == "2"
    assert _field(line, "subject_sha256_8") == subject_id_hash(SUBJECT)
    assert _field(line, "text_len") == str(len(TEXT))
    assert json.loads(_field(line, "text")) == TEXT
    assert "\n" not in line


def test_a_line_never_carries_a_number_the_reply_named(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    log_reply_text(_context(text="妈妈电话13800138000"), FENCE)
    assert json.loads(_field(_lines(caplog)[0], "text")) == "妈妈电话[手机号]"


def test_empty_or_marker_replies_write_no_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    log_reply_text(_context(text=" \n"), FENCE)
    log_reply_text(_context(), FENCE, aigc_marker=True)
    log_reply_text(SimpleNamespace(), FENCE)
    assert _lines(caplog) == []
