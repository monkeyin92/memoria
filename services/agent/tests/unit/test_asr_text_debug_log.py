"""The test-period ``asr_text_debug`` line: only listed subjects, and empty finals too.

FunASR sometimes returns an empty final for a one-character 「停」, and the playback-stop path
logged only lengths. These pin the switch (``ASR_TEXT_DEBUG_LOG_SUBJECTS``, off by default, parsed
like ``MINOR_LIMIT_BYPASS_TEST_SUBJECTS``), that an unlisted session never logs its text, and that
an empty final still gets a line with ``empty=True`` and its begin/end milliseconds.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.asr_text_debug_log import (
    ASR_TEXT_DEBUG_LOG_ENV,
    ASR_TEXT_DEBUG_LOG_PREFIX,
    AsrTextDebugSubjects,
    asr_text_debug_line,
    configured_asr_text_debug_subjects,
    session_asr_text_debug_subjects,
    subject_tag,
)
from services.agent.src.providers.funasr_protocol import FunASRSentence
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.src.voice_core.media_session_pending_turn import PendingTurn
from services.agent.src.voice_core.media_session_playback_stop import MediaPlaybackStopMixin
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.src.voice_core.speech_timeline import ASRResult

STOP_LOGGER = "services.agent.src.voice_core.media_session_playback_stop"
FUNASR_LOGGER = "services.agent.src.providers.funasr_stt"
ADAPTER_LOGGER = "services.agent.src.voice_core.provider_adapter"
SUBJECT = "subject-test-1"
UNLISTED = "subject-other-2"


def _debug_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(ASR_TEXT_DEBUG_LOG_PREFIX)
    ]


def test_the_registry_field_is_off_by_default() -> None:
    field_info = MediaVoiceCoreRegistry.__dataclass_fields__["asr_text_debug_subjects"]
    default: AsrTextDebugSubjects = field_info.default_factory()  # type: ignore[misc]
    assert isinstance(default, AsrTextDebugSubjects)
    # A test double without the field reads as off too.
    assert not configured_asr_text_debug_subjects(object()).enabled


def test_the_switch_is_off_unless_the_variable_names_a_subject() -> None:
    assert not AsrTextDebugSubjects.from_env({}).enabled
    assert AsrTextDebugSubjects.from_env({ASR_TEXT_DEBUG_LOG_ENV: ""}).subject_ids == frozenset()
    # Same format as MINOR_LIMIT_BYPASS_TEST_SUBJECTS: comma separated, whitespace trimmed.
    listed = AsrTextDebugSubjects.from_env({ASR_TEXT_DEBUG_LOG_ENV: f" {SUBJECT} , {UNLISTED}, ,"})
    assert listed.subject_ids == frozenset({SUBJECT, UNLISTED})
    assert listed.logs(SUBJECT) and not listed.logs("subject-unlisted")


def test_the_line_never_prints_without_a_listed_id() -> None:
    off = AsrTextDebugSubjects()
    listed = AsrTextDebugSubjects(frozenset({SUBJECT}))
    assert asr_text_debug_line(off, SUBJECT, "", text="停") is None
    assert asr_text_debug_line(listed, UNLISTED, UNLISTED, text="停") is None
    assert asr_text_debug_line(listed, None, "", text="停") is None
    assert asr_text_debug_line(None, SUBJECT, text="停") is None
    line = asr_text_debug_line(
        listed, SUBJECT, "", subject_sha256_8=subject_tag(SUBJECT), text="停"
    )
    assert line is not None and line.startswith(f"{ASR_TEXT_DEBUG_LOG_PREFIX} ")
    assert f"subject_sha256_8={subject_tag(SUBJECT)}" in line and "text=停" in line
    # The plain id selects but is not printed.
    assert f"={SUBJECT}" not in line and subject_tag(SUBJECT) != "-"


class _StopSession(MediaPlaybackStopMixin):
    def _reply_in_flight(self, context: Any) -> bool:
        return True

    def _scope_pending_turn_to(self, context: Any, result: ASRResult) -> None:
        return None

    def _schedule_turn_commit(self, context: Any) -> None:
        return None

    @staticmethod
    def _mark_empty_turn_endpoint_pin(context: Any, *, source: str) -> None:
        return None


def _context(
    *,
    subject_id: str = SUBJECT,
    registry_subjects: AsrTextDebugSubjects | None = None,
) -> Any:
    registry = (
        None
        if registry_subjects is None
        else SimpleNamespace(asr_text_debug_subjects=registry_subjects)
    )
    return SimpleNamespace(
        identity=SimpleNamespace(
            client_type="device",
            session_id="s1",
            subject_id=subject_id,
            device_id="",
        ),
        registry=registry,
        closed=False,
        standby_requested=False,
        pending=PendingTurn(),
        last_playback_end_sample=160_000,
        output=SimpleNamespace(assistant_text="小刺猬回到家。", output_owner=None),
        runtime=SimpleNamespace(assistant_speaking=True),
    )


def _final(text: str, *, begin_ms: int = 0, end_ms: int = 1000) -> ASRResult:
    return ASRResult(
        task_epoch=1,
        sentence_id="sentence-1",
        revision=1,
        capture_start_sample=160_000,
        capture_end_sample=176_000,
        text=text,
        is_final=True,
        provider_begin_ms=begin_ms,
        provider_end_ms=end_ms,
    )


def test_a_listed_subject_logs_the_playback_stop_final_with_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = _context(registry_subjects=AsrTextDebugSubjects(frozenset({SUBJECT})))
    with caplog.at_level(logging.INFO, logger=STOP_LOGGER):
        _StopSession()._maybe_pin_playback_stop(context, _final("停"), source="final")

    [line] = _debug_lines(caplog)
    assert "source=final" in line and "session_id=s1" in line
    assert "empty=False" in line and "text_len=1" in line and "text=1:停" in line
    assert "begin_ms=0" in line and "end_ms=1000" in line
    # The decision log beside it still carries lengths only.
    [decision] = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("media early playback-stop endpoint")
    ]
    assert "停" not in decision and "text_len=1" in decision


def test_an_unlisted_subject_logs_no_text_even_when_others_are_listed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = _context(subject_id=UNLISTED, registry_subjects=AsrTextDebugSubjects(frozenset({SUBJECT})))
    with caplog.at_level(logging.INFO, logger=STOP_LOGGER):
        _StopSession()._maybe_pin_playback_stop(context, _final("停"), source="final")

    assert not _debug_lines(caplog)
    assert all("停" not in record.getMessage() for record in caplog.records)


def test_the_default_off_switch_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    # A registry without the field, and one with it still empty, both stay silent.
    for context in (_context(), _context(registry_subjects=AsrTextDebugSubjects())):
        with caplog.at_level(logging.INFO, logger=STOP_LOGGER):
            _StopSession()._maybe_pin_playback_stop(context, _final("停"), source="final")
    assert not _debug_lines(caplog)


def test_an_empty_final_is_logged_with_empty_true_and_its_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The 「停」 FunASR dropped: text_len=0, so every guard below sees nothing.
    context = _context(registry_subjects=AsrTextDebugSubjects(frozenset({SUBJECT})))
    with caplog.at_level(logging.INFO, logger=STOP_LOGGER):
        _StopSession()._maybe_pin_playback_stop(
            context, _final("", begin_ms=1200, end_ms=1460), source="final"
        )

    [line] = _debug_lines(caplog)
    assert "empty=True" in line and "text_len=0" in line
    assert "begin_ms=1200" in line and "end_ms=1460" in line and "text=-" in line


def test_a_registry_switch_reaches_only_its_listed_session() -> None:
    listed = AsrTextDebugSubjects(frozenset({SUBJECT}))
    registry = SimpleNamespace(asr_text_debug_subjects=listed)

    class _Session:
        pass

    session = _Session()
    session.registry = registry  # type: ignore[attr-defined]
    session.identity = SimpleNamespace(subject_id=SUBJECT, device_id="")  # type: ignore[attr-defined]
    assert session_asr_text_debug_subjects(session).logs(SUBJECT)
    session.identity = SimpleNamespace(subject_id=UNLISTED, device_id="")  # type: ignore[attr-defined]
    assert not session_asr_text_debug_subjects(session).enabled


def _funasr_session(subjects: AsrTextDebugSubjects | None, *subject_ids: str) -> FunASRSession:
    """A provider session the bridge built for one session."""

    return FunASRSession(
        FunASRConfig(api_key="test", ws_url="ws://unused"),
        asr_text_debug=subjects,
        asr_text_debug_subject_ids=subject_ids,
        asr_text_debug_session_id="s1",
    )


def _sentence(text: str, *, begin_ms: int = 700, end_ms: int = 980) -> FunASRSentence:
    return FunASRSentence(
        sentence_id=3,
        text=text,
        begin_ms=begin_ms,
        end_ms=end_ms,
        sentence_end=True,
        heartbeat=False,
        words=(),
    )


def test_funasr_realtime_final_logs_the_raw_text_for_a_listed_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _funasr_session(AsrTextDebugSubjects(frozenset({SUBJECT})), SUBJECT)
    with caplog.at_level(logging.INFO, logger=FUNASR_LOGGER):
        session._log_realtime_final_text(_sentence("停"), "task-1")

    [line] = _debug_lines(caplog)
    assert "source=realtime_final" in line and "task_id=task-1" in line
    assert "session_id=s1" in line and "final=True" in line
    assert "empty=False" in line and "text=1:停" in line
    assert "begin_ms=700" in line and "end_ms=980" in line


def test_funasr_logs_an_empty_final_and_nothing_at_all_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=FUNASR_LOGGER):
        _funasr_session(None, SUBJECT)._log_realtime_final_text(_sentence(""), "task-1")
    assert not _debug_lines(caplog)

    with caplog.at_level(logging.INFO, logger=FUNASR_LOGGER):
        # The empty sentence must carry the text field's absence, not an empty one.
        _funasr_session(None, SUBJECT)._log_realtime_final_text(_sentence(""), "task-1")
        _funasr_session(
            AsrTextDebugSubjects(frozenset({SUBJECT})), SUBJECT
        )._log_realtime_final_text(_sentence(" "), "task-1")
    [line] = _debug_lines(caplog)
    assert "empty=True" in line and "text_len=1" in line
    assert "begin_ms=700" in line and "end_ms=980" in line


def test_funasr_logs_the_empty_class_where_one_is_classified(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _funasr_session(AsrTextDebugSubjects(frozenset({SUBJECT})), SUBJECT)
    with caplog.at_level(logging.INFO, logger=FUNASR_LOGGER):
        session._log_asr_text_debug_empty("task-1", "empty+vendor_silent", "rescue_no_text")

    [line] = _debug_lines(caplog)
    assert "empty=True" in line and "asr_empty_class=empty+vendor_silent" in line
    assert "rescue_no_text" in line and "text=-" in line


def test_the_bridge_normalized_final_is_logged_for_a_listed_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=lambda: _funasr_session(None),
        language_model=SimpleNamespace(),
        speech_synthesis=SimpleNamespace(),
        asr_text_debug_subjects=AsrTextDebugSubjects(frozenset({SUBJECT})),
    )
    identity = SimpleNamespace(
        session_id="s1", subject_id=SUBJECT, device_id="", stream_epoch=1
    )
    with caplog.at_level(logging.INFO, logger=ADAPTER_LOGGER):
        adapter._log_asr_text_debug_decision(identity, _final("停"), source="segment_final")  # type: ignore[arg-type]

    [line] = _debug_lines(caplog)
    # The plain ids select but never print; the tag is what other logs show.
    assert "subject-test-1" not in line and "text=1:停" in line
    assert f"subject_sha256_8={subject_tag(SUBJECT)}" in line

    unlisted = SimpleNamespace(session_id="s2", subject_id=UNLISTED, device_id="", stream_epoch=1)
    with caplog.at_level(logging.INFO, logger=ADAPTER_LOGGER):
        adapter._log_asr_text_debug_decision(unlisted, _final("停"), source="segment_final")  # type: ignore[arg-type]
    assert len(_debug_lines(caplog)) == 1
