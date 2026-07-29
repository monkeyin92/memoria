"""Pure episode matching; storage adapters keep evidence and projection writes atomic."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from services.archive.memory_domain import DomainCategory, MemorySensitivity


@dataclass(frozen=True, slots=True)
class EpisodeCandidate:
    account_id: str
    source_event_id: str
    session_id: str | None
    title: str
    domain_category: DomainCategory
    event_start: datetime
    event_end: datetime | None
    canonical_key: str
    entity_ids: tuple[str, ...]
    salience: float
    sensitivity: MemorySensitivity

    @property
    def fallback_key(self) -> str:
        if self.canonical_key.strip():
            return (
                f"canonical:{self.domain_category}:"
                f"{_normalized(self.canonical_key)}"
            )
        if self.session_id:
            return (
                f"session:{self.session_id}:{self.domain_category}:"
                f"{self.event_start.date().isoformat()}:"
                f"{_normalized(self.title)[:80]}"
            )
        return (
            f"title:{self.domain_category}:{self.event_start.date().isoformat()}:"
            f"{_normalized(self.title)[:80]}"
        )


@dataclass(frozen=True, slots=True)
class ExistingEpisode:
    episode_id: str
    consolidation_key: str
    title: str
    domain_category: DomainCategory
    event_start: datetime
    event_end: datetime | None
    entity_ids: tuple[str, ...]


class EpisodeConsolidator:
    """Choose one existing real-world episode without mutating source evidence."""

    def __init__(self, *, similarity_threshold: float = 0.62) -> None:
        if not 0 <= similarity_threshold <= 1:
            raise ValueError("episode similarity threshold must be between 0 and 1")
        self._threshold = similarity_threshold

    def choose(
        self,
        candidate: EpisodeCandidate,
        existing: tuple[ExistingEpisode, ...],
    ) -> ExistingEpisode | None:
        matches: list[tuple[float, ExistingEpisode]] = []
        for episode in existing:
            score = self._score(candidate, episode)
            if score >= self._threshold:
                matches.append((score, episode))
        if not matches:
            return None
        return max(matches, key=lambda item: (item[0], item[1].episode_id))[1]

    @staticmethod
    def _score(candidate: EpisodeCandidate, episode: ExistingEpisode) -> float:
        if candidate.domain_category != episode.domain_category:
            return 0.0

        candidate_key = _canonical_value(candidate.fallback_key)
        episode_key = _canonical_value(episode.consolidation_key)
        if candidate_key and episode_key and candidate_key == episode_key:
            return 1.0
        if (
            candidate.entity_ids
            and episode.entity_ids
            and set(candidate.entity_ids).isdisjoint(episode.entity_ids)
        ):
            return 0.0
        if candidate.fallback_key == episode.consolidation_key:
            return 1.0

        title_score = _jaccard(_bigrams(candidate.title), _bigrams(episode.title))
        entity_score = _jaccard(set(candidate.entity_ids), set(episode.entity_ids))
        time_score = _time_score(
            candidate.event_start,
            candidate.event_end,
            episode.event_start,
            episode.event_end,
        )
        score = 0.55 * title_score + 0.3 * entity_score + 0.15 * time_score
        if (
            _session_scope(candidate.fallback_key)
            and _session_scope(candidate.fallback_key)
            == _session_scope(episode.consolidation_key)
        ):
            score = max(score, 0.6 + 0.4 * title_score)
        return score


def _canonical_value(value: str) -> str:
    return value.removeprefix("canonical:") if value.startswith("canonical:") else ""


def _session_scope(value: str) -> str:
    if not value.startswith("session:") or ":" not in value:
        return ""
    return value.rsplit(":", 1)[0]


def _normalized(value: str) -> str:
    return "".join(re.findall(r"[\w\u4e00-\u9fff]+", value.casefold()))


def _bigrams(value: str) -> set[str]:
    normalized = _normalized(value)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _time_score(
    left_start: datetime,
    left_end: datetime | None,
    right_start: datetime,
    right_end: datetime | None,
) -> float:
    left_end = left_end or left_start
    right_end = right_end or right_start
    if left_start <= right_end and right_start <= left_end:
        return 1.0
    gap_seconds = min(
        abs((left_start - right_end).total_seconds()),
        abs((right_start - left_end).total_seconds()),
    )
    gap_days = gap_seconds / 86_400
    if gap_days <= 7:
        return 0.8
    if gap_days <= 30:
        return 0.4
    if gap_days <= 365:
        return 0.1
    return 0.0
