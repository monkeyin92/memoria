from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from packages.contracts.generated.python.multi_subject_contracts import AgeBand, SubjectCategory

from services.archive.domain import EvidenceEvent
from services.archive.memory_catalog import SubjectCategoryUnresolved

type IgnoredReason = Literal["minor_subject_skipped", "not_explicit_adult"]

type SubjectCategoryResolver = Callable[[EvidenceEvent], str | None | Awaitable[str | None]]

_MINOR_CATEGORIES: frozenset[str] = frozenset(
    {
        SubjectCategory.SUBJECT_CATEGORY_MINOR,
        *(frozenset(AgeBand) - {AgeBand.AGE_BAND_UNKNOWN, AgeBand.AGE_BAND_ADULT}),
    }
)
_EXPLICIT_ADULT = SubjectCategory.SUBJECT_CATEGORY_ADULT


@dataclass(frozen=True, slots=True)
class Extract:
    subject_category: Literal["adult"]

    def __post_init__(self) -> None:
        if self.subject_category != _EXPLICIT_ADULT:
            raise ValueError(f"only an explicit adult is extracted: {self.subject_category!r}")


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
    if category == _EXPLICIT_ADULT:
        return Extract("adult")
    if category in _MINOR_CATEGORIES:
        return Withhold("minor_subject_skipped")
    return Withhold("not_explicit_adult")
