"""In-memory adapter for the memory scope store seam.

Development / test fixture only (section 11.7: not the production
authority).  Every read call carries the caller's ``actor_subject_id`` the
same way the PostgreSQL adapter does; an empty actor is rejected so the
fail-closed contract is exercised in tests too.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from services.memory_scope.domain import (
    AlreadyVotedError,
    ConfirmationVote,
    CrossFamilyAccessError,
    MemoryAuditEvent,
    MemoryOutboxEvent,
    MemoryRecord,
    MemoryRecordStatusEvent,
    MemoryScope,
    ProposalStatus,
    SharedMemoryProposal,
    WriteFenceMissingError,
)

#: Explicit marker: this adapter must never back production traffic.
DEV_TEST_ONLY: bool = True


class InMemoryMemoryStore:
    """Thread-safe dev fixture; mirrors the SQLite/PostgreSQL semantics.

    ``_records`` stores the immutable row and ``_status_events`` derives the
    current status, so the store behaves append-only exactly like the
    production adapters.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, MemoryRecord] = {}
        self._status_events: dict[str, list[MemoryRecordStatusEvent]] = {}
        self._proposals: dict[str, SharedMemoryProposal] = {}
        self._votes: dict[str, list[ConfirmationVote]] = {}
        self._outbox: dict[str, MemoryOutboxEvent] = {}
        self._outbox_state: dict[str, str] = {}
        self._audit: list[MemoryAuditEvent] = []
        self.initialized: bool = False

    async def initialize(self) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.initialized = False

    # -- records ---------------------------------------------------------

    async def persist_record(
        self,
        record: MemoryRecord,
        *,
        actor_family_space_id: str | None,
        status_events: tuple[MemoryRecordStatusEvent, ...] = (),
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
        connection: object | None = None,
    ) -> None:
        _require_actor(record.created_by_actor_id)
        if record.family_space_id is not None:
            if actor_family_space_id != record.family_space_id:
                raise WriteFenceMissingError(
                    "record family scope must match the authoritative actor "
                    "family scope (fail closed)"
                )
        with self._lock:
            self._records[record.record_id] = record
            for event in status_events:
                self._status_events.setdefault(event.record_id, []).append(event)
            self._append_events(outbox, audit)

    async def get_record(
        self,
        record_id: str,
        *,
        actor_subject_id: str,
        actor_family_space_id: str | None = None,
        grant_owner_id: str | None = None,
        grant_scope: str | None = None,
    ) -> MemoryRecord | None:
        _require_actor(actor_subject_id)
        with self._lock:
            record = self._records.get(record_id)
            if record is not None and not _readable_as(
                record,
                actor_subject_id,
                actor_family_space_id,
                grant_owner_id,
                grant_scope,
            ):
                return None
            events = self._status_events.get(record_id, [])
            return _apply_status(record, events) if record is not None else None

    async def list_records_for_subject(
        self,
        subject_id: str,
        scopes: tuple[MemoryScope, ...] | None = None,
        *,
        actor_subject_id: str,
        actor_family_space_id: str | None = None,
        grant_owner_id: str | None = None,
        grant_scope: str | None = None,
        include_revoked: bool = False,
    ) -> tuple[MemoryRecord, ...]:
        _require_actor(actor_subject_id)
        with self._lock:
            return self._select(
                lambda record: _belongs_to(record, subject_id),
                scopes,
                include_revoked=include_revoked,
                readable=lambda record: _readable_as(
                    record,
                    actor_subject_id,
                    actor_family_space_id,
                    grant_owner_id,
                    grant_scope,
                ),
            )

    async def list_records_in_family(
        self,
        family_space_id: str,
        subject_id: str,
        scopes: tuple[MemoryScope, ...] | None = None,
        *,
        actor_subject_id: str,
        actor_family_space_id: str | None,
        include_revoked: bool = False,
    ) -> tuple[MemoryRecord, ...]:
        _require_actor(actor_subject_id)
        if actor_family_space_id is not None and actor_family_space_id != family_space_id:
            raise CrossFamilyAccessError(
                f"actor family {actor_family_space_id} does not match "
                f"family {family_space_id}"
            )
        with self._lock:
            return self._select(
                lambda record: record.family_space_id == family_space_id
                and _belongs_to(record, subject_id),
                scopes,
                include_revoked=include_revoked,
            )

    def _select(
        self,
        predicate: Callable[[MemoryRecord], bool],
        scopes: tuple[MemoryScope, ...] | None,
        *,
        include_revoked: bool,
        readable: Callable[[MemoryRecord], bool] | None = None,
    ) -> tuple[MemoryRecord, ...]:
        allowed = set(scopes) if scopes is not None else None
        result: list[MemoryRecord] = []
        for record in self._records.values():
            derived = _apply_status(
                record, self._status_events.get(record.record_id, [])
            )
            if allowed is not None and derived.scope not in allowed:
                continue
            if not include_revoked and not derived.is_visible():
                continue
            if readable is not None and not readable(derived):
                continue
            if predicate(derived):
                result.append(derived)
        result.sort(key=lambda item: item.created_at)
        return tuple(result)

    async def get_status_events(
        self, record_id: str, *, actor_subject_id: str
    ) -> tuple[MemoryRecordStatusEvent, ...]:
        _require_actor(actor_subject_id)
        with self._lock:
            return tuple(self._status_events.get(record_id, ()))

    # -- proposals ---------------------------------------------------------

    async def persist_proposal(
        self,
        proposal: SharedMemoryProposal,
        *,
        actor_family_space_id: str | None,
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
    ) -> None:
        _require_actor(proposal.proposer_subject_id)
        if proposal.family_space_id != actor_family_space_id:
            raise WriteFenceMissingError(
                "proposal family scope must match the authoritative actor "
                "family scope (fail closed)"
            )
        with self._lock:
            self._proposals[proposal.proposal_id] = proposal
            self._append_events(outbox, audit)

    async def get_proposal(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        actor_family_space_id: str,
    ) -> SharedMemoryProposal | None:
        _require_actor(actor_subject_id)
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None or proposal.family_space_id != actor_family_space_id:
                return None
            return proposal

    async def update_proposal_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        *,
        actor_subject_id: str,
        resolved_at: datetime | None = None,
    ) -> None:
        _require_actor(actor_subject_id)
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                return
            self._proposals[proposal_id] = replace(
                proposal,
                status=status,
                resolved_at=resolved_at if resolved_at is not None else proposal.resolved_at,
            )

    async def persist_vote(
        self,
        vote: ConfirmationVote,
        *,
        proposal_status: ProposalStatus | None = None,
        resolved_at: datetime | None = None,
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
    ) -> None:
        with self._lock:
            votes = self._votes.setdefault(vote.proposal_id, [])
            if any(
                existing.subject_id == vote.subject_id
                and existing.decision == vote.decision
                for existing in votes
            ):
                raise AlreadyVotedError(
                    f"vote already recorded for {vote.subject_id}"
                )
            votes.append(vote)
            if proposal_status is not None:
                proposal = self._proposals.get(vote.proposal_id)
                if proposal is not None:
                    self._proposals[vote.proposal_id] = replace(
                        proposal,
                        status=proposal_status,
                        resolved_at=resolved_at if resolved_at is not None else proposal.resolved_at,
                    )
            self._append_events(outbox, audit)

    async def vote_and_transition(
        self,
        vote: ConfirmationVote,
        *,
        actor_family_space_id: str | None,
        audit: tuple[MemoryAuditEvent, ...],
        now: datetime,
    ) -> str:
        """P0-F/P0-6: the authoritative proposal row is read inside the
        transaction - the caller's view can never influence promotion."""
        with self._lock:
            votes = self._votes.setdefault(vote.proposal_id, [])
            if any(
                existing.subject_id == vote.subject_id
                and existing.decision == vote.decision
                for existing in votes
            ):
                raise AlreadyVotedError(
                    f"vote already recorded for {vote.subject_id}"
                )
            current = self._proposals.get(vote.proposal_id)
            if current is None:
                return "terminal"
            if vote.decision == "confirm" and current.status != "pending":
                return "terminal"
            if vote.decision == "object" and current.status not in (
                "pending",
                "approvals_complete",
            ):
                return "terminal"
            proposal = current
            if vote.subject_id not in proposal.all_confirmable_subjects:
                from services.memory_scope.domain import NotAuthorizedError

                raise NotAuthorizedError(
                    f"{vote.subject_id} is not a co-subject of this memory"
                )
            votes.append(vote)
            confirmable = set(proposal.all_confirmable_subjects)
            # Append-only superseding votes (main review): the LATEST
            # decision per subject wins - a subject that confirmed may
            # still object before the promotion (append-only record, both
            # rows kept), so approvals_complete never locks objection out.
            latest_by_subject: dict[str, ConfirmationVote] = {}
            for existing in votes:
                latest_by_subject[existing.subject_id] = existing
            objectors = {
                subject
                for subject, v in latest_by_subject.items()
                if v.decision == "object"
            }
            confirmers = {
                subject
                for subject, v in latest_by_subject.items()
                if v.decision == "confirm"
            }
            result_status: str
            if objectors:
                result_status = "frozen"
                self._proposals[vote.proposal_id] = replace(
                    current, status="frozen", resolved_at=now
                )
                self._outbox[f"{vote.proposal_id}:frozen"] = MemoryOutboxEvent(
                    outbox_id=f"{vote.proposal_id}:frozen",
                    event_id=f"{vote.proposal_id}:frozen",
                    topic="memory.shared.frozen",
                    payload={
                        "proposal_id": vote.proposal_id,
                        "objected_by": sorted(objectors),
                    },
                    created_at=now,
                )
            elif confirmers == confirmable:
                # Vote-acceptance transaction (three authority actions (proposal / per-vote approval / final promotion)): vote is
                # appended but NOT promoted; the proposal reports
                # ``all_confirmed`` and the separate promotion finalizer
                # performs the CAS promotion with a DISTINCT fresh
                # family_shared_memory_promotion receipt.
                result_status = "approvals_complete"
                self._proposals[vote.proposal_id] = replace(
                    current, status="approvals_complete", resolved_at=now
                )
            else:
                result_status = "pending"
            self._audit.extend(audit)
            return result_status

    async def freeze_proposal_atomically(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        family_space_id: str,
        reason: str,
        audit: tuple[MemoryAuditEvent, ...],
        outbox: tuple[MemoryOutboxEvent, ...],
        now: datetime,
    ) -> bool:
        """Fail-closed freeze: a pending proposal whose authorization no
        longer holds moves to ``frozen`` atomically with the stable
        audit/outbox events and never promotes a record; terminal proposals
        are idempotent no-ops."""
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None or proposal.status not in (
                "pending",
                "approvals_complete",
            ):
                return False
            self._proposals[proposal_id] = replace(
                proposal,
                status="frozen",
                resolved_at=now,
            )
            self._append_events(outbox, audit)
            return True

    async def finalize_promotion_atomically(
        self,
        proposal_id: str,
        *,
        promotion_receipt_id: str,
        promotion_fence_context_hash: str,
        required_subject_ids: tuple[str, ...],
        approval_revisions: tuple[tuple[str, str, int, str], ...],
        actor_subject_id: str,
        family_space_id: str,
        audit: tuple[MemoryAuditEvent, ...],
        now: datetime,
    ) -> bool:
        """Promotion finalizer CAS (three authority actions (proposal / per-vote approval / final promotion)): re-lock the proposal
        and its vote set; promote only when still pending with the full
        confirm set and no objection.  At most one record is produced."""
        with self._lock:
            current = self._proposals.get(proposal_id)
            if current is None or current.status != "approvals_complete":
                return False
            votes = self._votes.get(proposal_id, ())
            confirmable = set(current.all_confirmable_subjects)
            latest_by_subject: dict[str, ConfirmationVote] = {}
            for vote in votes:
                latest_by_subject[vote.subject_id] = vote
            objectors = {
                subject
                for subject, vote in latest_by_subject.items()
                if vote.decision == "object"
            }
            confirmers = {
                subject
                for subject, vote in latest_by_subject.items()
                if vote.decision == "confirm"
            }
            if objectors or confirmers != confirmable:
                return False
            persisted_approvals = tuple(
                sorted(
                    (
                        latest.subject_id,
                        latest.approval_snapshot_id,
                        latest.approval_snapshot_revision,
                        latest.approval_snapshot_hash,
                    )
                    for latest in latest_by_subject.values()
                    if latest.decision == "confirm"
                )
            )
            expected_approvals = tuple(sorted(approval_revisions))
            if persisted_approvals != expected_approvals or any(
                not snapshot_id
                for _, snapshot_id, _, _ in persisted_approvals
            ):
                return False
            existing = [
                record
                for record in self._records.values()
                if record.shared_proposal_id == proposal_id
            ]
            if existing:
                return False
            self._proposals[proposal_id] = replace(
                current, status="promoted", resolved_at=now
            )
            record_id = str(uuid.uuid4())
            record = MemoryRecord(
                record_id=record_id,
                scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
                subject_id=current.proposer_subject_id,
                # Canonical ownership (main review): a family-shared record
                # is owned by the FAMILY SPACE (the verified promotion
                # authorization's expected_resource_owner_id), never by a
                # single proposer subject - receipt and row must agree.
                resource_owner_id=current.family_space_id,
                family_space_id=current.family_space_id,
                co_subject_ids=current.co_subject_ids,
                source_evidence_ids=current.source_evidence_ids,
                policy_receipt_id=current.proposal_policy_receipt_id,
                promotion_receipt_id=promotion_receipt_id,
                promotion_fence_context_hash=promotion_fence_context_hash,
                approval_evidence_refs=tuple(
                    (
                        vote.subject_id,
                        vote.approval_receipt_id,
                        vote.approval_snapshot_id,
                        vote.approval_snapshot_revision,
                        vote.approval_snapshot_hash,
                    )
                    for vote in votes
                    if vote.decision == "confirm"
                ),
                consent_snapshot_id=current.consent_snapshot_id,
                status="confirmed",
                payload={"title": current.title, "content": current.content},
                created_by_actor_id=actor_subject_id,
                created_at=now,
                shared_proposal_id=proposal_id,
            )
            self._records[record_id] = record
            self._status_events.setdefault(record_id, []).append(
                MemoryRecordStatusEvent(
                    event_id=f"{record_id}:confirmed",
                    record_id=record_id,
                    status="confirmed",
                    reason_code="all_co_subjects_confirmed",
                    created_at=now,
                )
            )
            self._outbox[f"{proposal_id}:confirmed"] = MemoryOutboxEvent(
                outbox_id=f"{proposal_id}:confirmed",
                event_id=f"{proposal_id}:confirmed",
                topic="memory.shared.confirmed",
                payload={
                    "proposal_id": proposal_id,
                    "record_id": record_id,
                    "family_space_id": current.family_space_id,
                    "promotion_receipt_id": promotion_receipt_id,
                },
                created_at=now,
            )
            self._audit.extend(audit)
            return True

    async def list_votes(
        self, proposal_id: str, *, actor_subject_id: str
    ) -> tuple[ConfirmationVote, ...]:
        _require_actor(actor_subject_id)
        with self._lock:
            return tuple(self._votes.get(proposal_id, ()))

    # -- test helpers ------------------------------------------------------

    def audit_events(self) -> tuple[MemoryAuditEvent, ...]:
        with self._lock:
            return tuple(sorted(self._audit, key=lambda item: item.created_at))

    def outbox_events(self) -> tuple[MemoryOutboxEvent, ...]:
        with self._lock:
            return tuple(
                sorted(self._outbox.values(), key=lambda item: item.created_at)
            )

    async def list_proposals_for_subject(
        self,
        subject_id: str,
        statuses: tuple[ProposalStatus, ...] | None = None,
        *,
        actor_subject_id: str,
        actor_family_space_id: str,
    ) -> tuple[SharedMemoryProposal, ...]:
        _require_actor(actor_subject_id)
        with self._lock:
            allowed = set(statuses) if statuses is not None else None
            result = [
                proposal
                for proposal in self._proposals.values()
                if proposal.family_space_id == actor_family_space_id
                and subject_id in proposal.all_confirmable_subjects
                and (allowed is None or proposal.status in allowed)
            ]
            result.sort(key=lambda item: item.created_at)
            return tuple(result)

    async def persist_withdrawal(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        record_id: str | None = None,
        status_events: tuple[MemoryRecordStatusEvent, ...] = (),
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
    ) -> None:
        _require_actor(actor_subject_id)
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is not None:
                self._proposals[proposal_id] = replace(
                    proposal,
                    status="withdrawn",
                    resolved_at=datetime.now(UTC),
                )
            for event in status_events:
                self._status_events.setdefault(event.record_id, []).append(event)
            self._append_events(outbox, audit)

    # -- outbox / audit ---------------------------------------------------

    async def append_outbox(self, event: MemoryOutboxEvent) -> None:
        with self._lock:
            self._outbox[event.outbox_id] = event
            self._outbox_state[event.outbox_id] = "pending"

    async def append_audit(self, event: MemoryAuditEvent) -> None:
        with self._lock:
            self._audit.append(event)

    async def list_pending_outbox(self, limit: int = 100) -> tuple[MemoryOutboxEvent, ...]:
        with self._lock:
            pending = [
                event
                for outbox_id, event in self._outbox.items()
                if self._outbox_state.get(outbox_id) == "pending"
            ]
            pending.sort(key=lambda item: item.created_at)
            return tuple(pending[:limit])

    async def mark_outbox_processed(self, outbox_id: str) -> None:
        with self._lock:
            if outbox_id in self._outbox:
                self._outbox_state[outbox_id] = "processed"

    def _append_events(
        self,
        outbox: tuple[MemoryOutboxEvent, ...],
        audit: tuple[MemoryAuditEvent, ...],
    ) -> None:
        for event in outbox:
            self._outbox[event.outbox_id] = event
            self._outbox_state[event.outbox_id] = "pending"
        self._audit.extend(audit)


