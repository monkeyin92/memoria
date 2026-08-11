"""Strict Policy Engine V2 wire seam.

Generated canonical V2 is wired directly in this module:

* ``obligations`` upgrades from a string array to ``{code, params}`` objects;
* receipts add ``purpose``, consent and relationship snapshot ids/revisions,
  ``device_trust``, ``data_classification``, ``safety_state``, ``jurisdiction``,
  ``binding_canonical_hash`` and ``exact_fence``.

Parameterized obligations are canonical ``PolicyObligationSpec`` objects.
Project Control/Session consumers still need downstream wiring before this can
be claimed end to end. Directed relationships use the generated Identity
authority vocabulary.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from packages.contracts.generated.python.multi_subject_contracts import (
    ObligationParams,
    PolicyDecision,
    PolicyObligationSpec,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyReceiptV2 as CanonicalPolicyReceiptV2,
)

from services.policy.action_fence import verify_action_resource_fence
from services.policy.receipts import PolicyReceiptV2

_RFC3339_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)
_HEX_64 = re.compile(r"^[a-f0-9]{64}$")
_RECEIPT_FIELDS = frozenset(CanonicalPolicyReceiptV2.model_fields)
_PARAM_FIELDS = frozenset(
    {
        "max_session_seconds",
        "retention_ttl_seconds",
        "quiet_hours",
        "extras",
    }
)


def obligations_to_wire(
    obligations: tuple[PolicyObligationSpec, ...],
) -> list[dict[str, object]]:
    """Project parameterized obligations into a stable JSON-compatible shape."""
    return [obligation.model_dump(mode="json") for obligation in obligations]


def decision_to_wire(
    decision: PolicyDecision,
    receipt: PolicyReceiptV2,
) -> dict[str, object]:
    """Project a matching decision/receipt pair to the complete V2 wire shape."""
    if (
        decision.receipt_id != receipt.receipt_id
        or decision.capability != receipt.capability
        or decision.purpose != receipt.purpose
        or decision.effect != receipt.effect
        or decision.reason_code != receipt.reason_code
        or decision.obligations != receipt.obligations
        or decision.policy_version != receipt.policy_version
        or decision.context_hash != receipt.context_hash
        or decision.created_at != receipt.created_at
        or decision.expires_at != receipt.expires_at
        or decision.action_resource_fence != receipt.action_resource_fence
        or decision.action_fence_hash != receipt.action_fence_hash
    ):
        raise ValueError("decision and receipt do not describe the same decision")
    return receipt.model_dump(mode="json")


def _to_rfc3339_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("wire timestamps must be aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def wire_to_receipt(data: dict[str, object]) -> PolicyReceiptV2:
    """Strictly decode the V2 wire shape; malformed or unknown input fails closed."""
    if not isinstance(data, dict):
        raise ValueError("receipt wire value must be an object")
    _require_exact_keys(data, _RECEIPT_FIELDS, "receipt")
    normalized = dict(data)
    _parse_rfc3339_utc(data["created_at"], "created_at")
    _parse_rfc3339_utc(data["expires_at"], "expires_at")
    normalized["obligations"] = obligations_to_wire(
        _decode_obligations(data["obligations"])
    )
    canonical = CanonicalPolicyReceiptV2.model_validate(normalized)
    if not verify_action_resource_fence(canonical.action_resource_fence):
        raise ValueError("action_resource_fence hashes are invalid")
    return canonical


def _require_exact_keys(
    data: dict[str, object], expected: frozenset[str], name: str
) -> None:
    actual = frozenset(data)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise ValueError(f"{name} keys mismatch: unknown={unknown}, missing={missing}")


def _required_string(data: dict[str, object], name: str) -> str:
    value = data[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _required_integer(
    data: dict[str, object], name: str, *, minimum: int
) -> int:
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{name} must be an array of non-empty strings")
    return tuple(value)


def _integer_tuple(value: object, name: str, *, minimum: int) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < minimum
        for item in value
    ):
        raise ValueError(f"{name} must be an array of integers >= {minimum}")
    return tuple(value)


def _parse_rfc3339_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339_UTC.fullmatch(value):
        raise ValueError(f"{name} must be an RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _decode_obligations(value: object) -> tuple[PolicyObligationSpec, ...]:
    if not isinstance(value, list):
        raise ValueError("obligations must be an array")
    obligations: list[PolicyObligationSpec] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each obligation must be an object")
        keys = frozenset(item)
        if keys != {"code", "params"}:
            raise ValueError("obligation requires exact code and params keys")
        normalized = dict(item)
        normalized["params"] = _decode_params(item["params"]).model_dump(
            mode="json"
        )
        obligations.append(PolicyObligationSpec.model_validate(normalized))
    return tuple(obligations)


def _decode_params(value: object) -> ObligationParams:
    if not isinstance(value, dict):
        raise ValueError("obligation params must be an object")
    if frozenset(value) != _PARAM_FIELDS:
        raise ValueError("obligation params require exact canonical keys")
    max_session = _optional_positive_integer(value, "max_session_seconds")
    retention = _optional_positive_integer(value, "retention_ttl_seconds")
    quiet_raw = value.get("quiet_hours")
    quiet_hours: tuple[str, str] | None = None
    if quiet_raw is not None:
        if (
            not isinstance(quiet_raw, list)
            or len(quiet_raw) != 2
            or not all(isinstance(item, str) for item in quiet_raw)
        ):
            raise ValueError("quiet_hours must be null or a two-string array")
        quiet_hours = (quiet_raw[0], quiet_raw[1])
    extras_raw = value.get("extras", [])
    if not isinstance(extras_raw, list):
        raise ValueError("extras must be an array")
    extras: list[tuple[str, str]] = []
    for pair in extras_raw:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], str)
        ):
            raise ValueError("extras entries must be two-string arrays")
        extras.append((pair[0], pair[1]))
    return ObligationParams.model_validate(
        {
            "max_session_seconds": max_session,
            "retention_ttl_seconds": retention,
            "quiet_hours": quiet_hours,
            "extras": tuple(extras),
        }
    )


def _optional_positive_integer(data: dict[str, object], name: str) -> int | None:
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be null or a positive integer")
    return value
