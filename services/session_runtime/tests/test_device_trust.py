"""The opt-in that turns a bound, unattested device into the ``trusted`` tier."""

from __future__ import annotations

import pytest
from services.session_runtime.device_trust import (
    BOUND_DEVICE_REASON,
    DeviceTrustSnapshot,
    snapshot_from_authority,
)


def _answer(**overrides: object) -> dict[str, object]:
    answer: dict[str, object] = {
        "available": True,
        "device_trust": "untrusted",
        "reason_code": "device_attestation_unavailable",
        "device_bound": True,
    }
    answer.update(overrides)
    return answer


def test_a_bound_device_is_upgraded_only_when_the_deployment_opted_in() -> None:
    opted_out = snapshot_from_authority(_answer(), accept_bound_device=False)
    opted_in = snapshot_from_authority(_answer(), accept_bound_device=True)

    assert opted_out == DeviceTrustSnapshot(
        available=True, trust="untrusted", reason_code="device_attestation_unavailable"
    )
    assert opted_in == DeviceTrustSnapshot(
        available=True, trust="trusted", reason_code=BOUND_DEVICE_REASON
    )


@pytest.mark.parametrize("fact", [False, None, "true", 1, "yes", [], {}])
def test_only_a_strict_true_counts_as_the_bound_fact(fact: object) -> None:
    snapshot = snapshot_from_authority(_answer(device_bound=fact), accept_bound_device=True)

    assert snapshot.trust == "untrusted"


def test_an_answer_without_the_fact_is_left_alone() -> None:
    answer = _answer()
    del answer["device_bound"]

    assert snapshot_from_authority(answer, accept_bound_device=True).trust == "untrusted"


@pytest.mark.parametrize("trust", ["verified", "revoked", "offline", "trusted"])
def test_nothing_but_untrusted_is_ever_changed(trust: str) -> None:
    snapshot = snapshot_from_authority(
        _answer(device_trust=trust, reason_code="whatever"), accept_bound_device=True
    )

    assert snapshot == DeviceTrustSnapshot(available=True, trust=trust, reason_code="whatever")


def test_an_unavailable_authority_is_never_upgraded() -> None:
    snapshot = snapshot_from_authority(_answer(available=False), accept_bound_device=True)

    assert (snapshot.available, snapshot.trust) == (False, "untrusted")


def test_a_missing_reason_code_becomes_unknown() -> None:
    answer = _answer(device_trust="verified")
    del answer["reason_code"]

    assert snapshot_from_authority(answer, accept_bound_device=False).reason_code == "unknown"


@pytest.mark.parametrize(
    "answer",
    [
        {"available": "yes", "device_trust": "verified"},
        {"available": True, "device_trust": "bound"},
        {"available": True, "device_trust": ""},
        {"available": True},
        {"device_trust": "verified"},
        {},
    ],
)
def test_an_answer_the_contract_does_not_allow_fails_closed(answer: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="Device trust authority is invalid"):
        snapshot_from_authority(answer, accept_bound_device=True)
