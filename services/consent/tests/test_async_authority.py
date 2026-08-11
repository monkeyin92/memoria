"""Async authority smoke test over an in-memory async adapter.

The PostgreSQL adapter is the production async store (live-tested only with
``MEMORIA_TEST_POSTGRES_DSN``); this adapter proves the async orchestration
path (grant / revoke / idempotent replay / rollback) without a database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.consent.authority import (
    AsyncConsentAuthority,
    AsyncEvidenceResolverPort,
    SubjectProof,
)
from services.consent.evidence import (
    BindingEvidence,
    ConsentOffer,
    ConsentParams,
    RelationshipEvidence,
)
from services.consent.in_memory_store import InMemoryConsentStore
from services.consent.store import (
    AsyncConsentStorePort,
    AsyncConsentUnitOfWork,
    ConsentConflictError,
    ConsentStorePort,
    ConsentUnitOfWork,
)


class AsyncInMemoryAdapter:
    """Thin async wrapper over the synchronous in-memory store."""

    def __init__(self, inner: ConsentStorePort) -> None:
        self._inner = inner

    async def transaction(self) -> AsyncConsentUnitOfWork:
        return _AsyncUow(self._inner.transaction())

    async def close(self) -> None:
        self._inner.close()


class _AsyncUow:
    def __init__(self, inner: ConsentUnitOfWork) -> None:
        self._inner = inner

    async def latest_offer(self, offer_id: str) -> object:
        return self._inner.latest_offer(offer_id)

    async def offer_by_id(self, offer_id: str, version: int) -> object:
        return self._inner.offer_by_id(offer_id, version)

    async def lock_offer_head(
        self, offer_id: str, actor_id: str, subject_id: str
    ) -> object:
        return self._inner.lock_offer_head(offer_id, actor_id, subject_id)

    async def latest_consent(self, consent_id: str) -> object:
        return self._inner.latest_consent(consent_id)

    async def get_consent(self, consent_id: str, version: int) -> object:
        return self._inner.get_consent(consent_id, version)

    async def lock_consent_head(
        self,
        request_actor_id: str,
        evidence_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: str,
        purpose: str,
    ) -> object:
        return self._inner.lock_consent_head(
            request_actor_id,
            evidence_actor_id,
            subject_id,
            binding_id,
            binding_version,
            capability,
            purpose,
        )

    async def active_chains(self, subject_id: str, binding_id: str, binding_version: int) -> object:
        return self._inner.active_chains(subject_id, binding_id, binding_version)

    async def all_active_chains(self) -> object:
        return self._inner.all_active_chains()

    async def latest_snapshot(
        self, subject_id: str, binding_id: str, binding_version: int
    ) -> object:
        return self._inner.latest_snapshot(subject_id, binding_id, binding_version)

    async def lock_snapshot_head(
        self,
        request_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> object:
        return self._inner.lock_snapshot_head(
            request_actor_id, subject_id, binding_id, binding_version
        )

    async def snapshot_by_id(self, snapshot_id: str) -> object:
        return self._inner.snapshot_by_id(snapshot_id)

    async def outbox_by_id(self, event_id: str) -> object:
        return self._inner.outbox_by_id(event_id)

    async def audit_by_id(self, audit_id: str) -> object:
        return self._inner.audit_by_id(audit_id)

    async def get_idempotency(self, idempotency_key: str) -> object:
        return self._inner.get_idempotency(idempotency_key)

    async def append_consent(self, evidence: object) -> None:
        self._inner.append_consent(evidence)  # type: ignore[arg-type]

    async def append_offer(self, offer: object) -> None:
        self._inner.append_offer(offer)  # type: ignore[arg-type]

    async def append_snapshot(self, snapshot: object) -> None:
        self._inner.append_snapshot(snapshot)  # type: ignore[arg-type]

    async def append_outbox(self, event: object) -> None:
        self._inner.append_outbox(event)  # type: ignore[arg-type]

    async def append_audit(self, entry: object) -> None:
        self._inner.append_audit(entry)  # type: ignore[arg-type]

    async def save_idempotency(self, record: object) -> None:
        self._inner.save_idempotency(record)  # type: ignore[arg-type]

    async def commit(self) -> None:
        self._inner.commit()

    async def rollback(self) -> None:
        self._inner.rollback()


class AsyncPassthroughResolver(AsyncEvidenceResolverPort):
    async def resolve_subject(self, candidate: SubjectProof) -> SubjectProof:
        return candidate

    async def resolve_binding(self, candidate: BindingEvidence, subject_id: str) -> BindingEvidence:
        del subject_id
        return candidate

    async def resolve_relationships(
        self,
        candidates: tuple[RelationshipEvidence, ...],
        actor_id: str,
        subject_id: str,
        binding_id: str,
    ) -> tuple[RelationshipEvidence, ...]:
        del actor_id, subject_id, binding_id
        return candidates


async def persist_offer(authority: AsyncConsentAuthority, offer: ConsentOffer) -> ConsentOffer:
    return await authority.create_offer(
        offer_id=offer.offer_id,
        version=offer.version,
        status=offer.status,
        issuer=offer.issuer,
        supersedes_offer_id=offer.supersedes_offer_id,
        capability=offer.capability,
        subject_id=offer.subject_id,
        actor_id=offer.actor_id,
        resource_owner_id=offer.resource_owner_id,
        purpose=offer.purpose,
        params=offer.params,
        valid_from=offer.valid_from,
        valid_until=offer.valid_until,
        policy_version=offer.policy_version,
        created_at=offer.created_at,
    )


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def make_context(now: datetime) -> tuple[SubjectProof, BindingEvidence, RelationshipEvidence]:
    binding = BindingEvidence(
        binding_id="bd_1",
        version=1,
        device_id="dev_1",
        status="active",
        declared_mode="parent_for_child",
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=365),
        canonical_hash="",
    )
    relationship = RelationshipEvidence(
        relationship_id="rel_1",
        snapshot_id="rs_1",
        revision=1,
        relation_type="guardian_of",
        status="active",
        source_person_id="person_guardian",
        target_person_id="person_minor",
        binding_id="bd_1",
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=365),
        canonical_hash="",
    )
    subject = SubjectProof(
        subject_id="person_minor",
        subject_category="minor",
        age_evidence_status="verified",
    )
    return subject, binding, relationship


def make_offer(now: datetime) -> ConsentOffer:
    return ConsentOffer(
        offer_id="of_async",
        capability="chat",
        subject_id="person_minor",
        actor_id="person_guardian",
        resource_owner_id="person_minor",
        purpose="user_request",
        params=ConsentParams(max_session_seconds=3600),
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=365),
        created_at=now,
        policy_version="policy-cn-minor-v5",
    )


@pytest.mark.asyncio
async def test_async_authority_grant_revoke_replay_expire() -> None:
    store: AsyncConsentStorePort = AsyncInMemoryAdapter(InMemoryConsentStore())
    authority = AsyncConsentAuthority(store, resolver=AsyncPassthroughResolver())
    now = utc("2026-08-09T10:00:00+00:00")
    subject, binding, relationship = make_context(now)
    offer = make_offer(now)
    await persist_offer(authority, offer)

    grant = await authority.grant(
        offer,
        actor_kind="guardian",
        subject=subject,
        binding=binding,
        relationships=(relationship,),
        idempotency_key="async-grant-1",
        now=now,
    )
    replay = await authority.grant(
        offer,
        actor_kind="guardian",
        subject=subject,
        binding=binding,
        relationships=(relationship,),
        idempotency_key="async-grant-1",
        now=now + timedelta(minutes=1),
    )
    assert replay.outbox_event.event_id == grant.outbox_event.event_id
    assert replay.evidence == grant.evidence

    revoked = await authority.revoke(
        grant.evidence.consent_id,
        actor_id="person_guardian",
        actor_kind="guardian",
        relationships=(relationship,),
        idempotency_key="async-revoke-1",
        now=now + timedelta(minutes=2),
    )
    assert revoked.evidence.status == "revoked"
    assert revoked.evidence.version == 2

    # Replay of the revocation (same key) returns the stored event id.
    replay_revoke = await authority.revoke(
        grant.evidence.consent_id,
        actor_id="person_guardian",
        actor_kind="guardian",
        relationships=(relationship,),
        idempotency_key="async-revoke-1",
        now=now + timedelta(minutes=3),
    )
    assert replay_revoke.outbox_event.event_id == revoked.outbox_event.event_id
    with pytest.raises(ConsentConflictError):
        # The chain is already revoked; a fresh (different-key) attempt conflicts.
        await authority.revoke(
            grant.evidence.consent_id,
            actor_id="person_guardian",
            actor_kind="guardian",
            relationships=(relationship,),
            idempotency_key="async-revoke-other",
            now=now + timedelta(minutes=4),
        )
    assert replay_revoke.evidence.status == "revoked"


@pytest.mark.asyncio
async def test_async_authority_expire_due() -> None:
    inner = InMemoryConsentStore()
    store: AsyncConsentStorePort = AsyncInMemoryAdapter(inner)
    authority = AsyncConsentAuthority(store, resolver=AsyncPassthroughResolver())
    now = utc("2026-08-09T10:00:00+00:00")
    subject, binding, relationship = make_context(now)
    short_offer = ConsentOffer(
        offer_id="of_short_async",
        capability="chat",
        subject_id="person_minor",
        actor_id="person_guardian",
        resource_owner_id="person_minor",
        purpose="user_request",
        params=ConsentParams(),
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(minutes=30),
        created_at=now,
        policy_version="policy-cn-minor-v5",
    )
    await persist_offer(authority, short_offer)
    await authority.grant(
        short_offer,
        actor_kind="guardian",
        subject=subject,
        binding=binding,
        relationships=(relationship,),
        now=now,
    )
    expired = await authority.expire_due(now + timedelta(hours=1))
    assert len(expired) == 1
    assert expired[0].evidence.status == "expired"
    assert expired[0].outbox_event.aggregate_type == "consent_expire"
    assert await authority.expire_due(now + timedelta(hours=2)) == ()
