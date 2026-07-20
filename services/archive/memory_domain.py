"""Public records for reconstructable people, timeline, knowledge and review projections."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from services.archive.domain import EvidenceEvent, SpeakerClass

MemoryCategory = Literal[
    "life_story",
    "work_experience",
    "family_principle",
    "parenting_principle",
    "life_wisdom",
    "daily_life",
]
MemoryStatus = Literal["candidate", "confirmed", "disputed", "retracted"]
AccountWriteGuard = Callable[[str], AbstractAsyncContextManager[None]]


class AccountWriteRejectedError(RuntimeError):
    """A projection write arrived after durable account deletion started."""


@dataclass(frozen=True, slots=True)
class ExtractedClaim:
    category: MemoryCategory
    subject_key: str
    predicate: str
    value: str
    confidence: float
    sensitive_domain: str = "personal"


@dataclass(frozen=True, slots=True)
class ExtractedPerson:
    display_name: str
    relationship_to_owner: str
    canonical_key: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExtractedRelationship:
    person_key: str
    relationship_type: str


@dataclass(frozen=True, slots=True)
class ExtractedTimeline:
    title: str
    category: MemoryCategory
    event_start: datetime
    event_end: datetime | None = None
    time_precision: str = "conversation_time"


@dataclass(frozen=True, slots=True)
class ExtractedKnowledge:
    category: MemoryCategory
    question: str
    answer: str
    applicability: str = ""
    counterexample: str = ""


@dataclass(frozen=True, slots=True)
class MemoryExtraction:
    claims: tuple[ExtractedClaim, ...] = ()
    people: tuple[ExtractedPerson, ...] = ()
    relationships: tuple[ExtractedRelationship, ...] = ()
    timeline: tuple[ExtractedTimeline, ...] = ()
    knowledge: tuple[ExtractedKnowledge, ...] = ()
    extractor_version: str = ""


class MemoryExtractor(Protocol):
    version: str

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction: ...


class MemoryEmbedder(Protocol):
    model: str

    async def embed(self, text: str) -> tuple[float, ...]: ...


class MemoryEmbeddingUnavailableError(RuntimeError):
    """The optional semantic projection cannot currently be generated."""


class MemoryCatalogPort(Protocol):
    async def compile_pending(self, *, limit: int = 100) -> CompileReport: ...

    async def search(self, query: MemorySearchQuery) -> MemorySearchResult: ...

    async def context(self, query: MemorySearchQuery) -> MemorySearchResult: ...

    async def timeline(
        self,
        *,
        account_id: str,
        limit: int = 50,
    ) -> tuple[TimelineItem, ...]: ...

    async def people(
        self,
        *,
        account_id: str,
        limit: int = 100,
    ) -> tuple[PersonItem, ...]: ...

    async def review_queue(self, *, account_id: str) -> tuple[ReviewQueueItem, ...]: ...

    async def review(self, command: MemoryClaimReview) -> ReviewedClaim: ...


@dataclass(frozen=True, slots=True)
class CompileReport:
    compiled_events: int = 0
    ignored_events: int = 0
    failed_events: int = 0


@dataclass(frozen=True, slots=True)
class MemorySearchQuery:
    account_id: str
    speaker_class: SpeakerClass
    text: str = ""
    kinds: tuple[str, ...] = ()
    categories: tuple[MemoryCategory, ...] = ()
    include_candidates: bool = True
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None
    limit: int = 20

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not 1 <= self.limit <= 100:
            raise ValueError("memory search requires account_id and limit 1..100")
        for value in (self.occurred_after, self.occurred_before):
            if value is not None and value.tzinfo is None:
                raise ValueError("memory search timestamps must include timezone")


@dataclass(frozen=True, slots=True)
class MemorySearchItem:
    item_id: str
    kind: str
    title: str
    snippet: str
    category: MemoryCategory
    status: MemoryStatus
    source_event_id: str
    occurred_at: datetime
    score: float


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    items: tuple[MemorySearchItem, ...] = ()


@dataclass(frozen=True, slots=True)
class TimelineItem:
    timeline_id: str
    title: str
    category: MemoryCategory
    status: MemoryStatus
    event_start: datetime
    event_end: datetime | None
    time_precision: str
    source_event_id: str
    episode_id: str


@dataclass(frozen=True, slots=True)
class PersonItem:
    person_id: str
    display_name: str
    relationship_to_owner: str
    aliases: tuple[str, ...]
    status: MemoryStatus
    source_event_id: str


@dataclass(frozen=True, slots=True)
class ReviewQueueItem:
    item_id: str
    kind: str
    category: MemoryCategory
    value: str
    status: MemoryStatus
    reason: str
    source_event_id: str


@dataclass(frozen=True, slots=True)
class MemoryClaimReview:
    account_id: str
    claim_id: str
    action: Literal["confirm", "dispute", "retract", "correct"]
    corrected_value: str | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.claim_id.strip():
            raise ValueError("claim review requires account_id and claim_id")
        if self.action == "correct" and not (self.corrected_value or "").strip():
            raise ValueError("corrected_value is required for correction")
        if self.action != "correct" and self.corrected_value is not None:
            raise ValueError("corrected_value is only valid for correction")


@dataclass(frozen=True, slots=True)
class ReviewedClaim:
    claim_id: str
    status: MemoryStatus
    value: str
    review_event_id: str


def utc_now() -> datetime:
    return datetime.now(UTC)
