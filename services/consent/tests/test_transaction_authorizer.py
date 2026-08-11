"""Transaction-bound actor-exact authorization invokes one immediate callback."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import asyncpg
import pytest
import services.consent.transaction_authorizer as transaction_authorizer
from services.consent.evidence import ConsentSnapshot
from services.consent.tests.test_evidence import base_binding, base_evidence
from services.consent.transaction_authorizer import (
    ConsentFenceMismatchError,
    ExpectedConsentFence,
    TransactionBoundConsentAuthorizer,
)

NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


def current_rows(
    *,
    actor_id: str = "person_guardian",
    subject_id: str = "person_minor",
) -> tuple[dict[str, object], ...]:
    evidence = base_evidence(
        consent_id=f"consent-{actor_id}-{subject_id}",
        snapshot_id=f"snapshot-{actor_id}-{subject_id}",
        actor_id=actor_id,
        subject_id=subject_id,
        resource_owner_id=subject_id,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
    )
    snapshot = ConsentSnapshot(
        snapshot_id=evidence.snapshot_id,
        version=1,
        subject_id=subject_id,
        binding_id=evidence.binding_id,
        binding_version=evidence.binding_version,
        policy_version=evidence.policy_version,
        created_at=NOW - timedelta(minutes=1),
        grants=(evidence,),
        relationships=(),
        binding=base_binding(),
        canonical_hash="",
    )
    return (
        {
            "actor_id": actor_id,
            "subject_id": subject_id,
            "binding_id": evidence.binding_id,
            "binding_version": evidence.binding_version,
            "capability": evidence.capability,
            "purpose": evidence.purpose,
            "current_consent_id": evidence.consent_id,
            "current_revision": evidence.version,
            "current_hash": evidence.canonical_hash,
            "evidence_json": evidence.to_canonical_dict(),
        },
        {
            "actor_id": actor_id,
            "subject_id": subject_id,
            "binding_id": snapshot.binding_id,
            "binding_version": snapshot.binding_version,
            "current_snapshot_id": snapshot.snapshot_id,
            "current_revision": snapshot.version,
            "current_hash": snapshot.canonical_hash,
            "snapshot_json": snapshot.to_canonical_dict(),
        },
        {"evidence_json": evidence.to_canonical_dict()},
        {"snapshot_json": snapshot.to_canonical_dict()},
    )


def expected(
    rows: tuple[dict[str, object], ...],
    *,
    request_actor_id: str | None = None,
) -> ExpectedConsentFence:
    head, snapshot_head, _, _ = rows
    return ExpectedConsentFence(
        request_actor_id=request_actor_id or str(head["actor_id"]),
        evidence_actor_id=str(head["actor_id"]),
        consent_id=str(head["current_consent_id"]),
        revision=cast(int, head["current_revision"]),
        canonical_hash=str(head["current_hash"]),
        current_snapshot_id=str(snapshot_head["current_snapshot_id"]),
        current_snapshot_revision=cast(int, snapshot_head["current_revision"]),
        current_snapshot_hash=str(snapshot_head["current_hash"]),
        subject_id=str(head["subject_id"]),
        binding_id=str(head["binding_id"]),
        binding_version=cast(int, head["binding_version"]),
        capability=str(head["capability"]),  # type: ignore[arg-type]
        purpose=str(head["purpose"]),  # type: ignore[arg-type]
    )


class FakeConnection:
    def __init__(
        self,
        rows_by_actor_subject: dict[
            tuple[str, str], tuple[dict[str, object], ...]
        ],
        *,
        in_transaction: bool = True,
        authorized_requests: set[tuple[str, str, str]] | None = None,
    ) -> None:
        self.rows_by_actor_subject = rows_by_actor_subject
        self.in_transaction = in_transaction
        self.locked_keys: list[tuple[str, str]] = []
        self.lock_sequence: list[str] = []
        self.authorized_requests = authorized_requests or {
            (actor, subject, str(rows[0]["binding_id"]))
            for (actor, subject), rows in rows_by_actor_subject.items()
        }

    def is_in_transaction(self) -> bool:
        return self.in_transaction

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        if "consent_lock_authority_head" in query:
            request_actor_id = str(args[0])
            key = (str(args[1]), str(args[2]))
            self.locked_keys.append(key)
            self.lock_sequence.append(f"consent:{key[0]}:{key[1]}")
            rows = self.rows_by_actor_subject.get(key)
            if (request_actor_id, key[1], str(args[3])) not in self.authorized_requests:
                return None
            return rows[0] if rows else None
        if "consent_lock_snapshot_head" in query:
            request_actor_id = str(args[0])
            subject_id = str(args[1])
            binding_id = str(args[2])
            self.lock_sequence.append(f"snapshot:{subject_id}:{binding_id}")
            if (request_actor_id, subject_id, binding_id) not in self.authorized_requests:
                return None
            rows = next(
                (
                    candidate
                    for (_actor, subject), candidate in self.rows_by_actor_subject.items()
                    if subject == subject_id
                ),
                None,
            )
            return rows[1] if rows else None
        subject_id = str(args[0])
        rows = next(
            (
                candidate
                for (actor, subject), candidate in self.rows_by_actor_subject.items()
                if subject == subject_id
            ),
            None,
        )
        if "FROM consent_evidence" in query:
            return rows[2] if rows else None
        if "FROM consent_snapshot" in query:
            return rows[3] if rows else None
        raise AssertionError(f"unexpected query: {query}")


@pytest.mark.parametrize("field", ["request_actor_id", "evidence_actor_id"])
@pytest.mark.parametrize("actor_id", ["", "   "])
def test_expected_fence_requires_both_actor_identities(field: str, actor_id: str) -> None:
    rows = current_rows()
    with pytest.raises(ValueError, match=field):
        replace(expected(rows), **{field: actor_id})


@pytest.mark.parametrize(
    "field", ["revision", "current_snapshot_revision", "binding_version"]
)
@pytest.mark.parametrize("value", [True, False, 1.0, "1"])
def test_expected_fence_positive_integers_are_strict(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        replace(expected(current_rows()), **{field: value})


def test_public_surface_has_no_reusable_locked_fact_or_lock_method() -> None:
    assert not hasattr(transaction_authorizer, "LockedConsentFacts")
    assert not hasattr(TransactionBoundConsentAuthorizer(), "lock_current")


@pytest.mark.asyncio
async def test_requires_an_already_open_caller_transaction() -> None:
    rows = current_rows()
    conn = FakeConnection({("person_guardian", "person_minor"): rows}, in_transaction=False)

    async def operation(connection: asyncpg.Connection) -> str:
        del connection
        return "effect"

    with pytest.raises(RuntimeError, match="caller transaction"):
        await TransactionBoundConsentAuthorizer().execute_with_authority(
            cast(asyncpg.Connection, cast(Any, conn)),
            expected=(expected(rows),),
            now=NOW,
            operation=operation,
        )
    assert conn.locked_keys == []


@pytest.mark.asyncio
async def test_actor_is_part_of_stable_lock_key_and_callback_uses_same_connection() -> None:
    rows_z = current_rows(actor_id="actor-z", subject_id="subject-a")
    rows_a = current_rows(actor_id="actor-a", subject_id="subject-z")
    conn = FakeConnection(
        {("actor-z", "subject-a"): rows_z, ("actor-a", "subject-z"): rows_a}
    )
    callback_connections: list[object] = []

    async def operation(connection: asyncpg.Connection) -> str:
        callback_connections.append(connection)
        return "persisted-effect"

    result = await TransactionBoundConsentAuthorizer().execute_with_authority(
        cast(asyncpg.Connection, cast(Any, conn)),
        expected=(expected(rows_z), expected(rows_a)),
        now=NOW,
        operation=operation,
    )
    assert conn.locked_keys == [("actor-a", "subject-z"), ("actor-z", "subject-a")]
    assert all(item.startswith("consent:") for item in conn.lock_sequence[:2])
    assert all(item.startswith("snapshot:") for item in conn.lock_sequence[2:])
    assert callback_connections == [conn]
    assert result == "persisted-effect"


@pytest.mark.asyncio
async def test_minor_request_actor_can_consume_guardian_evidence_when_authorized() -> None:
    rows = current_rows(actor_id="guardian", subject_id="minor")
    conn = FakeConnection(
        {("guardian", "minor"): rows},
        authorized_requests={("minor", "minor", "bd_1")},
    )

    async def operation(connection: asyncpg.Connection) -> str:
        assert connection is conn
        return "minor-effect"

    assert (
        await TransactionBoundConsentAuthorizer().execute_with_authority(
            cast(asyncpg.Connection, cast(Any, conn)),
            expected=(expected(rows, request_actor_id="minor"),),
            now=NOW,
            operation=operation,
        )
        == "minor-effect"
    )


@pytest.mark.asyncio
async def test_current_global_snapshot_may_contain_historical_evidence_snapshot() -> None:
    rows = current_rows(actor_id="guardian", subject_id="minor")
    historical_evidence = rows[0]["evidence_json"]
    assert isinstance(historical_evidence, dict)
    assert historical_evidence["snapshot_id"] == "snapshot-guardian-minor"
    current_snapshot = ConsentSnapshot(
        snapshot_id="snapshot-global-current",
        version=2,
        subject_id="minor",
        binding_id="bd_1",
        binding_version=1,
        policy_version="policy-v2",
        created_at=NOW,
        grants=(
            transaction_authorizer.ConsentEvidence.from_canonical_dict(
                historical_evidence
            ),
        ),
        relationships=(),
        binding=base_binding(),
    )
    rows[1].update(
        {
            "current_snapshot_id": current_snapshot.snapshot_id,
            "current_revision": current_snapshot.version,
            "current_hash": current_snapshot.canonical_hash,
            "snapshot_json": current_snapshot.to_canonical_dict(),
        }
    )
    conn = FakeConnection({("guardian", "minor"): rows})
    called = False

    async def operation(connection: asyncpg.Connection) -> str:
        nonlocal called
        assert connection is conn
        called = True
        return "accepted"

    assert (
        await TransactionBoundConsentAuthorizer().execute_with_authority(
            cast(asyncpg.Connection, cast(Any, conn)),
            expected=(expected(rows),),
            now=NOW,
            operation=operation,
        )
        == "accepted"
    )
    assert called is True

@pytest.mark.asyncio
async def test_unauthorized_request_actor_cannot_borrow_another_evidence_actor() -> None:
    rows = current_rows(actor_id="actor-B", subject_id="subject-B")
    conn = FakeConnection(
        {("actor-B", "subject-B"): rows},
        authorized_requests={("actor-B", "subject-B", "bd_1")},
    )
    called = False

    async def operation(connection: asyncpg.Connection) -> str:
        nonlocal called
        del connection
        called = True
        return "forged"

    with pytest.raises(ConsentFenceMismatchError, match="authority head missing"):
        await TransactionBoundConsentAuthorizer().execute_with_authority(
            cast(asyncpg.Connection, cast(Any, conn)),
            expected=(expected(rows, request_actor_id="actor-A"),),
            now=NOW,
            operation=operation,
        )
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["revision", "canonical_hash", "current_snapshot_hash"]
)
async def test_stale_revision_or_hash_rejects_before_callback(field: str) -> None:
    rows = current_rows()
    fence = expected(rows)
    replacement: object = 2 if field == "revision" else "f" * 64
    stale = replace(fence, **{field: replacement})
    conn = FakeConnection({("person_guardian", "person_minor"): rows})
    called = False

    async def operation(connection: asyncpg.Connection) -> str:
        nonlocal called
        del connection
        called = True
        return "effect"

    with pytest.raises(ConsentFenceMismatchError, match=field):
        await TransactionBoundConsentAuthorizer().execute_with_authority(
            cast(asyncpg.Connection, cast(Any, conn)),
            expected=(stale,),
            now=NOW,
            operation=operation,
        )
    assert called is False
