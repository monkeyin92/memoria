"""Authority ports for identity lifecycle mutations.

Client-supplied strings are never authoritative.  Ownership transfer
requires TWO independent authorities:

1. ``PolicyReceiptAuthority`` — resolves an immutable ``PolicyReceiptV2``
   from the policy service by id and verifies the exact
   ``device_ownership_transfer`` capability, purpose, effect, actor /
   target / device / binding / version / expiry and evidence fence.  The
   policy service owns receipt issuance; this module never mints one.
2. ``StepUpEvidenceAuthority`` — an independent, short-lived, one-time
   authentication evidence bound to actor + operation + nonce with an
   explicit consume/replay rule.

``TransferEvidenceVerifier`` is the composite port consumed by the service;
``Rejecting*`` implementations fail closed, so production stays unwired
until the policy/step-up integration lands.  The HMAC adapter is a
local/test step-up authority only — it must never be wired as a policy
receipt authority.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from services.consent.binding_snapshot import (
    BindingConsentCommand,
    DeterministicBindingConsentAuthority,
    RejectingBindingConsentAuthority,
)

TRANSFER_PURPOSE = "device_transfer"
TRANSFER_CAPABILITY = "device_ownership_transfer"
TRANSFER_DEFAULT_TTL = timedelta(days=7)
TRANSFER_MAX_TTL = timedelta(days=30)
_CLAIM_MAX_LENGTH = 2048


class TransferVerificationError(ValueError):
    """Transfer evidence failed verification (fail closed)."""


class ConsentAuthorityUnavailableError(RuntimeError):
    """No consent authority could resolve the binding consent snapshot."""


# ---------------------------------------------------------------------------
# Policy receipt authority (Policy V2 immutable receipts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedPolicyReceipt:
    """The authority-verified facts of an immutable policy receipt."""

    receipt_id: str
    actor_person_id: str
    subject_person_id: str
    device_id: str
    binding_id: str
    binding_version: int
    capability: str
    purpose: str
    effect: str
    expires_at: datetime
    session_id: str | None = None
    runtime_profile_id: str | None = None


class PolicyReceiptAuthority(Protocol):
    """Resolves a policy receipt by id and verifies the exact transfer
    fence (capability/purpose/effect/actor/subject/device/binding/version/
    expiry).  Receipts are immutable; issuance belongs to the policy
    service."""

    async def verify(
        self,
        *,
        policy_receipt_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
        actor_person_id: str,
        expected_subject_person_id: str,
        now: datetime,
    ) -> VerifiedPolicyReceipt: ...


class RejectingPolicyReceiptAuthority:
    """Default: no policy receipt store is wired; transfer fails closed."""

    async def verify(
        self,
        *,
        policy_receipt_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
        actor_person_id: str,
        expected_subject_person_id: str,
        now: datetime,
    ) -> VerifiedPolicyReceipt:
        raise TransferVerificationError(
            "no policy receipt authority is configured; "
            "ownership transfer fails closed"
        )


# ---------------------------------------------------------------------------
# Step-up evidence authority (independent one-time authentication)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedStepUpEvidence:
    evidence_id: str
    actor_person_id: str
    operation: str
    issued_at: datetime
    expires_at: datetime
    nonce: str


class StepUpEvidenceAuthority(Protocol):
    """Short-lived, one-time authentication evidence bound to actor +
    operation + nonce, with an explicit consume/replay rule."""

    async def verify(
        self,
        *,
        step_up_evidence_id: str,
        actor_person_id: str,
        operation: str,
        now: datetime,
    ) -> VerifiedStepUpEvidence: ...


class RejectingStepUpEvidenceAuthority:
    """Default: no step-up authority; transfer fails closed."""

    async def verify(
        self,
        *,
        step_up_evidence_id: str,
        actor_person_id: str,
        operation: str,
        now: datetime,
    ) -> VerifiedStepUpEvidence:
        raise TransferVerificationError(
            "no step-up evidence authority is configured; "
            "ownership transfer fails closed"
        )


class HmacStepUpEvidenceAuthority:
    """Local/test step-up adapter: HMAC-signed one-time tickets.

    This is NOT a policy receipt and carries no capability claims; it only
    proves that the actor held a server-signed step-up nonce for the
    transfer operation within its TTL.  Tickets are single-use: a verified
    evidence id is recorded as consumed, so replays fail (idempotent command
    replays never re-verify — they read the committed idempotency record).
    """

    def __init__(self, secret: bytes, *, ttl: timedelta = timedelta(minutes=10)) -> None:
        if len(secret) < 32:
            raise ValueError("step-up secret must be >= 32 bytes")
        self._secret = secret
        self._ttl = ttl
        self._consumed: set[str] = set()
        self._lock = threading.Lock()

    def mint(
        self,
        *,
        actor_person_id: str,
        operation: str = "device_ownership_transfer",
        nonce: str = "step-up-1",
        expires_at: datetime | None = None,
    ) -> str:
        if not isinstance(actor_person_id, str) or not actor_person_id.strip():
            raise ValueError("actor_person_id must be a non-empty string")
        if not isinstance(nonce, str) or not nonce.strip():
            raise ValueError("nonce must be a non-empty string")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation must be a non-empty string")
        claims = {
            "evidence_id": str(uuid.uuid4()),
            "actor_person_id": actor_person_id,
            "operation": operation,
            "nonce": nonce,
            "issued_at": datetime.now(UTC).isoformat(),
            "expires_at": (
                expires_at or datetime.now(UTC) + self._ttl
            ).astimezone(UTC).isoformat(),
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        signature = hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{encoded}.{signature}"

    async def verify(
        self,
        *,
        step_up_evidence_id: str,
        actor_person_id: str,
        operation: str,
        now: datetime,
    ) -> VerifiedStepUpEvidence:
        ticket = step_up_evidence_id.strip()
        if "." not in ticket:
            raise TransferVerificationError("malformed step-up ticket")
        encoded, _, signature = ticket.partition(".")
        expected = hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise TransferVerificationError("step-up ticket signature invalid")
        try:
            claims = json.loads(
                base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise TransferVerificationError("step-up ticket payload invalid") from exc
        required = {
            "evidence_id",
            "actor_person_id",
            "operation",
            "nonce",
            "issued_at",
            "expires_at",
        }
        if set(claims) != required:
            raise TransferVerificationError("step-up ticket claims incomplete")
        for key in required:
            value = claims[key]
            if (
                not isinstance(value, str)
                or not value.strip()
                or not (1 <= len(value) <= _CLAIM_MAX_LENGTH)
            ):
                raise TransferVerificationError(
                    f"step-up ticket claim {key!r} must be a bounded non-empty string"
                )
        if claims["actor_person_id"] != actor_person_id:
            raise TransferVerificationError(
                "step-up ticket actor does not match the authenticated user"
            )
        if claims["operation"] != operation:
            raise TransferVerificationError(
                f"step-up ticket operation {claims['operation']!r} is not {operation!r}"
            )
        try:
            expires_at = datetime.fromisoformat(claims["expires_at"])
        except ValueError as exc:
            raise TransferVerificationError(
                "step-up ticket expiry malformed"
            ) from exc
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise TransferVerificationError("step-up ticket expiry must be aware")
        expires_at = expires_at.astimezone(UTC)
        if expires_at <= now.astimezone(UTC):
            raise TransferVerificationError(
                f"step-up ticket expired at {expires_at.isoformat()}"
            )
        evidence_id = str(claims["evidence_id"])
        with self._lock:
            if evidence_id in self._consumed:
                raise TransferVerificationError(
                    "step-up evidence already consumed (replay rejected)"
                )
            self._consumed.add(evidence_id)
        return VerifiedStepUpEvidence(
            evidence_id=evidence_id,
            actor_person_id=str(claims["actor_person_id"]),
            operation=str(claims["operation"]),
            issued_at=datetime.fromisoformat(str(claims["issued_at"])).astimezone(UTC),
            expires_at=expires_at,
            nonce=str(claims["nonce"]),
        )


# ---------------------------------------------------------------------------
# Composite transfer verifier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedTransferEvidence:
    receipt_id: str
    actor_person_id: str
    subject_person_id: str
    device_id: str
    binding_id: str
    binding_version: int
    purpose: str
    capability: str
    effect: str
    expires_at: datetime
    step_up_evidence_id: str


class TransferEvidenceVerifier(Protocol):
    """Composite: policy receipt + step-up evidence both verified."""

    async def verify(
        self,
        *,
        step_up_evidence_id: str,
        policy_receipt_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
        actor_person_id: str,
        expected_subject_person_id: str,
        now: datetime,
    ) -> VerifiedTransferEvidence: ...


class CompositeTransferEvidenceVerifier:
    """Requires BOTH authorities; either missing fails closed."""

    def __init__(
        self,
        *,
        policy_receipt_authority: PolicyReceiptAuthority,
        step_up_authority: StepUpEvidenceAuthority,
    ) -> None:
        self._policy_receipt_authority = policy_receipt_authority
        self._step_up_authority = step_up_authority

    async def verify(
        self,
        *,
        step_up_evidence_id: str,
        policy_receipt_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
        actor_person_id: str,
        expected_subject_person_id: str,
        now: datetime,
    ) -> VerifiedTransferEvidence:
        policy = await self._policy_receipt_authority.verify(
            policy_receipt_id=policy_receipt_id,
            device_id=device_id,
            binding_id=binding_id,
            binding_version=binding_version,
            actor_person_id=actor_person_id,
            expected_subject_person_id=expected_subject_person_id,
            now=now,
        )
        step_up = await self._step_up_authority.verify(
            step_up_evidence_id=step_up_evidence_id,
            actor_person_id=actor_person_id,
            operation=TRANSFER_CAPABILITY,
            now=now,
        )
        if policy.actor_person_id != actor_person_id:
            raise TransferVerificationError(
                "policy receipt actor does not match the authenticated user"
            )
        if policy.subject_person_id != expected_subject_person_id:
            raise TransferVerificationError(
                "policy receipt subject does not match the requested target"
            )
        if policy.device_id != device_id:
            raise TransferVerificationError("policy receipt device mismatch")
        if policy.binding_id != binding_id:
            raise TransferVerificationError("policy receipt binding mismatch")
        if policy.binding_version != binding_version:
            raise TransferVerificationError(
                "policy receipt binding version mismatch"
            )
        return VerifiedTransferEvidence(
            receipt_id=policy.receipt_id,
            actor_person_id=policy.actor_person_id,
            subject_person_id=policy.subject_person_id,
            device_id=policy.device_id,
            binding_id=policy.binding_id,
            binding_version=policy.binding_version,
            purpose=policy.purpose,
            capability=policy.capability,
            effect=policy.effect,
            expires_at=policy.expires_at,
            step_up_evidence_id=step_up.evidence_id,
        )


class RejectingTransferEvidenceVerifier(CompositeTransferEvidenceVerifier):
    """Default wiring: both authorities absent, transfer fails closed."""

    def __init__(self) -> None:
        super().__init__(
            policy_receipt_authority=RejectingPolicyReceiptAuthority(),
            step_up_authority=RejectingStepUpEvidenceAuthority(),
        )


# ---------------------------------------------------------------------------
# Consent snapshot resolver (fail closed by default)
# ---------------------------------------------------------------------------


class ConsentSnapshotResolver(Protocol):
    """Resolves a binding's consent snapshot from the server-side authority."""

    async def resolve(self, *, command: BindingConsentCommand) -> str | None: ...


class RejectingConsentSnapshotResolver(RejectingBindingConsentAuthority):
    """Default: no consent authority, sensitive bindings fail closed."""


class DeterministicConsentSnapshotResolver(DeterministicBindingConsentAuthority):
    """TEST-ONLY server-side snapshot ids.  Never wired in production:
    a real consent service must validate offer catalogs and issue the
    snapshot; until then the production resolver stays Rejecting."""
