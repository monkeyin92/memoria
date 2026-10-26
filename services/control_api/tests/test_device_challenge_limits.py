"""A device challenge cannot be minted without limit or spent twice.

The challenge route is unauthenticated. Three outstanding challenges is the
cap, and the nonce that authenticates a session dies on first use.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services.agent.src.voice_core.device_security import (
    ProvisionedDevice,
    SignedChallenge,
    provision_device,
    sign_challenge,
)
from services.control_api.app.database import MemoryStore
from services.control_api.app.device_registry import (
    DeviceChallengeRateLimited,
    DeviceChallengeRejected,
    DeviceRegistry,
)

pytestmark = pytest.mark.sqlite_only

DEVICE = "dev-challenge-limits"
NOW = "2026-10-25T00:00:00Z"


def _registry(tmp_path: Path) -> tuple[DeviceRegistry, ProvisionedDevice]:
    store = MemoryStore(str(tmp_path / "control.sqlite3"))
    registry = DeviceRegistry(store)
    provisioned = provision_device(DEVICE)
    registry.register(
        device_id=DEVICE,
        account_id="account-1",
        public_key_b64=provisioned.identity.public_key_b64,
        now=NOW,
    )
    return registry, provisioned


def test_a_fourth_outstanding_challenge_is_refused(tmp_path: Path) -> None:
    registry, _provisioned = _registry(tmp_path)
    for index in range(3):
        registry.issue_challenge(DEVICE, now_ms=1_000 + index)
    with pytest.raises(DeviceChallengeRateLimited):
        registry.issue_challenge(DEVICE, now_ms=2_000)


def test_a_used_challenge_frees_its_slot_and_cannot_sign_in_again(tmp_path: Path) -> None:
    registry, provisioned = _registry(tmp_path)
    first = registry.issue_challenge(DEVICE, now_ms=1_000)
    for index in range(2):
        registry.issue_challenge(DEVICE, now_ms=1_100 + index)
    signed = sign_challenge(
        provisioned, nonce=first.nonce, issued_at_ms=first.issued_at_ms
    )
    assert registry.authenticate(DEVICE, signed, now_ms=1_500) == "account-1"
    registry.issue_challenge(DEVICE, now_ms=1_600)
    with pytest.raises(DeviceChallengeRejected):
        registry.authenticate(DEVICE, signed, now_ms=1_700)


def test_a_challenge_signed_by_another_key_is_rejected(tmp_path: Path) -> None:
    registry, _provisioned = _registry(tmp_path)
    issued = registry.issue_challenge(DEVICE, now_ms=1_000)
    stranger = sign_challenge(
        provision_device("dev-someone-else"),
        nonce=issued.nonce,
        issued_at_ms=issued.issued_at_ms,
    )
    forged = SignedChallenge(
        device_id=DEVICE,
        nonce=stranger.nonce,
        issued_at_ms=stranger.issued_at_ms,
        signature_b64=stranger.signature_b64,
    )
    with pytest.raises(DeviceChallengeRejected):
        registry.authenticate(DEVICE, forged, now_ms=1_500)
