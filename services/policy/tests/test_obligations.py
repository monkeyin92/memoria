"""Generated PolicyObligationSpec parameters are canonical and data-only."""

from __future__ import annotations

import pytest
from packages.contracts.generated.python import multi_subject_contracts as generated
from pydantic import ValidationError
from services.policy.obligations import (
    obligation_params_from_json,
    obligation_params_to_json,
)


def _params(**changes: object) -> generated.ObligationParams:
    payload: dict[str, object] = {
        "max_session_seconds": None,
        "retention_ttl_seconds": None,
        "quiet_hours": None,
        "extras": [],
    }
    payload.update(changes)
    return generated.ObligationParams.model_validate(payload)


def test_obligation_params_reject_non_positive_session_seconds() -> None:
    with pytest.raises(ValueError):
        _params(max_session_seconds=0)
    with pytest.raises(ValueError):
        _params(max_session_seconds=-30)


def test_obligation_params_reject_non_positive_retention_ttl() -> None:
    with pytest.raises(ValueError):
        _params(retention_ttl_seconds=0)
    with pytest.raises(ValueError):
        _params(retention_ttl_seconds=-1)


@pytest.mark.parametrize(
    "bad",
    [
        ("25:00", "08:00"),
        ("08:00", "24:00"),
        ("8:00", "09:00"),
        ("08:00", "9:30"),
        ("08:60", "09:00"),
        ("08:00", "09:61"),
        ("0800", "0900"),
        ("", "09:00"),
    ],
)
def test_obligation_params_reject_invalid_quiet_hours(bad: tuple[str, str]) -> None:
    with pytest.raises(ValueError, match="quiet_hours"):
        _params(quiet_hours=bad)


def test_obligation_params_accept_valid_quiet_hours() -> None:
    params = _params(quiet_hours=("21:30", "06:30"))
    assert params.quiet_hours == ("21:30", "06:30")


def test_obligation_params_preserve_canonical_wire_extras() -> None:
    params = _params(extras=(("a", "2"), ("z", "3")))
    assert params.extras == (("a", "2"), ("z", "3"))


def test_obligation_params_reject_non_string_extras() -> None:
    with pytest.raises((TypeError, ValueError)):
        _params(extras=((1, 2),))


def test_generated_obligation_uses_code_projection_not_string_equality() -> None:
    obligation = generated.PolicyObligationSpec(
        code=generated.PolicyObligation.POLICY_OBLIGATION_DO_NOT_PERSIST,
        params=_params(),
    )
    assert obligation.code.value == "DO_NOT_PERSIST"
    assert obligation != "DO_NOT_PERSIST"


def test_policy_obligation_carries_params() -> None:
    obligation = generated.PolicyObligationSpec(
        code=generated.PolicyObligation.POLICY_OBLIGATION_MAX_SESSION_SECONDS,
        params=_params(max_session_seconds=1800),
    )
    assert obligation.params.max_session_seconds == 1800
    assert obligation.params.retention_ttl_seconds is None
    assert obligation.params.quiet_hours is None


def test_generated_policy_obligation_equality_includes_params() -> None:
    plain = generated.PolicyObligationSpec(
        code=generated.PolicyObligation.POLICY_OBLIGATION_MAX_SESSION_SECONDS,
        params=_params(),
    )
    with_params = generated.PolicyObligationSpec(
        code=generated.PolicyObligation.POLICY_OBLIGATION_MAX_SESSION_SECONDS,
        params=_params(max_session_seconds=1800),
    )
    assert plain != with_params
    assert with_params == generated.PolicyObligationSpec(
        code=generated.PolicyObligation.POLICY_OBLIGATION_MAX_SESSION_SECONDS,
        params=_params(max_session_seconds=1800),
    )


def test_obligations_carry_data_only_not_callables() -> None:
    """Obligations must never embed executable callables: params are auditable data."""
    obligation = generated.PolicyObligationSpec(
        code=generated.PolicyObligation.POLICY_OBLIGATION_QUIET_HOURS,
        params=_params(quiet_hours=("21:30", "06:30")),
    )
    jsonish = obligation_params_to_json(obligation.params)
    assert jsonish == {
        "max_session_seconds": None,
        "retention_ttl_seconds": None,
        "quiet_hours": ["21:30", "06:30"],
        "extras": [],
    }
    assert obligation_params_from_json(jsonish) == obligation.params


def test_params_json_roundtrip_with_extras() -> None:
    params = _params(
        max_session_seconds=1800,
        retention_ttl_seconds=86400,
        extras=(("data_classification", "biometric"),),
    )
    assert obligation_params_from_json(obligation_params_to_json(params)) == params


def test_obligation_is_frozen() -> None:
    obligation = generated.PolicyObligationSpec(
        code=generated.PolicyObligation.POLICY_OBLIGATION_NO_MODEL_TRAINING,
        params=_params(),
    )
    with pytest.raises(ValidationError):
        obligation.code = generated.PolicyObligation.POLICY_OBLIGATION_DO_NOT_PERSIST


def test_obligation_params_json_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown"):
        obligation_params_from_json({"prompt": "execute this"})


def test_obligation_params_json_requires_all_generated_keys() -> None:
    with pytest.raises(ValueError, match="keys"):
        obligation_params_from_json({"max_session_seconds": None})


@pytest.mark.parametrize(
    "field",
    ["max_session_seconds", "retention_ttl_seconds"],
)
def test_obligation_params_json_rejects_bool_numeric_field(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        obligation_params_from_json(
            {
                "max_session_seconds": True if field == "max_session_seconds" else None,
                "retention_ttl_seconds": (
                    True if field == "retention_ttl_seconds" else None
                ),
                "quiet_hours": None,
                "extras": [],
            }
        )


def test_obligation_params_json_rejects_malformed_quiet_hours() -> None:
    with pytest.raises(ValueError, match="quiet_hours"):
        obligation_params_from_json(
            {
                "max_session_seconds": None,
                "retention_ttl_seconds": None,
                "quiet_hours": ["21:30"],
                "extras": [],
            }
        )


def test_obligation_params_json_rejects_malformed_extras() -> None:
    with pytest.raises(ValueError, match="extras"):
        obligation_params_from_json(
            {
                "max_session_seconds": None,
                "retention_ttl_seconds": None,
                "quiet_hours": None,
                "extras": [["only-key"]],
            }
        )
