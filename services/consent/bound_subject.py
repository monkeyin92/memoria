"""Standing consents for the one person a device is bound to.

A device serves one person (the product decision of 2026-09-25). What that
person may use on it is decided by Policy from consent evidence in this
authority, and until now nothing ever wrote any: the binding recorded which
offers were accepted (``binding_snapshot``) but, by design, issued no grant.

This module turns an accepted binding offer into authority grants for that
subject on that exact binding, and withdraws them again. Who consents is
recorded as who actually consented:

- ``subject``: an adult binding a device for themself;
- ``guardian``: a parent for a child (under the attested ``guardian_of``);
- ``delegate``: an adult child for an elderly parent (under the attested
  ``delegate_for``). Never recorded as the parent's own consent.

Evidence is resolved from Identity for the acting person on every call; no
caller-supplied subject, binding or relationship payload is trusted.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, cast

from packages.contracts.generated.python.multi_subject_contracts import CapabilityValue

from services.consent.authority import AsyncConsentAuthority, SubjectProof
from services.consent.evidence import (
    BindingEvidence,
    ConsentEvidence,
    ConsentParams,
    RelationshipEvidence,
)
from services.consent.store import AsyncConsentStorePort
from services.identity.domain import DeviceBinding, Relationship
from services.identity.service import IdentityService
from services.policy.context import canonical_runtime_purpose_for_capability

__all__ = [
    "BOUND_SUBJECT_POLICY_VERSION",
    "GUARDIAN_MEMORY_CAPABILITIES",
    "MEMORY_CAPABILITIES",
    "MINOR_SESSION_CAPABILITIES",
    "BoundSubjectConsentService",
    "BoundSubjectGrant",
    "IdentityEvidenceResolver",
]

BOUND_SUBJECT_POLICY_VERSION: Final = "bound-subject-consent-v1"
#: The subject's own long-term memory on the device.
MEMORY_CAPABILITIES: Final[tuple[CapabilityValue, ...]] = (
    "memory_capture",
    "memory_recall_private",
)
#: A guardian's long-term-memory grant for a child also opens the weekly
#: summary: without retained memory there is nothing to summarize, and the
#: parent sees summaries, never the child's words (user decision 2026-09-26).
GUARDIAN_MEMORY_CAPABILITIES: Final[tuple[CapabilityValue, ...]] = (
    *MEMORY_CAPABILITIES,
    "guardian_summary_view",
)
#: What a guardian's minor voice-session offer covers.
MINOR_SESSION_CAPABILITIES: Final[tuple[CapabilityValue, ...]] = (
    "chat",
    "tutor",
    "english_practice",
)
#: A grant lasts until it is revoked or its binding ends.
_NO_EXPIRY: Final = datetime(9999, 12, 31, tzinfo=UTC)
_PROXY_RELATIONS: Final[frozenset[str]] = frozenset(
    {"guardian_of", "parent_of", "delegate_for"}
)

type GrantKind = Literal["subject", "guardian", "delegate"]


def _relationship_evidence(relationship: Relationship, *, binding_id: str) -> RelationshipEvidence:
    # Same snapshot identity the Session Runtime derives from the locked
    # binding, so a receipt names the relationship revision consent verified.
    revision = max(1, int(relationship.updated_at.timestamp() * 1_000_000))
    return RelationshipEvidence(
        relationship_id=relationship.relationship_id,
        snapshot_id=f"{relationship.relationship_id}:v{revision}",
        revision=revision,
        relation_type=relationship.relation_type,
        status=relationship.status,
        source_person_id=relationship.source_person_id,
        target_person_id=relationship.target_person_id,
        binding_id=binding_id,
        valid_from=relationship.valid_from,
        valid_until=relationship.valid_until or _NO_EXPIRY,
    )


def _binding_evidence(binding: DeviceBinding) -> BindingEvidence:
    return BindingEvidence(
        binding_id=binding.binding_id,
        version=binding.binding_version,
        device_id=binding.device_id,
        status=binding.status,
        declared_mode=binding.declared_mode,
        valid_from=binding.valid_from,
        valid_until=binding.valid_until or _NO_EXPIRY,
    )


class IdentityEvidenceResolver:
    """``AsyncEvidenceResolverPort`` over Identity, read as one acting person.

    Every method ignores the candidate payload beyond its stable id and
    re-reads the authoritative record, so a caller cannot smuggle a subject
    category, a binding window or an active relationship into a grant.
    """

    def __init__(self, identity: IdentityService, *, actor_person_id: str) -> None:
        self._identity = identity
        self._actor = actor_person_id

    async def resolve_subject(self, candidate: SubjectProof) -> SubjectProof:
        person = await self._identity.get_person(
            candidate.subject_id, actor_person_id=self._actor
        )
        return SubjectProof(
            subject_id=person.person_id,
            subject_category=person.subject_category,
            age_evidence_status=person.age_evidence_status,
        )

    async def resolve_binding(
        self, candidate: BindingEvidence, subject_id: str
    ) -> BindingEvidence:
        binding = await self._identity.get_binding(
            candidate.binding_id, actor_person_id=self._actor
        )
        if (
            binding.status != "active"
            or binding.binding_version != candidate.version
            or binding.device_id != candidate.device_id
            or subject_id not in binding.primary_subject_ids
        ):
            raise PermissionError("binding does not serve this subject")
        return _binding_evidence(binding)

    async def resolve_relationships(
        self,
        candidates: tuple[RelationshipEvidence, ...],
        actor_id: str,
        subject_id: str,
        binding_id: str,
    ) -> tuple[RelationshipEvidence, ...]:
        del candidates
        if actor_id == subject_id:
            return ()
        relationships = await self._identity.list_relationships(
            person_id=subject_id,
            statuses=("active",),
            actor_person_id=self._actor,
        )
        return tuple(
            _relationship_evidence(item, binding_id=binding_id)
            for item in relationships
            if item.relation_type in _PROXY_RELATIONS
            and item.source_person_id == actor_id
            and item.target_person_id == subject_id
        )


@dataclass(frozen=True, slots=True)
class BoundSubjectGrant:
    """One accepted binding offer turned into standing grants."""

    actor_person_id: str
    subject_person_id: str
    binding_id: str
    kind: GrantKind
    capabilities: tuple[CapabilityValue, ...]
    #: Stable id of what the person accepted (e.g. the binding consent
    #: snapshot); a retry with the same key replays instead of re-granting.
    source_key: str

    def __post_init__(self) -> None:
        if not self.capabilities:
            raise ValueError("a bound-subject grant needs at least one capability")
        if (self.kind == "subject") != (self.actor_person_id == self.subject_person_id):
            raise ValueError("only a subject grant has the subject as its actor")


def _digest(*parts: object) -> str:
    return hashlib.sha256("\0".join(str(part) for part in parts).encode("utf-8")).hexdigest()


type ProvisionPort = Callable[[str, str, str, datetime], Awaitable[None]]


class BoundSubjectConsentService:
    """Grant and withdraw the bound subject's standing consents."""

    def __init__(
        self,
        *,
        store: AsyncConsentStorePort,
        identity: IdentityService,
        provision: ProvisionPort | None = None,
        prepare: Callable[[], Awaitable[None]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._prepare = prepare
        self._identity = identity
        # Deployment authorization for the consent role to write this
        # (actor, subject, binding); PostgreSQL enforces it with RLS.
        self._provision = provision
        self._now = now or (lambda: datetime.now(UTC))

    def _authority(self, actor_person_id: str) -> AsyncConsentAuthority:
        return AsyncConsentAuthority(
            self._store,
            resolver=IdentityEvidenceResolver(self._identity, actor_person_id=actor_person_id),
            now=self._now,
        )

    async def _ready(self) -> None:
        if self._prepare is not None:
            await self._prepare()

    async def grant(self, request: BoundSubjectGrant) -> tuple[ConsentEvidence, ...]:
        await self._ready()
        now = self._now()
        binding = await self._identity.get_binding(
            request.binding_id, actor_person_id=request.actor_person_id
        )
        evidence = _binding_evidence(binding)
        if self._provision is not None:
            await self._provision(
                request.actor_person_id, request.subject_person_id, binding.binding_id, now
            )
        authority = self._authority(request.actor_person_id)
        granted: list[ConsentEvidence] = []
        for capability in request.capabilities:
            purpose = canonical_runtime_purpose_for_capability(capability)
            offer = await authority.create_offer(
                capability=capability,
                subject_id=request.subject_person_id,
                actor_id=request.actor_person_id,
                resource_owner_id=request.subject_person_id,
                purpose=purpose,
                params=ConsentParams(),
                valid_from=binding.valid_from,
                valid_until=evidence.valid_until,
                policy_version=BOUND_SUBJECT_POLICY_VERSION,
                # Deterministic offer identity and clock: a retry must land on
                # the same canonical offer, never a conflicting second one.
                offer_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "memoria:bound-offer:"
                        f"{binding.binding_id}:{binding.binding_version}:"
                        f"{request.subject_person_id}:{request.actor_person_id}:"
                        f"{capability}:{BOUND_SUBJECT_POLICY_VERSION}",
                    )
                ),
                created_at=binding.valid_from,
            )
            result = await authority.grant(
                offer,
                expected_version=offer.version,
                subject=SubjectProof(
                    subject_id=request.subject_person_id,
                    subject_category="unknown",
                    age_evidence_status="unverified",
                ),
                binding=evidence,
                actor_kind=request.kind,
                idempotency_key=_digest(
                    "bound-grant",
                    binding.binding_id,
                    binding.binding_version,
                    request.subject_person_id,
                    capability,
                    request.source_key,
                )[:64],
                now=now,
            )
            granted.append(result.evidence)
        return tuple(granted)

    async def active(
        self, *, subject_person_id: str, binding_id: str, binding_version: int
    ) -> tuple[ConsentEvidence, ...]:
        await self._ready()
        uow = await self._store.transaction()
        try:
            chains = await uow.active_chains(subject_person_id, binding_id, binding_version)
            await uow.commit()
        except BaseException:
            await uow.rollback()
            raise
        return chains

    async def revoke(
        self,
        *,
        actor_person_id: str,
        subject_person_id: str,
        binding_id: str,
        capabilities: tuple[CapabilityValue, ...] | None = None,
        reason: str,
    ) -> int:
        """Withdraw what ``actor_person_id`` granted; returns how many."""

        binding = await self._identity.get_binding(binding_id, actor_person_id=actor_person_id)
        chains = await self.active(
            subject_person_id=subject_person_id,
            binding_id=binding.binding_id,
            binding_version=binding.binding_version,
        )
        resolver = IdentityEvidenceResolver(self._identity, actor_person_id=actor_person_id)
        relationships = await resolver.resolve_relationships(
            (), actor_person_id, subject_person_id, binding.binding_id
        )
        authority = self._authority(actor_person_id)
        revoked = 0
        for chain in chains:
            if chain.actor_id != actor_person_id:
                continue
            if capabilities is not None and chain.capability not in capabilities:
                continue
            await authority.revoke(
                chain.consent_id,
                actor_id=actor_person_id,
                actor_kind=chain.actor_kind,
                relationships=relationships,
                reason=reason,
                idempotency_key=_digest("bound-revoke", chain.consent_id, chain.version)[:64],
                now=self._now(),
            )
            revoked += 1
        return revoked

    async def carry_forward(
        self,
        *,
        actor_person_id: str,
        subject_person_id: str,
        previous_binding_id: str,
        previous_binding_version: int,
        binding_id: str,
    ) -> tuple[ConsentEvidence, ...]:
        """Re-grant on a superseding binding what the previous one carried.

        Grants are fenced to one binding version, so without this every
        supersede would silently switch the subject's memory off.
        """

        chains = await self.active(
            subject_person_id=subject_person_id,
            binding_id=previous_binding_id,
            binding_version=previous_binding_version,
        )
        by_kind: dict[GrantKind, list[CapabilityValue]] = {}
        for chain in chains:
            if chain.actor_id != actor_person_id or chain.actor_kind not in {
                "subject",
                "guardian",
                "delegate",
            }:
                continue
            by_kind.setdefault(cast(GrantKind, chain.actor_kind), []).append(chain.capability)
        granted: list[ConsentEvidence] = []
        for kind, capabilities in sorted(by_kind.items()):
            granted.extend(
                await self.grant(
                    BoundSubjectGrant(
                        actor_person_id=actor_person_id,
                        subject_person_id=subject_person_id,
                        binding_id=binding_id,
                        kind=kind,
                        capabilities=tuple(sorted(set(capabilities))),
                        source_key=f"carry:{previous_binding_id}:{previous_binding_version}",
                    )
                )
            )
        return tuple(granted)
