"""Explicit migration-only decoders for deprecated Policy contracts."""

from services.policy.migration.legacy import (
    LegacyPolicyReceiptRecord,
    LegacyRuntimeProfileRecord,
    decode_legacy_policy_receipt,
    decode_legacy_runtime_profile,
)

__all__ = [
    "LegacyPolicyReceiptRecord",
    "LegacyRuntimeProfileRecord",
    "decode_legacy_policy_receipt",
    "decode_legacy_runtime_profile",
]