def _belongs_to(record: MemoryRecord, subject_id: str) -> bool:
    return (
        record.subject_id == subject_id
        or record.resource_owner_id == subject_id
        or subject_id in record.co_subject_ids
    )


def _readable_as(
    record: MemoryRecord,
    actor_subject_id: str,
    actor_family_space_id: str | None,
    grant_owner_id: str | None,
    grant_scope: str | None,
) -> bool:
    """Mirror of the PostgreSQL RLS semantics (fifth review): family rows
    need a matching non-empty family context; grant reads are pinned to the
    granted owner + scope."""
    if grant_owner_id is not None and grant_scope is not None:
        return (
            record.resource_owner_id == grant_owner_id
            and record.scope.value == grant_scope
        )
    if record.family_space_id is not None:
        if actor_family_space_id != record.family_space_id:
            return False
    return (
        record.subject_id == actor_subject_id
        or record.resource_owner_id == actor_subject_id
        or actor_subject_id in record.co_subject_ids
    )


def _require_actor(actor_subject_id: str) -> None:
    if not actor_subject_id or not actor_subject_id.strip():
        raise WriteFenceMissingError(
            "store reads require the caller's actor_subject_id (fail closed)"
        )


def _apply_status(
    record: MemoryRecord, events: Sequence[MemoryRecordStatusEvent]
) -> MemoryRecord:
    """Derive the current status from the append-only event stream."""
    if not events:
        return record
    latest = events[-1]
    withdrawn_at = latest.created_at if latest.status == "revoked" else None
    return replace(
        record,
        status=latest.status,
        withdrawn_at=withdrawn_at,
        updated_at=latest.created_at,
    )
