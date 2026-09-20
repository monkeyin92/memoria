"""Public records for reconstructable people, timeline, knowledge and review projections."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from services.archive.domain import EvidenceEvent, SpeakerClass


def lexical_query_terms(text: str, *, max_terms: int = 48) -> tuple[str, ...]:
    """Return bounded exact/Chinese n-gram terms for sparse memory fallback."""

    normalized = unicodedata.normalize("NFKC", text or "").strip().lower()
    if not normalized or max_terms < 1:
        return ()
    chunks = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized)
    terms: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if len(value) < 2 or value in seen:
            return
        seen.add(value)
        terms.append(value)

    for chunk in chunks:
        if re.fullmatch(r"[a-z0-9]+", chunk):
            add(chunk)
            continue
        if len(chunk) <= 12:
            add(chunk)
        for size in (3, 2):
            for start in range(0, len(chunk) - size + 1):
                add(chunk[start : start + size])
                if len(terms) >= max_terms:
                    return tuple(terms)
    return tuple(terms[:max_terms])


def subject_lineage_predicates(
    *,
    bind: Callable[[str], str],
    subject_id: str,
    account_column: str,
    single_source_column: str,
    evidence_table: str = "evidence_events",
    merged_source_link: tuple[str, str, str] | None = None,
    merged_source_extra: tuple[tuple[str, str], ...] = (),
    require_merged_source: bool = True,
    document_source_item: tuple[str, str, str] | None = None,
) -> tuple[str, ...]:
    """SQL predicates that pin one projection row to a speaking subject.

    Every account-keyed catalog row carries its source evidence id, so an
    explicit subject scope is enforced by reading that lineage instead of
    storing a second subject column.  ``bind`` appends the value and returns
    its placeholder (``?`` for SQLite, ``$n`` for PostgreSQL); the returned
    predicates append their own parameters in order.

    ``evidence_table`` is the ledger table of the caller's store (SQLite keeps
    ``evidence_events``, PostgreSQL ``archive_evidence_events``); it is a code
    literal, never caller input.

    ``account_column`` is the account of the row being filtered.  Every hop in
    the lineage must stay inside that account -- the evidence row, the link row
    and the search document all carry their own ``account_id`` -- so a link that
    happens to reference a foreign or stale event id is treated as a missing
    source and hides the row instead of resolving a subject across accounts.

    A missing source event and a NULL ``subject_id`` both fail the comparison,
    so an unclaimed speaker never satisfies an explicit scope: in this project
    NULL means "no confirmed speaker", never "the account owner".

    ``merged_source_link`` is ``(link_table, link_column, owner_column)`` for a
    projection merged from several evidence rows (documents and episodes), and
    ``merged_source_extra`` adds further ``(link_column, owner_column)``
    equalities such as the account that owns the link row.  The row stays
    visible only while every linked source resolves to the subject; one
    missing, unclaimed or foreign source hides the whole row rather than
    partially rewriting it, so an item fed by one matching session and one
    foreign session never passes on its single matching source.
    ``require_merged_source=False`` covers sets where empty is legitimate (a
    person without aliases).

    ``document_source_item`` is ``(document_kind, item_id_column,
    account_id_column)`` for an item that also reaches the search projection:
    the document merged every event that ever wrote that item, and the whole
    set must belong to the subject too.  ``document_kind`` is a code literal,
    never caller input.
    """

    predicates = [
        f"(SELECT lineage.subject_id FROM {evidence_table} lineage"
        f" WHERE lineage.event_id = {single_source_column}"
        f" AND lineage.account_id = {account_column}) = {bind(subject_id)}"
    ]
    if merged_source_link is not None:
        link_table, link_column, owner_column = merged_source_link
        extra = "".join(
            f" AND link.{left} = {right}" for left, right in merged_source_extra
        )
        link_scope = (
            f"link.{link_column} = {owner_column}{extra}"
            f" AND link.account_id = {account_column}"
        )
        if require_merged_source:
            predicates.append(
                f"EXISTS (SELECT 1 FROM {link_table} link"
                f" WHERE {link_scope})"
            )
        predicates.append(
            f"NOT EXISTS (SELECT 1 FROM {link_table} link"
            f" LEFT JOIN {evidence_table} lineage"
            " ON lineage.event_id = link.source_event_id"
            " AND lineage.account_id = link.account_id"
            f" WHERE {link_scope}"
            " AND (lineage.subject_id IS NULL"
            f" OR lineage.subject_id <> {bind(subject_id)}))"
        )
    if document_source_item is not None:
        document_kind, item_id_column, account_id_column = document_source_item
        predicates.append(
            "NOT EXISTS (SELECT 1 FROM memory_search_documents document_link"
            " JOIN memory_search_document_sources link"
            " ON link.document_id = document_link.document_id"
            " AND link.account_id = document_link.account_id"
            f" LEFT JOIN {evidence_table} lineage"
            " ON lineage.event_id = link.source_event_id"
            " AND lineage.account_id = link.account_id"
            f" WHERE document_link.kind = '{document_kind}'"
            f" AND document_link.item_id = {item_id_column}"
            f" AND document_link.account_id = {account_id_column}"
            " AND (lineage.subject_id IS NULL"
            f" OR lineage.subject_id <> {bind(subject_id)}))"
        )
    return tuple(predicates)


DomainCategory = Literal[
    "life_story",
    "work_experience",
    "family_principle",
    "parenting_principle",
    "life_wisdom",
    "daily_life",
    "study_progress",
    "learning_preference",
]
MemoryCategory = DomainCategory
MemoryKind = Literal["semantic", "episodic", "procedural", "relationship"]
MemoryItemKind = Literal["claim", "episode", "knowledge", "skill"]
MemorySensitivity = Literal["public", "personal", "sensitive", "highly_sensitive"]
ConflictState = Literal["none", "potential", "active", "resolved"]
MemoryStatus = Literal["candidate", "confirmed", "disputed", "retracted"]
AccountWriteGuard = Callable[[str], AbstractAsyncContextManager[None]]


class AccountWriteRejectedError(RuntimeError):
    """A projection write arrived after durable account deletion started."""


@dataclass(frozen=True, slots=True)
class ExtractedClaim:
    domain_category: DomainCategory
    subject_key: str
    predicate: str
    value: str
    confidence: float
    sensitive_domain: str = "personal"
    entity_keys: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    salience: float = 0.5

    @property
    def category(self) -> DomainCategory:
        """Legacy read alias; new code must use domain_category."""
        return self.domain_category


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
    domain_category: DomainCategory
    event_start: datetime
    event_end: datetime | None = None
    time_precision: str = "conversation_time"
    canonical_key: str = ""
    participant_keys: tuple[str, ...] = ()
    salience: float = 0.6
    sensitivity: MemorySensitivity = "personal"

    @property
    def category(self) -> DomainCategory:
        """Legacy read alias; new code must use domain_category."""
        return self.domain_category


@dataclass(frozen=True, slots=True)
class ExtractedKnowledge:
    domain_category: DomainCategory
    question: str
    answer: str
    applicability: str = ""
    counterexample: str = ""
    entity_keys: tuple[str, ...] = ()
    salience: float = 0.55
    sensitivity: MemorySensitivity = "personal"

    @property
    def category(self) -> DomainCategory:
        """Legacy read alias; new code must use domain_category."""
        return self.domain_category


@dataclass(frozen=True, slots=True)
class ExtractionUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class MemoryExtraction:
    claims: tuple[ExtractedClaim, ...] = ()
    people: tuple[ExtractedPerson, ...] = ()
    relationships: tuple[ExtractedRelationship, ...] = ()
    timeline: tuple[ExtractedTimeline, ...] = ()
    knowledge: tuple[ExtractedKnowledge, ...] = ()
    extractor_version: str = ""
    usage: ExtractionUsage = ExtractionUsage()


class MemoryExtractor(Protocol):
    version: str

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction: ...


class MemoryEmbedder(Protocol):
    model: str
    dimensions: int

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
        subject_id: str | None = None,
    ) -> tuple[TimelineItem, ...]: ...

    async def people(
        self,
        *,
        account_id: str,
        limit: int = 100,
        subject_id: str | None = None,
    ) -> tuple[PersonItem, ...]: ...

    async def review_queue(
        self,
        *,
        account_id: str,
        subject_id: str | None = None,
    ) -> tuple[ReviewQueueItem, ...]: ...

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
    memory_kinds: tuple[MemoryKind, ...] = ()
    domain_categories: tuple[DomainCategory, ...] = ()
    categories: tuple[MemoryCategory, ...] = ()
    include_candidates: bool = True
    entity_ids: tuple[str, ...] = ()
    valid_at: datetime | None = None
    sensitivities: tuple[MemorySensitivity, ...] = ()
    conflict_states: tuple[ConflictState, ...] = ()
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None
    limit: int = 20
    subject_id: str | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not 1 <= self.limit <= 100:
            raise ValueError("memory search requires account_id and limit 1..100")
        if self.subject_id is not None and not self.subject_id.strip():
            raise ValueError("memory search subject_id must not be blank")
        if self.categories and self.domain_categories:
            raise ValueError("use domain_categories or legacy categories, not both")
        if self.categories:
            object.__setattr__(self, "domain_categories", self.categories)
        for value in (self.valid_at, self.occurred_after, self.occurred_before):
            if value is not None and value.tzinfo is None:
                raise ValueError("memory search timestamps must include timezone")
        if (
            self.occurred_after is not None
            and self.occurred_before is not None
            and self.occurred_after > self.occurred_before
        ):
            raise ValueError("occurred_after must not follow occurred_before")
        try:
            for entity_id in self.entity_ids:
                UUID(entity_id)
        except ValueError as exc:
            raise ValueError("memory search entity_ids must be UUID values") from exc


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
    memory_kind: MemoryKind = "semantic"
    domain_category: DomainCategory = "daily_life"
    entity_ids: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    observed_at: datetime | None = None
    stability: float = 0.0
    salience: float = 0.0
    sensitivity: MemorySensitivity = "personal"
    conflict_state: ConflictState = "none"

    def __post_init__(self) -> None:
        if self.domain_category == "daily_life" and self.category != "daily_life":
            object.__setattr__(self, "domain_category", self.category)
        if not self.source_event_ids and self.source_event_id:
            object.__setattr__(self, "source_event_ids", (self.source_event_id,))
        if self.observed_at is None:
            object.__setattr__(self, "observed_at", self.occurred_at)


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
    domain_category: DomainCategory = "daily_life"
    source_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.domain_category == "daily_life" and self.category != "daily_life":
            object.__setattr__(self, "domain_category", self.category)
        if not self.source_event_ids:
            object.__setattr__(self, "source_event_ids", (self.source_event_id,))


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
    memory_kind: MemoryKind = "semantic"
    domain_category: DomainCategory = "daily_life"
    conflict_state: ConflictState = "none"

    def __post_init__(self) -> None:
        if self.domain_category == "daily_life" and self.category != "daily_life":
            object.__setattr__(self, "domain_category", self.category)


@dataclass(frozen=True, slots=True)
class MemoryClaimReview:
    account_id: str
    claim_id: str
    action: Literal["confirm", "dispute", "retract", "correct"]
    corrected_value: str | None = None
    subject_id: str | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.claim_id.strip():
            raise ValueError("claim review requires account_id and claim_id")
        if self.subject_id is not None and not self.subject_id.strip():
            raise ValueError("claim review subject_id must not be blank")
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
