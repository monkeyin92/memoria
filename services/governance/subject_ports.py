"""Ports for deleting one bound subject's data without touching their account owner.

A device serves one person (2026-09-25). A child or elder with no account of
their own has their turns stored under the binding owner's account, attributed
to them by ``subject_id``. Account deletion is account-wide, so erasing that
person needs every store to answer, and act on, one narrower question: which
rows are this subject's inside this owner's account.

Attribution follows the subject export rule (``subject_export``): an evidence
row is the subject's only when it names them; a derived row is the subject's
when ANY of its source events is (deletion errs toward removing a merged
projection rather than keeping one that quotes the subject).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.archive.object_store import ObjectRef

__all__ = [
    "SubjectArchivePort",
    "SubjectGuardianPort",
    "SubjectMemoryScopePort",
    "SubjectScope",
]


@dataclass(frozen=True, slots=True)
class SubjectScope:
    """One subject inside one owner account."""

    #: The binding owner's account that stores the subject's device turns.
    account_id: str
    #: The subject's own person id (never an account id here).
    subject_id: str

    def __post_init__(self) -> None:
        for value, name in ((self.account_id, "account_id"), (self.subject_id, "subject_id")):
            if not value.strip() or len(value) > 128:
                raise ValueError(f"{name} must be a bounded non-empty id")
        if self.account_id == self.subject_id:
            raise ValueError("a subject-scoped deletion never targets the account itself")


class SubjectArchivePort(Protocol):
    """Archive + memory catalog (+ skills, persona/self-model sources) for one subject."""

    async def subject_event_ids(self, scope: SubjectScope) -> tuple[str, ...]:
        """Evidence ids in ``scope.account_id`` whose ``subject_id`` is the subject.

        Includes rows withheld from export (retention-withheld, unparsed) and
        every event that supersedes one of them.
        """
        ...

    async def subject_account_event_ids(self, subject_id: str) -> tuple[str, ...]:
        """Evidence stored under the subject's own id as account (crisis evidence).

        Excludes consent-ledger evidence (``guardian.person_consent_*``), which
        is retained as the audit of what was authorized.
        """
        ...

    async def object_references_for(
        self, *, account_id: str, event_ids: tuple[str, ...]
    ) -> tuple[ObjectRef, ...]:
        """Object-store blobs attached to exactly these evidence events."""
        ...

    async def delete_events(
        self, *, account_id: str, event_ids: tuple[str, ...]
    ) -> dict[str, int]:
        """Delete these events and everything derived from any of them.

        Merged projections (episodes, search documents, their vectors) that
        cite any of these events are deleted before the evidence, since the
        lineage join disappears with it. Idempotent: already-deleted ids are
        skipped. Returns per-table deleted counts.
        """
        ...

    async def remaining_rows_for(
        self, *, account_id: str, event_ids: tuple[str, ...], subject_id: str | None
    ) -> dict[str, int]:
        """Rows still attributable to these events (or ``subject_id``); empty when done."""
        ...


class SubjectGuardianPort(Protocol):
    """Guardian/tutor store rows for one subject (crisis, notifications, tutor)."""

    async def subject_tutor_event_ids(self, *, account_id: str, subject_id: str) -> tuple[str, ...]:
        """Archive evidence ids of the subject's tutor practice (archived without subject_id)."""
        ...

    async def delete_subject_rows(self, *, account_id: str, subject_id: str) -> dict[str, int]:
        """Delete crisis events + their notifications and the subject's tutor rows.

        Person consents are NOT deleted: revoked consents are the audit trail.
        """
        ...

    async def remaining_subject_rows(self, *, account_id: str, subject_id: str) -> dict[str, int]:
        ...


class SubjectMemoryScopePort(Protocol):
    """Subject-scoped memory records (append-only store) for one subject."""

    async def erase_subject(self, *, subject_id: str) -> dict[str, int]:
        """Remove (or irreversibly scrub) records, proposals, votes and outbox
        payloads that name the subject as subject or co-subject; keep only
        content-free audit rows."""
        ...

    async def remaining_subject_rows(self, *, subject_id: str) -> dict[str, int]:
        ...
