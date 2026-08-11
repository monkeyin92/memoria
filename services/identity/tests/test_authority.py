"""Transfer evidence authority contracts: immutable policy receipts and
one-time step-up evidence, forged/expired/replayed across both ports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.identity.authority import (
    TRANSFER_CAPABILITY,
    TRANSFER_PURPOSE,
    HmacStepUpEvidenceAuthority,
    RejectingPolicyReceiptAuthority,
    RejectingStepUpEvidenceAuthority,
    RejectingTransferEvidenceVerifier,
    TransferVerificationError,
)
from services.identity.testing_authorities import (
    TestTransferAuthority,
)

_SECRET = b"test-secret-that-is-at-least-32-bytes"
_NOW = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)


async def _verify(
    authority,
    *,
    policy_receipt_id: str,
    step_up_evidence_id: str,
    subject: str = "subject-2",
) -> object:
    return await authority.verify(
        step_up_evidence_id=step_up_evidence_id,
        policy_receipt_id=policy_receipt_id,
        device_id="device-3",
        binding_id="binding-4",
        binding_version=2,
        actor_person_id="actor-1",
        expected_subject_person_id=subject,
        now=_NOW,
    )


def _mint_pair(authority: TestTransferAuthority) -> tuple[str, str]:
    return authority.mint(
        actor_person_id="actor-1",
        subject_person_id="subject-2",
        device_id="device-3",
        binding_id="binding-4",
        binding_version=2,
        expires_at=_NOW + timedelta(hours=1),
        nonce="step-up-9",
    )


@pytest.mark.asyncio
async def test_composite_requires_both_authorities() -> None:
    authority = TestTransferAuthority(_SECRET)
    policy_id, step_up = _mint_pair(authority)
    evidence = await _verify(
        authority,
        policy_receipt_id=policy_id,
        step_up_evidence_id=step_up,
    )
    assert evidence.receipt_id == policy_id
    assert evidence.capability == TRANSFER_CAPABILITY
    assert evidence.purpose == TRANSFER_PURPOSE
    assert evidence.effect == "allow"
    assert evidence.step_up_evidence_id


@pytest.mark.asyncio
async def test_forged_and_expired_policy_receipts_fail_closed() -> None:
    authority = TestTransferAuthority(_SECRET)
    policy_id, step_up = _mint_pair(authority)
    with pytest.raises(TransferVerificationError, match="not found"):
        await _verify(
            authority,
            policy_receipt_id="forged-receipt-id",
            step_up_evidence_id=step_up,
        )
    # An expired receipt is refused even with a valid step-up ticket.
    expired_id = authority.policy_authority.mint(
        actor_person_id="actor-1",
        subject_person_id="subject-2",
        device_id="device-3",
        binding_id="binding-4",
        binding_version=2,
        expires_at=_NOW - timedelta(minutes=1),
    )
    with pytest.raises(TransferVerificationError, match="expired"):
        await _verify(
            authority,
            policy_receipt_id=expired_id,
            step_up_evidence_id=step_up,
        )
    # Subject mismatch on the receipt is refused.
    other_id = authority.policy_authority.mint(
        actor_person_id="actor-1",
        subject_person_id="someone-else",
        device_id="device-3",
        binding_id="binding-4",
        binding_version=2,
        expires_at=_NOW + timedelta(hours=1),
    )
    with pytest.raises(TransferVerificationError, match="subject"):
        await _verify(
            authority,
            policy_receipt_id=other_id,
            step_up_evidence_id=step_up,
        )


@pytest.mark.asyncio
async def test_step_up_tickets_are_one_time_and_short_lived() -> None:
    authority = TestTransferAuthority(_SECRET)
    policy_id, step_up = _mint_pair(authority)
    await _verify(
        authority,
        policy_receipt_id=policy_id,
        step_up_evidence_id=step_up,
    )
    # Replay of the same step-up ticket is rejected (consumed).
    with pytest.raises(TransferVerificationError, match="consumed"):
        await _verify(
            authority,
            policy_receipt_id=policy_id,
            step_up_evidence_id=step_up,
        )
    # Forged ticket signature is rejected.
    with pytest.raises(TransferVerificationError, match="malformed|signature"):
        await _verify(
            authority,
            policy_receipt_id=policy_id,
            step_up_evidence_id="not-a-ticket",
        )


@pytest.mark.asyncio
async def test_step_up_expired_ticket_fails_closed() -> None:
    authority = TestTransferAuthority(_SECRET)
    policy_id = authority.policy_authority.mint(
        actor_person_id="actor-1",
        subject_person_id="subject-2",
        device_id="device-3",
        binding_id="binding-4",
        binding_version=2,
        expires_at=_NOW + timedelta(hours=1),
    )
    expired_ticket = authority.step_up_authority.mint(
        actor_person_id="actor-1",
        expires_at=_NOW - timedelta(minutes=1),
    )
    with pytest.raises(TransferVerificationError, match="expired"):
        await _verify(
            authority,
            policy_receipt_id=policy_id,
            step_up_evidence_id=expired_ticket,
        )


@pytest.mark.asyncio
async def test_rejecting_authorities_fail_closed_by_default() -> None:
    with pytest.raises(TransferVerificationError, match="policy receipt"):
        await RejectingPolicyReceiptAuthority().verify(
            policy_receipt_id="p",
            device_id="d",
            binding_id="b",
            binding_version=1,
            actor_person_id="a",
            expected_subject_person_id="s",
            now=_NOW,
        )
    with pytest.raises(TransferVerificationError, match="step-up"):
        await RejectingStepUpEvidenceAuthority().verify(
            step_up_evidence_id="s",
            actor_person_id="a",
            operation=TRANSFER_CAPABILITY,
            now=_NOW,
        )
    with pytest.raises(TransferVerificationError, match="no .* authority"):
        await RejectingTransferEvidenceVerifier().verify(
            step_up_evidence_id="s",
            policy_receipt_id="p",
            device_id="d",
            binding_id="b",
            binding_version=1,
            actor_person_id="a",
            expected_subject_person_id="s",
            now=_NOW,
        )


@pytest.mark.asyncio
async def test_hmac_step_up_claims_are_strict() -> None:
    step_up_authority = HmacStepUpEvidenceAuthority(_SECRET)
    ticket = step_up_authority.mint(actor_person_id="actor-1")
    with pytest.raises(TransferVerificationError, match="actor"):
        await step_up_authority.verify(
            step_up_evidence_id=ticket,
            actor_person_id="other-actor",
            operation=TRANSFER_CAPABILITY,
            now=_NOW,
        )
    with pytest.raises(TransferVerificationError, match="operation"):
        await step_up_authority.verify(
            step_up_evidence_id=ticket,
            actor_person_id="actor-1",
            operation="other_operation",
            now=_NOW,
        )
