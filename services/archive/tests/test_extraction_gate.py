from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest
from packages.contracts.generated.python.multi_subject_contracts import AgeBand, SubjectCategory
from services.archive.domain import EvidenceEvent
from services.archive.extraction_gate import Extract, IgnoredReason, Withhold, extraction_gate
from services.archive.memory_catalog import SubjectCategoryUnresolved

SCHEMA = Path(__file__).resolve().parents[1] / "postgres_memory_schema.sql"
EVENT = EvidenceEvent(
    event_id="gate-event",
    account_id="gate-account",
    subject_id="gate-subject",
    session_id="gate-session",
    turn_id=1,
    generation_id=1,
    event_type="speech.utterance_finalized",
    occurred_at=datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
    speaker_class="owner",
    source="test",
    payload={"text": "gate"},
)
MINOR_SKIPPED = Withhold("minor_subject_skipped")
NOT_ADULT = Withhold("not_explicit_adult")
GATE_BY_CONTRACT_VALUE = {
    None: NOT_ADULT,
    "unknown": NOT_ADULT,
    "adult": Extract("adult"),
    "minor": MINOR_SKIPPED,
    "under_14": MINOR_SKIPPED,
    "14_17": MINOR_SKIPPED,
}


def test_every_contract_value_has_a_pinned_gate() -> None:
    assert set(GATE_BY_CONTRACT_VALUE) == {None, *SubjectCategory.values(), *AgeBand.values()}


@pytest.mark.parametrize(("value", "expected"), list(GATE_BY_CONTRACT_VALUE.items()))
async def test_the_gate_decides_each_contract_value(
    value: str | None, expected: Extract | Withhold
) -> None:
    assert await extraction_gate(lambda _event: value, EVENT) == expected


@pytest.mark.parametrize("value", ["", "teen", "Minor", " minor", "unknown"])
async def test_a_label_that_is_not_explicit_adult_is_withheld(value: str) -> None:
    assert await extraction_gate(lambda _event: value, EVENT) == NOT_ADULT


@pytest.mark.parametrize("value", ["minor", "under_14", "14_17", "unknown", "teen", None])
def test_extract_accepts_only_an_explicit_adult(value: str | None) -> None:
    with pytest.raises(ValueError):
        Extract(value)  # type: ignore[arg-type]


async def test_an_awaitable_resolver_is_awaited() -> None:
    async def child(_event: EvidenceEvent) -> str | None:
        return "under_14"

    async def adult(_event: EvidenceEvent) -> str | None:
        return "adult"

    assert await extraction_gate(child, EVENT) == MINOR_SKIPPED
    assert await extraction_gate(adult, EVENT) == Extract("adult")


async def test_an_unresolved_subject_is_withheld_without_a_reason() -> None:
    def refuse(event: EvidenceEvent) -> str | None:
        raise SubjectCategoryUnresolved(event.subject_id)

    async def refuse_later(event: EvidenceEvent) -> str | None:
        raise SubjectCategoryUnresolved(event.subject_id)

    assert await extraction_gate(refuse, EVENT) == Withhold(None)
    assert await extraction_gate(refuse_later, EVENT) == Withhold(None)


async def test_other_resolver_errors_propagate() -> None:
    def broken(_event: EvidenceEvent) -> str | None:
        raise RuntimeError("identity bug")

    with pytest.raises(RuntimeError):
        await extraction_gate(broken, EVENT)


def test_ignored_reasons_match_the_receipt_check_constraint() -> None:
    constraint = re.search(
        r"ADD CONSTRAINT memory_compile_receipts_ignored_reason_check CHECK \((?P<body>.*?)\);",
        SCHEMA.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert constraint is not None
    allowed = re.search(r"ignored_reason IN \((?P<values>[^)]*)\)", constraint["body"])
    assert allowed is not None

    assert set(re.findall(r"'([^']*)'", allowed["values"])) == set(
        get_args(IgnoredReason.__value__)
    )
