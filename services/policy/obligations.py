"""Generated canonical obligation helpers.

New Policy paths carry only generated ``PolicyObligationSpec`` and generated
``ObligationParams``.  This module contains strict JSON validation and an
explicit code projection for assertions/legacy readers; it defines no parallel
obligation model and never gives generated objects string equality semantics.
"""

from __future__ import annotations

from packages.contracts.generated.python.multi_subject_contracts import (
    ObligationParams,
    PolicyObligationSpec,
)

_PARAM_KEYS = frozenset(
    {
        "max_session_seconds",
        "retention_ttl_seconds",
        "quiet_hours",
        "extras",
    }
)


def obligation_params_to_json(params: ObligationParams) -> dict[str, object]:
    if not isinstance(params, ObligationParams):
        raise TypeError("params must be generated ObligationParams")
    return params.model_dump(mode="json")


def obligation_params_from_json(data: dict[str, object]) -> ObligationParams:
    if not isinstance(data, dict):
        raise ValueError("obligation params must be an object")
    actual = frozenset(data)
    if actual != _PARAM_KEYS:
        raise ValueError(
            "obligation params keys mismatch: "
            f"unknown={sorted(actual - _PARAM_KEYS)}, "
            f"missing={sorted(_PARAM_KEYS - actual)}"
        )
    for name in ("max_session_seconds", "retention_ttl_seconds"):
        value = data[name]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{name} must be null or a positive integer")
    quiet_hours = data["quiet_hours"]
    if quiet_hours is not None and (
        not isinstance(quiet_hours, list)
        or len(quiet_hours) != 2
        or any(not isinstance(item, str) for item in quiet_hours)
    ):
        raise ValueError("quiet_hours must be null or a two-string array")
    extras = data["extras"]
    if not isinstance(extras, list) or any(
        not isinstance(pair, list)
        or len(pair) != 2
        or not isinstance(pair[0], str)
        or not isinstance(pair[1], str)
        for pair in extras
    ):
        raise ValueError("extras must be an array of two-string arrays")
    return ObligationParams.model_validate(data)


def obligation_codes(
    obligations: tuple[PolicyObligationSpec, ...],
) -> tuple[str, ...]:
    """Explicit canonical code projection; not an equality compatibility shim."""
    if any(not isinstance(item, PolicyObligationSpec) for item in obligations):
        raise TypeError("obligations must contain generated PolicyObligationSpec")
    return tuple(item.code.value for item in obligations)
