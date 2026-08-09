"""Rebuildable, transcript-free weekly guardian projection.

The projection consumes only already-authorized evidence.  It deliberately
does not infer a diagnosis or expose source text; callers receive bounded
counts and durations that can be deleted and rebuilt from the ledger.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any, Final
from zoneinfo import ZoneInfo

from services.archive.domain import EvidenceEvent

WEEKLY_REPORT_POLICY_VERSION: Final = "guardian-weekly-v1"
EMOTION_LABELS: Final = frozenset(
    {"neutral", "happy", "sad", "angry", "fearful", "disgusted", "surprised"}
)
CARE_LABELS: Final = frozenset({"sad", "angry", "fearful", "disgusted"})
TOPIC_DOMAINS: Final = frozenset(
    {"english", "homework", "school", "interests", "family", "friends", "daily", "other"}
)
_MAX_PRACTICE_SECONDS_PER_EVENT: Final = 4 * 60 * 60


def _eligible(payload: Mapping[str, Any]) -> bool:
    interaction = payload.get("interaction")
    nested_history = interaction.get("history_eligible") if isinstance(interaction, Mapping) else None
    nested_owner = (
        interaction.get("owner_projection_eligible")
        if isinstance(interaction, Mapping)
        else None
    )
    history = payload.get("history_eligible", nested_history)
    owner = payload.get("owner_projection_eligible", nested_owner)
    return history is True and owner is True


@dataclass(frozen=True, slots=True)
class WeeklyReport:
    minor_user_id: str
    week_start: date
    week_end: date
    emotion_distribution: Mapping[str, int]
    care_days: tuple[date, ...]
    study_minutes: int
    topic_distribution: Mapping[str, int]
    source_event_count: int
    policy_version: str = WEEKLY_REPORT_POLICY_VERSION

    def __post_init__(self) -> None:
        if not self.minor_user_id.strip():
            raise ValueError("minor_user_id must not be blank")
        if self.week_end < self.week_start:
            raise ValueError("week_end must not precede week_start")
        if self.study_minutes < 0 or self.source_event_count < 0:
            raise ValueError("weekly report counts must not be negative")
        object.__setattr__(
            self,
            "emotion_distribution",
            MappingProxyType(dict(sorted(self.emotion_distribution.items()))),
        )
        object.__setattr__(
            self,
            "topic_distribution",
            MappingProxyType(dict(sorted(self.topic_distribution.items()))),
        )

    def public_payload(self) -> dict[str, Any]:
        """Return the only shape allowed to cross the guardian API boundary."""

        return {
            "minor_user_id": self.minor_user_id,
            "window": {
                "start": self.week_start.isoformat(),
                "end": self.week_end.isoformat(),
            },
            "emotion_distribution": dict(self.emotion_distribution),
            "care_days": [value.isoformat() for value in self.care_days],
            "study_minutes": self.study_minutes,
            "topic_distribution": dict(self.topic_distribution),
            "source_event_count": self.source_event_count,
            "policy_version": self.policy_version,
            "privacy": {
                "contains_transcript": False,
                "diagnostic_assessment": False,
            },
        }


class WeeklyReportProjector:
    """Fold immutable evidence into one bounded weekly read model."""

    @staticmethod
    def build(
        *,
        minor_user_id: str,
        week_start: date,
        events: Iterable[EvidenceEvent],
        timezone: str = "Asia/Shanghai",
    ) -> WeeklyReport:
        zone = ZoneInfo(timezone)
        week_end = date.fromordinal(week_start.toordinal() + 6)
        emotions: Counter[str] = Counter()
        emotions_by_day: dict[date, Counter[str]] = {}
        topics: Counter[str] = Counter()
        study_seconds = 0
        accepted_ids: set[str] = set()

        for event in events:
            if event.account_id != minor_user_id or event.event_id in accepted_ids:
                continue
            local_day = event.occurred_at.astimezone(zone).date()
            if local_day < week_start or local_day > week_end:
                continue
            payload = event.payload
            if not _eligible(payload):
                continue

            accepted = False
            if event.event_type == "emotion_observation":
                label = payload.get("label")
                if isinstance(label, str) and label in EMOTION_LABELS:
                    emotions[label] += 1
                    emotions_by_day.setdefault(local_day, Counter())[label] += 1
                    accepted = True
            elif event.event_type in {"tutor.practice_completed", "study.progress_updated"}:
                duration = payload.get("duration_seconds")
                if isinstance(duration, int) and not isinstance(duration, bool) and 0 < duration:
                    study_seconds += min(duration, _MAX_PRACTICE_SECONDS_PER_EVENT)
                    accepted = True
            elif event.event_type == "topic.observation":
                topic = payload.get("topic_domain")
                if isinstance(topic, str) and topic in TOPIC_DOMAINS:
                    topics[topic] += 1
                    accepted = True
            if accepted:
                accepted_ids.add(event.event_id)

        care_days = tuple(
            sorted(
                day
                for day, counts in emotions_by_day.items()
                if sum(counts.values()) >= 3
                and sum(counts[label] for label in CARE_LABELS) * 3
                >= sum(counts.values()) * 2
            )
        )
        return WeeklyReport(
            minor_user_id=minor_user_id,
            week_start=week_start,
            week_end=week_end,
            emotion_distribution=emotions,
            care_days=care_days,
            study_minutes=study_seconds // 60,
            topic_distribution=topics,
            source_event_count=len(accepted_ids),
        )


def utc_now() -> datetime:
    """Injectable timestamp seam for projection jobs."""

    return datetime.now(UTC)
