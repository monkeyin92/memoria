"""Pure boundary tests for Scheme A child memory layers."""

from __future__ import annotations

import pytest
from services.guardian.child_memory import (
    ArchiveFact,
    ChildAudioForbidden,
    DialogueSummary,
    TurnFacts,
    accept_persistent_audio,
    durable_event,
    forbidden_topic,
    one_sentence,
    quotes_speech,
    strip_transcript_fields,
)


def test_accept_persistent_audio_refuses_minor_and_unknown() -> None:
    minor = TurnFacts(
        session_id="s1",
        turn_id=1,
        subject_id="child",
        subject_class="minor",
        pcm=b"\x00\x01",
        transcript="你好",
    )
    with pytest.raises(ChildAudioForbidden):
        accept_persistent_audio(minor)
    unknown = TurnFacts(
        session_id="s2",
        turn_id=1,
        subject_id=None,
        subject_class="unknown",
        pcm=b"",
        transcript="hi",
    )
    with pytest.raises(ChildAudioForbidden):
        accept_persistent_audio(unknown)


def test_durable_event_strips_transcript_for_minors() -> None:
    turn = TurnFacts(
        session_id="s1",
        turn_id=3,
        subject_id="child",
        subject_class="minor",
        pcm=b"",
        transcript="我喜欢小狗",
    )
    event = durable_event(turn, base={"text": "我喜欢小狗", "speaker_class": "owner"})
    assert "text" not in event
    assert event["memory_retention"] == "ephemeral_only"
    assert event["subject_class"] == "minor"


def test_dialogue_summary_rejects_quotes_and_overlength() -> None:
    with pytest.raises(ValueError):
        DialogueSummary(
            subject_id="child",
            text='孩子说"我想回家"',
            source_event_id="e1",
        )
    with pytest.raises(ValueError):
        DialogueSummary(
            subject_id="child",
            text="x" * 201,
            source_event_id="e1",
        )
    ok = DialogueSummary(
        subject_id="child",
        text="孩子聊了小狗和画画",
        source_event_id="e1",
        session_id="sess-1",
    )
    assert ok.text == "孩子聊了小狗和画画"


def test_archive_fact_whitelist_and_forbidden_topics() -> None:
    fact = ArchiveFact(subject_id="child", field="pet_name", text="家里有只叫豆豆的狗")
    assert fact.field == "pet_name"
    with pytest.raises(ValueError):
        ArchiveFact(subject_id="child", field="preference", text="孩子在学校成绩很好")
    assert forbidden_topic("孩子去医院了")
    assert quotes_speech('他说"好"')
    assert one_sentence("喜欢画画")
    assert not one_sentence("喜欢画画。也喜欢跑步。")


def test_strip_transcript_fields() -> None:
    cleaned = strip_transcript_fields(
        {"text": "hi", "transcript": "hi", "turn_id": 1, "assistant_text": "ok"}
    )
    assert cleaned == {"turn_id": 1}
