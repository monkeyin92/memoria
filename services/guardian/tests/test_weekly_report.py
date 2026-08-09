from __future__ import annotations

import json
from datetime import UTC, date, datetime

from services.archive.domain import EvidenceEvent
from services.guardian.weekly_report import WeeklyReportProjector


def _event(
    event_id: str,
    *,
    event_type: str,
    day: int,
    payload: dict[str, object],
    account_id: str = "minor-1",
) -> EvidenceEvent:
    return EvidenceEvent(
        event_id=event_id,
        account_id=account_id,
        event_type=event_type,
        occurred_at=datetime(2026, 8, day, 4, tzinfo=UTC),
        speaker_class="system",
        source="test.authoritative_projection",
        payload=payload,
    )


def _eligible(**values: object) -> dict[str, object]:
    return {
        **values,
        "history_eligible": True,
        "owner_projection_eligible": True,
        "text": "这段原文绝不能出现在家长周报",
    }


def test_weekly_projection_uses_only_fenced_authoritative_aggregates() -> None:
    events = [
        _event("emotion-1", event_type="emotion_observation", day=3, payload=_eligible(label="sad")),
        _event("emotion-2", event_type="emotion_observation", day=3, payload=_eligible(label="fearful")),
        _event("emotion-3", event_type="emotion_observation", day=3, payload=_eligible(label="neutral")),
        _event("study-1", event_type="tutor.practice_completed", day=4, payload=_eligible(duration_seconds=1850)),
        _event("topic-1", event_type="topic.observation", day=4, payload=_eligible(topic_domain="english")),
        _event("guest", event_type="emotion_observation", day=4, payload={"label": "sad", "history_eligible": False, "owner_projection_eligible": False}),
        _event("unknown", event_type="emotion_observation", day=4, payload=_eligible(label="diagnosis_score")),
        _event("other-account", event_type="emotion_observation", day=4, account_id="minor-2", payload=_eligible(label="sad")),
    ]

    report = WeeklyReportProjector.build(
        minor_user_id="minor-1",
        week_start=date(2026, 8, 3),
        events=events,
    )

    assert dict(report.emotion_distribution) == {"fearful": 1, "neutral": 1, "sad": 1}
    assert report.care_days == (date(2026, 8, 3),)
    assert report.study_minutes == 30
    assert dict(report.topic_distribution) == {"english": 1}
    assert report.source_event_count == 5


def test_public_weekly_payload_never_contains_transcript_or_diagnosis() -> None:
    report = WeeklyReportProjector.build(
        minor_user_id="minor-1",
        week_start=date(2026, 8, 3),
        events=[
            _event(
                "emotion-1",
                event_type="emotion_observation",
                day=3,
                payload=_eligible(label="happy"),
            )
        ],
    )

    serialized = json.dumps(report.public_payload(), ensure_ascii=False)
    assert "这段原文绝不能出现在家长周报" not in serialized
    assert '"text"' not in serialized
    assert "score" not in serialized
    assert report.public_payload()["privacy"] == {
        "contains_transcript": False,
        "diagnostic_assessment": False,
    }


def test_duplicate_and_out_of_window_evidence_is_ignored() -> None:
    duplicate = _event(
        "emotion-1",
        event_type="emotion_observation",
        day=3,
        payload=_eligible(label="happy"),
    )
    report = WeeklyReportProjector.build(
        minor_user_id="minor-1",
        week_start=date(2026, 8, 3),
        events=[
            duplicate,
            duplicate,
            _event("old", event_type="emotion_observation", day=2, payload=_eligible(label="sad")),
        ],
    )

    assert dict(report.emotion_distribution) == {"happy": 1}
    assert report.source_event_count == 1
