from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from packages.contracts.generated.python.multi_subject_contracts import AgeBand, SubjectCategory

from services.archive.domain import EvidenceEvent
from services.archive.memory_catalog import SubjectCategoryUnresolved

type IgnoredReason = Literal["minor_subject_skipped"]

type SubjectCategoryResolver = Callable[[EvidenceEvent], str | None | Awaitable[str | None]]

_MINOR_CATEGORIES: frozenset[str] = frozenset(
    {
        SubjectCategory.SUBJECT_CATEGORY_MINOR,
        *(frozenset(AgeBand) - {AgeBand.AGE_BAND_UNKNOWN, AgeBand.AGE_BAND_ADULT}),
    }
)


@dataclass(frozen=True, slots=True)
class Extract:
    subject_category: str | None

    def __post_init__(self) -> None:
        if self.subject_category in _MINOR_CATEGORIES:
            raise ValueError(f"a minor subject is never extracted: {self.subject_category!r}")


@dataclass(frozen=True, slots=True)
class Withhold:
    ignored_reason: IgnoredReason | None


type ExtractionGate = Extract | Withhold


async def extraction_gate(
    resolver: SubjectCategoryResolver, event: EvidenceEvent
) -> ExtractionGate:
    try:
        answer = resolver(event)
        category = await answer if inspect.isawaitable(answer) else answer
    except SubjectCategoryUnresolved:
        return Withhold(None)
    if category in _MINOR_CATEGORIES:
        return Withhold("minor_subject_skipped")
    return Extract(category)
