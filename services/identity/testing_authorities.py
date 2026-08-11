"""TEST-ONLY authority implementations shared by unit and API tests.

Never import this module from production wiring: the Control API fails
closed (Rejecting authorities) until the real policy-receipt and step-up
integration lands.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from services.identity.authority import (
    TRANSFER_CAPABILITY,
    TRANSFER_PURPOSE,
    CompositeTransferEvidenceVerifier,
    DeterministicConsentSnapshotResolver,
    HmacStepUpEvidenceAuthority,
    TransferVerificationError,
    VerifiedPolicyReceipt,
)
from services.identity.repository import IdentityStore
from services.identity.service import IdentityService


class FakePolicyReceiptAuthority:
    """TEST-ONLY directory of immutable policy receipts (Policy V2 shape).

    Real issuance belongs to the policy service; this fake only lets tests
    exercise the exact-fence verification path.
    """

    def __init__(self) -> None:
        self._receipts: dict[str, VerifiedPolicyReceipt] = {}

    def mint(
        self,
        *,
        actor_person_id: str,
        subject_person_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
        expires_at: datetime,
    ) -> str:
        receipt_id = str(uuid.uuid4())
        self._receipts[receipt_id] = VerifiedPolicyReceipt(
            receipt_id=receipt_id,
            actor_person_id=actor_person_id,
            subject_person_id=subject_person_id,
            device_id=device_id,
            binding_id=binding_id,
            binding_version=binding_version,
            capability=TRANSFER_CAPABILITY,
            purpose=TRANSFER_PURPOSE,
            effect="allow",
            expires_at=expires_at.astimezone(UTC),
        )
        return receipt_id

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
        receipt = self._receipts.get(policy_receipt_id.strip())
        if receipt is None:
            raise TransferVerificationError("policy receipt not found")
        if receipt.capability != TRANSFER_CAPABILITY:
            raise TransferVerificationError("policy receipt capability mismatch")
        if receipt.purpose != TRANSFER_PURPOSE:
            raise TransferVerificationError("policy receipt purpose mismatch")
        if receipt.effect != "allow":
            raise TransferVerificationError("policy receipt effect is not allow")
        if receipt.actor_person_id != actor_person_id:
            raise TransferVerificationError("policy receipt actor mismatch")
        if receipt.subject_person_id != expected_subject_person_id:
            raise TransferVerificationError("policy receipt subject mismatch")
        if receipt.device_id != device_id:
            raise TransferVerificationError("policy receipt device mismatch")
        if receipt.binding_id != binding_id:
            raise TransferVerificationError("policy receipt binding mismatch")
        if receipt.binding_version != binding_version:
            raise TransferVerificationError(
                "policy receipt binding version mismatch"
            )
        if receipt.expires_at <= now.astimezone(UTC):
            raise TransferVerificationError(
                f"policy receipt expired at {receipt.expires_at.isoformat()}"
            )
        return receipt


class TestTransferAuthority(CompositeTransferEvidenceVerifier):
    """TEST-ONLY composite: fake policy receipts + HMAC step-up tickets."""

    __test__ = False  # never collected as a pytest test class

    def __init__(self, secret: bytes) -> None:
        self.policy_authority = FakePolicyReceiptAuthority()
        self.step_up_authority = HmacStepUpEvidenceAuthority(secret)
        super().__init__(
            policy_receipt_authority=self.policy_authority,
            step_up_authority=self.step_up_authority,
        )

    def mint(
        self,
        *,
        actor_person_id: str,
        subject_person_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
        expires_at: datetime,
        nonce: str = "step-up-1",
    ) -> tuple[str, str]:
        """Returns (policy_receipt_id, step_up_evidence_id)."""
        policy_receipt_id = self.policy_authority.mint(
            actor_person_id=actor_person_id,
            subject_person_id=subject_person_id,
            device_id=device_id,
            binding_id=binding_id,
            binding_version=binding_version,
            expires_at=expires_at,
        )
        step_up_ticket = self.step_up_authority.mint(
            actor_person_id=actor_person_id,
            nonce=nonce,
        )
        return policy_receipt_id, step_up_ticket


def make_test_service(
    store: IdentityStore,
    *,
    secret: bytes = b"test-secret-that-is-at-least-32-bytes",
) -> tuple[IdentityService, TestTransferAuthority]:
    authority = TestTransferAuthority(secret)
    service = IdentityService(
        store,
        transfer_verifier=authority,
        consent_resolver=DeterministicConsentSnapshotResolver(),
    )
    return service, authority
