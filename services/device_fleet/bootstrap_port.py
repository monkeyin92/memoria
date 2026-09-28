"""Store port for device onboarding.

``DeviceOnboardingService`` depends on this Protocol; the SQLite and
PostgreSQL stores both implement it, so mypy checks each against the same
signatures.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from services.device_fleet.bootstrap_domain import (
    ActivationRecord,
    BindingRecord,
    BootstrapSession,
    BootstrapState,
    ClaimReservation,
    DeviceChallenge,
    DeviceMediaChallenge,
    DeviceRecord,
)


class BootstrapStorePort(Protocol):
    """Structural store boundary shared by SQLite and PostgreSQL.

    Lists exactly the operations DeviceOnboardingService calls, so the service
    type-checks against the port instead of silencing attr-defined errors.
    """

    def accept_activation_ack(
        self, *, ack_payload: Mapping[str, object], now: datetime
    ) -> ActivationRecord: ...

    def accept_online_proof(
        self,
        *,
        challenge_id: str,
        onboarding_session_id: str,
        device_id: str,
        monotonic_counter: int,
        firmware_version: str,
        firmware_security_version: int,
        now: datetime,
    ) -> BootstrapSession: ...

    def active_claim_for_device(
        self, *, device_id: str, now: datetime
    ) -> ClaimReservation | None: ...

    def adopt_binding_authority(
        self, *, claim_id: str, actor_id: str, binding_id: str, binding_version: int
    ) -> BindingRecord: ...

    def begin_binding(
        self,
        *,
        binding: BindingRecord,
        idempotency_key: str,
        expected_state_version: int,
        now: datetime,
    ) -> BindingRecord: ...

    def commit_binding(
        self,
        *,
        claim_id: str,
        actor_id: str,
        binding_id: str,
        binding_version: int,
        manifest: Mapping[str, object],
        manifest_hash: str,
        activation_id: str,
        activation_version: int,
        activation_expires_at: datetime,
        now: datetime,
    ) -> tuple[BindingRecord, ActivationRecord]: ...

    def consume_media_challenge(
        self, *, challenge_id: str, device_id: str, nonce_hash: str, now: datetime
    ) -> DeviceMediaChallenge: ...

    def create_session(self, session: BootstrapSession) -> BootstrapSession: ...

    def expire_claim_if_needed(self, claim_id: str, *, now: datetime) -> ClaimReservation: ...

    def expire_session(self, onboarding_session_id: str, *, now: datetime) -> BootstrapSession: ...

    def find_session_by_client(
        self, *, actor_id: str, client_onboarding_id: str
    ) -> BootstrapSession | None: ...

    def find_session_by_qr(
        self, *, device_id: str, qr_nonce_hash: str
    ) -> BootstrapSession | None: ...

    def get_binding(self, binding_id: str) -> BindingRecord | None: ...

    def get_binding_for_claim(self, claim_id: str) -> BindingRecord | None: ...

    def get_challenge(self, challenge_id: str) -> DeviceChallenge | None: ...

    def get_claim(self, claim_id: str) -> ClaimReservation | None: ...

    def get_claim_for_session(self, onboarding_session_id: str) -> ClaimReservation | None: ...

    def get_device(self, device_id: str) -> DeviceRecord | None: ...

    def get_media_challenge(self, challenge_id: str) -> DeviceMediaChallenge | None: ...

    def get_session(self, onboarding_session_id: str) -> BootstrapSession | None: ...

    def is_actor_bound_to_device(self, *, actor_id: str, device_id: str) -> bool: ...

    def issue_challenge(self, challenge: DeviceChallenge) -> DeviceChallenge: ...

    def issue_media_challenge(
        self, challenge: DeviceMediaChallenge, *, max_outstanding: int = 3
    ) -> DeviceMediaChallenge: ...

    def latest_activation_for_device(self, device_id: str) -> ActivationRecord | None: ...

    def mark_activation_downloaded(self, *, device_id: str, now: datetime) -> ActivationRecord: ...

    def register_manufactured_device(self, device: DeviceRecord) -> DeviceRecord: ...

    def release_binding(
        self, *, claim_id: str, actor_id: str, reason_code: str, now: datetime
    ) -> ClaimReservation: ...

    def reserve_claim(
        self, claim: ClaimReservation, *, expected_state_version: int, now: datetime
    ) -> ClaimReservation: ...

    def transition_session(
        self,
        onboarding_session_id: str,
        *,
        expected_state_version: int,
        target: BootstrapState,
        actor_type: str,
        actor_id: str | None,
        event_type: str,
        reason_code: str | None = None,
        fields: Mapping[str, object] | None = None,
    ) -> BootstrapSession: ...


__all__ = ["BootstrapStorePort"]
