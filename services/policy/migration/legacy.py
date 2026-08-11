"""Strict legacy decoders that can never return authorization capability.

Deprecated v1 objects remain readable only for migration or quarantine.  The
records are immutable and intentionally expose ``may_authorize=False``; no
function in this package converts them into a current decision or receipt.
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyReceipt,
    RuntimeProfile,
)


@dataclass(frozen=True, slots=True)
class LegacyPolicyReceiptRecord:
    payload: PolicyReceipt

    @property
    def source_contract(self) -> str:
        return "PolicyReceipt"

    @property
    def disposition(self) -> str:
        return "migration_only"

    @property
    def may_authorize(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class LegacyRuntimeProfileRecord:
    payload: RuntimeProfile

    @property
    def source_contract(self) -> str:
        return "RuntimeProfile"

    @property
    def disposition(self) -> str:
        return "migration_only"

    @property
    def may_authorize(self) -> bool:
        return False


def decode_legacy_policy_receipt(data: dict[str, object]) -> LegacyPolicyReceiptRecord:
    """Strictly decode a v1 receipt into a non-authorizing migration record."""
    return LegacyPolicyReceiptRecord(
        payload=PolicyReceipt.model_validate(data),
    )


def decode_legacy_runtime_profile(data: dict[str, object]) -> LegacyRuntimeProfileRecord:
    """Strictly decode a v1 profile into a non-authorizing migration record."""
    return LegacyRuntimeProfileRecord(
        payload=RuntimeProfile.model_validate(data),
    )
