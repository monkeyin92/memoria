"""Application service for Memoria Device Bootstrap/Claim/Activation.

The service is deliberately small and synchronous: the SQLite adapter is a
local development store, while the public methods are the integration seam for
the future PostgreSQL implementation and the existing Identity binding
authority.  No method accepts Wi-Fi credentials, a device private key, or a
raw QR/PoP value after QR verification.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from fastapi import FastAPI

from services.device_fleet.bootstrap_domain import (
    ActivationAck,
    ActivationRecord,
    ActivationStatus,
    ActorMismatch,
    BindingConflict,
    BindingInitialization,
    BindingRecord,
    BootstrapSession,
    BootstrapState,
    ChallengeReplay,
    ClaimConflict,
    ClaimNotFound,
    ClaimReservation,
    ClaimStatus,
    DevelopmentRegistrationDisabled,
    DeviceAlreadyBound,
    DeviceChallenge,
    DeviceLifecycle,
    DeviceMediaChallenge,
    DeviceMediaNotReady,
    DeviceNotFound,
    DeviceOnlineProof,
    DeviceRecord,
    DeviceRevoked,
    FirmwareBlocked,
    IntegrationUnavailable,
    InvalidDeviceProof,
    InvalidOnboardingRequest,
    ProximityRequired,
    QRSignatureError,
    SessionExpired,
    SessionNotFound,
    b64url_decode,
    b64url_encode,
    canonical_json_bytes,
    encode_bootstrap_qr,
    hash_b64url,
    now_utc,
    parse_bootstrap_qr,
    public_key_from_bytes,
    sha256_hex,
    validate_activation_manifest,
    verify_bootstrap_qr,
    verify_signed_payload,
)
from services.device_fleet.bootstrap_store import (
    BootstrapStorePort,
    SQLiteBootstrapStore,
)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _rfc3339(value: datetime) -> str:
    return now_utc(value).isoformat().replace("+00:00", "Z")


def _parse_idempotency(value: str) -> str:
    if not isinstance(value, str) or not 8 <= len(value) <= 128:
        raise InvalidOnboardingRequest("idempotency_key is invalid")
    if value != value.strip() or any(character.isspace() for character in value):
        raise InvalidOnboardingRequest("idempotency_key is invalid")
    return value


def _client_info(value: Mapping[str, object]) -> None:
    if set(value) != {"platform", "app_version", "base_library_version"}:
        raise InvalidOnboardingRequest("client contains an unsupported field")
    for field in value:
        item = value[field]
        if not isinstance(item, str) or not item or len(item) > 128 or item != item.strip():
            raise InvalidOnboardingRequest("client is invalid")


def _safe_protocol_id(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None
    ):
        raise InvalidOnboardingRequest(f"{field} is invalid")
    return value


def _client_claim_status(status: ClaimStatus) -> str:
    """Map internal reservation states to the fixed miniprogram vocabulary."""

    if status in {ClaimStatus.RESERVED, ClaimStatus.BINDING_COMMITTING}:
        return "reserved"
    if status is ClaimStatus.COMMITTED:
        return "bound"
    if status is ClaimStatus.CONFLICT:
        return "suspended"
    return "unclaimed"


@dataclass(frozen=True, slots=True)
class BindingAuthorityResult:
    """Server-authoritative result returned by Identity integration."""

    binding_id: str
    binding_version: int
    persona_assignment_id: str
    service_profile_version: str
    policy_bundle_version: str
    runtime_profile_version: int
    robot_name: str
    primary_subject_display_name: str
    control_api_endpoint: str
    device_media_endpoint: str


class BindingAuthorityPort(Protocol):
    """Adapter implemented by the existing Identity/Binding service."""

    def commit_binding(
        self,
        *,
        actor_id: str,
        claim_id: str,
        onboarding_session_id: str,
        initialization: BindingInitialization,
        idempotency_key: str,
    ) -> BindingAuthorityResult:
        """Create or return the idempotent authoritative Identity binding."""


class DeviceOnboardingService:
    """Bootstrap/Claim/Activation coordinator with explicit production gates."""

    def __init__(
        self,
        store: BootstrapStorePort,
        *,
        server_signing_key: Ed25519PrivateKey | None = None,
        binding_authority: BindingAuthorityPort | None = None,
        offline_mock: bool = False,
        now_fn: Callable[[], datetime] | None = None,
        onboarding_ttl: timedelta = timedelta(minutes=15),
        challenge_ttl: timedelta = timedelta(minutes=2),
        claim_ttl: timedelta = timedelta(minutes=10),
        activation_ttl: timedelta = timedelta(days=30),
        minimum_firmware_security_version: int = 0,
    ) -> None:
        if onboarding_ttl <= timedelta(0) or challenge_ttl <= timedelta(0):
            raise ValueError("onboarding and challenge TTLs must be positive")
        if claim_ttl <= timedelta(0) or activation_ttl <= timedelta(0):
            raise ValueError("claim and activation TTLs must be positive")
        if minimum_firmware_security_version < 0:
            raise ValueError("minimum firmware security version must be non-negative")
        if server_signing_key is None and offline_mock:
            server_signing_key = Ed25519PrivateKey.generate()
        self.store = store
        self.server_signing_key = server_signing_key
        self.binding_authority = binding_authority
        self.offline_mock = offline_mock
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.onboarding_ttl = onboarding_ttl
        self.challenge_ttl = challenge_ttl
        self.claim_ttl = claim_ttl
        self.activation_ttl = activation_ttl
        self.minimum_firmware_security_version = minimum_firmware_security_version

    @property
    def activation_public_key_b64(self) -> str:
        if self.server_signing_key is None:
            raise IntegrationUnavailable("activation signing key is not configured")
        raw = self.server_signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,  # type: ignore[arg-type]
            format=serialization.PublicFormat.Raw,  # type: ignore[arg-type]
        )
        return b64url_encode(raw)

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if callable(close):
            close()

    def _now(self) -> datetime:
        return now_utc(self.now_fn())

    def _mobile_nonce(
        self,
        *,
        actor_id: str,
        client_onboarding_id: str,
        device_id: str,
        qr_nonce_hash: str,
    ) -> str:
        """Derive a replay-stable nonce without persisting its plaintext."""

        if self.server_signing_key is None:
            raise IntegrationUnavailable("bootstrap nonce authority is unavailable")
        key = self.server_signing_key.private_bytes(
            encoding=serialization.Encoding.Raw,  # type: ignore[arg-type]
            format=serialization.PrivateFormat.Raw,  # type: ignore[arg-type]
            encryption_algorithm=serialization.NoEncryption(),
        )
        message = canonical_json_bytes(
            {
                "purpose": "device-onboarding-mobile-nonce-v1",
                "actor_id": actor_id,
                "client_onboarding_id": client_onboarding_id,
                "device_id": device_id,
                "qr_nonce_hash": qr_nonce_hash,
            }
        )
        return b64url_encode(hmac.new(key, message, hashlib.sha256).digest())

    def _device(self, device_id: str) -> DeviceRecord:
        device = cast(
            DeviceRecord | None,
            self.store.get_device(device_id),  # type: ignore[attr-defined]
        )
        if device is None:
            raise DeviceNotFound()
        return device

    def _session(self, onboarding_session_id: str) -> BootstrapSession:
        session = cast(
            BootstrapSession | None,
            self.store.get_session(onboarding_session_id),  # type: ignore[attr-defined]
        )
        if session is None:
            raise SessionNotFound()
        return session

    def _claim(self, claim_id: str) -> ClaimReservation:
        claim = cast(
            ClaimReservation | None,
            self.store.get_claim(claim_id),  # type: ignore[attr-defined]
        )
        if claim is None:
            raise ClaimNotFound()
        return claim

    @staticmethod
    def _assert_actor(expected: str, actual: str) -> None:
        if expected != actual:
            raise ActorMismatch()

    def _expire_session_if_needed(self, session: BootstrapSession) -> BootstrapSession:
        if session.expires_at <= self._now() and session.state not in {
            BootstrapState.EXPIRED,
            BootstrapState.CANCELLED,
            BootstrapState.ACTIVATED,
        }:
            return cast(
                BootstrapSession,
                self.store.expire_session(  # type: ignore[attr-defined]
                    session.onboarding_session_id, now=self._now()
                ),
            )
        return session

    # ------------------------------------------------------------------
    # Bootstrap and device proof
    # ------------------------------------------------------------------

    def introspect(
        self,
        *,
        actor_id: str,
        qr_payload: str,
        client_onboarding_id: str,
        client: Mapping[str, object],
    ) -> dict[str, object]:
        actor_id = _safe_protocol_id(actor_id, field="actor_id")
        client_onboarding_id = _safe_protocol_id(client_onboarding_id, field="client_onboarding_id")
        _client_info(client)
        parsed = parse_bootstrap_qr(qr_payload)
        payload = parsed.payload
        device = self._device(payload.device_id)
        try:
            verified_payload = verify_bootstrap_qr(
                qr_payload, public_key_from_bytes(device.public_key)
            )
        except (QRSignatureError, InvalidOnboardingRequest) as exc:
            if isinstance(exc, QRSignatureError):
                raise
            raise QRSignatureError("QR signature is invalid") from exc
        if verified_payload.certificate_id != device.certificate_id:
            raise QRSignatureError("QR certificate is not the manufactured certificate")
        if verified_payload.firmware_version != device.firmware_version:
            raise QRSignatureError("QR firmware identity is stale")
        if device.lifecycle_status is DeviceLifecycle.REVOKED:
            raise DeviceRevoked()
        if device.lifecycle_status is DeviceLifecycle.BOUND:
            raise DeviceAlreadyBound()
        qr_nonce_hash = hash_b64url(payload.bootstrap_nonce, field="bootstrap_nonce")
        mobile_nonce = self._mobile_nonce(
            actor_id=actor_id,
            client_onboarding_id=client_onboarding_id,
            device_id=device.device_id,
            qr_nonce_hash=qr_nonce_hash,
        )
        existing_client = self.store.find_session_by_client(  # type: ignore[attr-defined]
            actor_id=actor_id, client_onboarding_id=client_onboarding_id
        )
        if existing_client is not None:
            existing_client = self._expire_session_if_needed(existing_client)
            if (
                existing_client.device_id != device.device_id
                or existing_client.qr_nonce_hash != qr_nonce_hash
            ):
                raise ClaimConflict("client onboarding id has already been used")
            if existing_client.state in {
                BootstrapState.EXPIRED,
                BootstrapState.CANCELLED,
                BootstrapState.FAILED,
                BootstrapState.REVOKED,
                BootstrapState.CONFLICT,
            }:
                raise ClaimConflict("client onboarding session is terminal")
            return self._session_view(existing_client, mobile_nonce=mobile_nonce)
        active_claim = self.store.active_claim_for_device(  # type: ignore[attr-defined]
            device_id=device.device_id, now=self._now()
        )
        if active_claim is not None and active_claim.actor_id != actor_id:
            raise ClaimConflict("device is being configured by another actor")
        now = self._now()
        session = BootstrapSession(
            onboarding_session_id=_id("onb"),
            device_id=device.device_id,
            actor_id=actor_id,
            client_onboarding_id=client_onboarding_id,
            qr_nonce_hash=qr_nonce_hash,
            pop_hash=hash_b64url(payload.pop, field="pop"),
            mobile_nonce_hash=hash_b64url(mobile_nonce, field="mobile_nonce"),
            protocol_version=payload.ver,
            ble_name=payload.ble_name,
            ble_service_uuid=payload.ble_service_uuid,
            state=BootstrapState.QR_VERIFIED,
            state_version=1,
            first_seen_at=now,
            expires_at=now + self.onboarding_ttl,
            proximity_verified_at=None,
            wifi_connected_at=None,
            device_online_at=None,
            cancelled_at=None,
            consumed_at=None,
            failure_code=None,
        )
        self.store.create_session(session)  # type: ignore[attr-defined]
        return self._session_view(session, mobile_nonce=mobile_nonce)

    def _session_view(
        self,
        session: BootstrapSession,
        *,
        mobile_nonce: str | None = None,
    ) -> dict[str, object]:
        session = self._expire_session_if_needed(session)
        device = self._device(session.device_id)
        claim_status = "unclaimed"
        claim = self.store.get_claim_for_session(  # type: ignore[attr-defined]
            session.onboarding_session_id
        )
        if claim is not None:
            claim_status = _client_claim_status(claim.status)
        result: dict[str, object] = {
            "onboarding_session_id": session.onboarding_session_id,
            "state": session.state.value,
            "state_version": session.state_version,
            "expires_at": _rfc3339(session.expires_at),
            "device": {
                "device_id": device.device_id,
                "display_tail": device.device_id[-4:],
                "model": device.product_model,
                "firmware_version": device.firmware_version,
                "claim_status": claim_status,
            },
            "provisioning": {
                "transport": "ble",
                "ble_name": session.ble_name,
                "service_uuid": session.ble_service_uuid,
                "protocol_version": session.protocol_version,
            },
        }
        if mobile_nonce is not None:
            result["mobile_nonce"] = mobile_nonce
        if claim is not None:
            result["claim_id"] = claim.claim_id
            if claim.binding_id is not None:
                result["binding_id"] = claim.binding_id
        activation = self.store.latest_activation_for_device(  # type: ignore[attr-defined]
            session.device_id
        )
        if activation is not None:
            result["activation_status"] = activation.status.value
        return result

    def get_session(self, *, actor_id: str, onboarding_session_id: str) -> dict[str, object]:
        session = self._session(onboarding_session_id)
        self._assert_actor(session.actor_id, actor_id)
        return self._session_view(session)

    def cancel_session(self, *, actor_id: str, onboarding_session_id: str) -> dict[str, object]:
        session = self._session(onboarding_session_id)
        self._assert_actor(session.actor_id, actor_id)
        session = self._expire_session_if_needed(session)
        if session.state in {BootstrapState.CANCELLED, BootstrapState.EXPIRED}:
            return self._session_view(session)
        if session.state in {
            BootstrapState.BOUND,
            BootstrapState.ACTIVATING,
            BootstrapState.ACTIVATED,
        }:
            raise BindingConflict("activated onboarding cannot be cancelled")
        updated = self.store.transition_session(  # type: ignore[attr-defined]
            onboarding_session_id,
            expected_state_version=session.state_version,
            target=BootstrapState.CANCELLED,
            actor_type="user",
            actor_id=actor_id,
            event_type="session_cancelled",
            reason_code="user_cancelled",
            fields={"cancelled_at": self._now()},
        )
        return self._session_view(updated)

    def issue_challenge(
        self,
        *,
        onboarding_session_id: str,
        device_id: str,
        certificate_id: str,
    ) -> dict[str, object]:
        session = self._expire_session_if_needed(self._session(onboarding_session_id))
        device = self._device(device_id)
        if session.device_id != device_id or device.certificate_id != certificate_id:
            raise InvalidDeviceProof("device identity does not match onboarding session")
        if device.lifecycle_status is DeviceLifecycle.REVOKED:
            raise DeviceRevoked()
        if device.lifecycle_status is DeviceLifecycle.BOUND:
            raise DeviceAlreadyBound()
        if session.state is BootstrapState.QR_VERIFIED:
            session = self.store.transition_session(  # type: ignore[attr-defined]
                onboarding_session_id,
                expected_state_version=session.state_version,
                target=BootstrapState.BLE_CONNECTING,
                actor_type="device",
                actor_id=device_id,
                event_type="device_challenge_requested",
            )
        if session.state not in {
            BootstrapState.BLE_CONNECTING,
            BootstrapState.PROXIMITY_VERIFIED,
            BootstrapState.WIFI_CONFIGURING,
            BootstrapState.WIFI_CONNECTED,
            BootstrapState.DEVICE_ONLINE,
        }:
            raise InvalidDeviceProof("device challenge is not valid in this state")
        now = self._now()
        nonce = b64url_encode(secrets.token_bytes(32))
        challenge = DeviceChallenge(
            challenge_id=_id("chal"),
            onboarding_session_id=onboarding_session_id,
            device_id=device_id,
            nonce_hash=hash_b64url(nonce, field="challenge_nonce"),
            issued_at=now,
            expires_at=min(now + self.challenge_ttl, session.expires_at),
            used_at=None,
        )
        self.store.issue_challenge(challenge)  # type: ignore[attr-defined]
        return {
            "challenge_id": challenge.challenge_id,
            "nonce": nonce,
            "expires_at": _rfc3339(challenge.expires_at),
        }

    def submit_online_proof(
        self,
        *,
        onboarding_session_id: str,
        proof: DeviceOnlineProof | Mapping[str, object],
    ) -> dict[str, object]:
        normalized = (
            proof if isinstance(proof, DeviceOnlineProof) else DeviceOnlineProof.from_mapping(proof)
        )
        session = self._expire_session_if_needed(self._session(onboarding_session_id))
        device = self._device(normalized.device_id)
        if session.device_id != normalized.device_id:
            raise InvalidDeviceProof("device does not match onboarding session")
        if device.certificate_id != normalized.certificate_id:
            raise InvalidDeviceProof("certificate does not belong to device")
        if device.lifecycle_status is DeviceLifecycle.REVOKED:
            raise DeviceRevoked()
        if device.lifecycle_status is DeviceLifecycle.BOUND:
            raise DeviceAlreadyBound()
        if not all(normalized.network_result.values()):
            raise InvalidDeviceProof("device network is not ready")
        if normalized.firmware_security_version < max(
            device.minimum_firmware_security_version,
            self.minimum_firmware_security_version,
        ):
            raise FirmwareBlocked()
        if normalized.capability_manifest_hash != device.capability_manifest_hash:
            raise InvalidDeviceProof("device capability manifest is not registered")
        challenge = self.store.get_challenge(normalized.challenge_id)  # type: ignore[attr-defined]
        if challenge is None:
            raise ChallengeReplay("device challenge not found")
        if challenge.onboarding_session_id != onboarding_session_id:
            raise ChallengeReplay("device challenge does not belong to session")
        if challenge.device_id != normalized.device_id:
            raise ChallengeReplay("device challenge does not belong to device")
        if challenge.used_at is not None or challenge.expires_at <= self._now():
            raise ChallengeReplay("device challenge is no longer usable")
        if challenge.nonce_hash != hash_b64url(normalized.challenge_nonce, field="challenge_nonce"):
            raise InvalidDeviceProof("device challenge nonce does not match")
        if session.qr_nonce_hash != hash_b64url_from_qr_device_value(normalized, session):
            raise InvalidDeviceProof("bootstrap nonce does not match")
        if session.mobile_nonce_hash != normalized.mobile_nonce_hash:
            raise ProximityRequired("mobile nonce was not delivered through the nearby device")
        verify_signed_payload(
            public_key=public_key_from_bytes(device.public_key),
            payload=normalized.signing_dict(),
            signature=normalized.signature,
        )
        updated = self.store.accept_online_proof(  # type: ignore[attr-defined]
            challenge_id=normalized.challenge_id,
            onboarding_session_id=onboarding_session_id,
            device_id=normalized.device_id,
            monotonic_counter=normalized.monotonic_counter,
            firmware_version=normalized.firmware_version,
            firmware_security_version=normalized.firmware_security_version,
            now=self._now(),
        )
        return self._session_view(updated)

    # ------------------------------------------------------------------
    # Claim and binding Saga hooks
    # ------------------------------------------------------------------

    def reserve_claim(
        self,
        *,
        actor_id: str,
        onboarding_session_id: str,
        device_id: str,
        idempotency_key: str,
        expected_state_version: int,
    ) -> dict[str, object]:
        idempotency_key = _parse_idempotency(idempotency_key)
        session = self._expire_session_if_needed(self._session(onboarding_session_id))
        if session.state is BootstrapState.EXPIRED:
            raise SessionExpired()
        self._assert_actor(session.actor_id, actor_id)
        if session.device_id != device_id:
            raise ActorMismatch()
        if session.state not in {BootstrapState.DEVICE_ONLINE, BootstrapState.CLAIM_RESERVED}:
            if not session.proximity_verified_at or not session.device_online_at:
                raise ProximityRequired("nearby device proof is required")
            raise ClaimConflict("onboarding session is not ready to claim")
        if not session.proximity_verified_at or not session.device_online_at:
            raise ProximityRequired("nearby device proof is required")
        existing = self.store.get_claim_for_session(  # type: ignore[attr-defined]
            onboarding_session_id
        )
        if existing is not None and existing.status in {
            ClaimStatus.RESERVED,
            ClaimStatus.BINDING_COMMITTING,
            ClaimStatus.COMMITTED,
        }:
            if existing.actor_id != actor_id:
                raise ClaimConflict()
            if (
                existing.idempotency_key == idempotency_key
                or existing.status is ClaimStatus.COMMITTED
            ):
                return self._claim_view(existing)
        now = self._now()
        claim = ClaimReservation(
            claim_id=_id("claim"),
            onboarding_session_id=onboarding_session_id,
            device_id=device_id,
            actor_id=actor_id,
            status=ClaimStatus.RESERVED,
            idempotency_key=idempotency_key,
            reserved_at=now,
            expires_at=min(now + self.claim_ttl, session.expires_at),
            binding_id=None,
            binding_version=None,
            committed_at=None,
            released_at=None,
        )
        reserved = self.store.reserve_claim(  # type: ignore[attr-defined]
            claim, expected_state_version=expected_state_version, now=now
        )
        return self._claim_view(reserved)

    @staticmethod
    def _claim_view(claim: ClaimReservation) -> dict[str, object]:
        return {
            "claim_id": claim.claim_id,
            "onboarding_session_id": claim.onboarding_session_id,
            "device_id": claim.device_id,
            "status": claim.status.value,
            "expires_at": _rfc3339(claim.expires_at),
        }

    def get_claim(self, *, actor_id: str, claim_id: str) -> dict[str, object]:
        claim = self._claim(claim_id)
        self._assert_actor(claim.actor_id, actor_id)
        claim = self.store.expire_claim_if_needed(claim_id, now=self._now())  # type: ignore[attr-defined]
        return self._claim_view(claim)

    def binding_begin(
        self,
        *,
        actor_id: str,
        claim_id: str,
        onboarding_session_id: str,
        initialization: BindingInitialization | Mapping[str, object],
        idempotency_key: str,
        expected_state_version: int | None = None,
        binding_id: str | None = None,
        binding_version: int = 1,
    ) -> dict[str, object]:
        claim = self._claim(claim_id)
        self._assert_actor(claim.actor_id, actor_id)
        if claim.onboarding_session_id != onboarding_session_id:
            raise ClaimConflict("claim does not belong to onboarding session")
        init = (
            initialization
            if isinstance(initialization, BindingInitialization)
            else BindingInitialization.from_mapping(initialization)
        )
        if init.account_owner_person_id != actor_id:
            raise ActorMismatch()
        key = _parse_idempotency(idempotency_key)
        session = self._expire_session_if_needed(self._session(onboarding_session_id))
        if expected_state_version is None:
            expected_state_version = session.state_version
        if binding_version < 1:
            raise InvalidOnboardingRequest("binding_version is invalid")
        binding = BindingRecord(
            binding_id=claim.binding_id
            or (
                _safe_protocol_id(binding_id, field="binding_id")
                if binding_id is not None
                else _id("bind")
            ),
            claim_id=claim_id,
            device_id=claim.device_id,
            actor_id=actor_id,
            binding_version=claim.binding_version or binding_version,
            status="draft",
            initialization=init,
            created_at=self._now(),
            committed_at=None,
        )
        draft = self.store.begin_binding(  # type: ignore[attr-defined]
            binding=binding,
            idempotency_key=key,
            expected_state_version=expected_state_version,
            now=self._now(),
        )
        updated_session = self._session(onboarding_session_id)
        return {
            "claim_id": claim_id,
            "onboarding_session_id": onboarding_session_id,
            "binding_id": draft.binding_id,
            "binding_version": draft.binding_version,
            "status": "binding_committing",
            "state_version": updated_session.state_version,
            "expires_at": _rfc3339(claim.expires_at),
        }

    def binding_release(
        self, *, actor_id: str, claim_id: str, reason: str = "user_released"
    ) -> dict[str, object]:
        if not isinstance(reason, str) or not reason or len(reason) > 128:
            raise InvalidOnboardingRequest("reason is invalid")
        claim = self._claim(claim_id)
        self._assert_actor(claim.actor_id, actor_id)
        released = self.store.release_binding(  # type: ignore[attr-defined]
            claim_id=claim_id,
            actor_id=actor_id,
            reason_code=reason,
            now=self._now(),
        )
        return self._claim_view(released)

    def _local_binding_authority(
        self, *, binding: BindingRecord, device: DeviceRecord
    ) -> BindingAuthorityResult:
        if not self.offline_mock:
            raise IntegrationUnavailable(
                "Identity binding authority is required outside offline_mock"
            )
        preferences = binding.initialization.service_preferences
        robot_name = str(preferences.get("robot_name", "Memoria"))
        return BindingAuthorityResult(
            binding_id=binding.binding_id,
            binding_version=binding.binding_version,
            persona_assignment_id=binding.initialization.persona_selection,
            service_profile_version=f"{binding.initialization.declared_mode}-v1",
            policy_bundle_version="multi-subject-v1",
            runtime_profile_version=1,
            robot_name=robot_name,
            primary_subject_display_name="主要使用者",
            control_api_endpoint="https://control.invalid",
            device_media_endpoint="wss://media.invalid",
        )

    def _binding_authority_result(self, *, binding: BindingRecord) -> BindingAuthorityResult:
        if self.binding_authority is not None:
            result = self.binding_authority.commit_binding(
                actor_id=binding.actor_id,
                claim_id=binding.claim_id,
                onboarding_session_id=self._claim(binding.claim_id).onboarding_session_id,
                initialization=binding.initialization,
                idempotency_key=binding.claim_id,
            )
            if not isinstance(result, BindingAuthorityResult):
                raise IntegrationUnavailable("binding authority returned an invalid result")
            if (
                result.binding_id != binding.binding_id
                or result.binding_version != binding.binding_version
            ):
                raise BindingConflict("binding authority result does not match claim")
            return result
        return self._local_binding_authority(
            binding=binding, device=self._device(binding.device_id)
        )

    def _commit_binding_with_authority(
        self,
        *,
        actor_id: str,
        claim: ClaimReservation,
        binding: BindingRecord,
        authority: BindingAuthorityResult,
    ) -> dict[str, object]:
        if authority.binding_id != binding.binding_id:
            raise BindingConflict("binding authority result does not match claim")
        if authority.binding_version != binding.binding_version:
            raise BindingConflict("binding authority result does not match claim")
        if authority.runtime_profile_version < 1:
            raise BindingConflict("binding authority returned an invalid runtime version")
        for value in (
            authority.persona_assignment_id,
            authority.service_profile_version,
            authority.policy_bundle_version,
            authority.robot_name,
            authority.primary_subject_display_name,
            authority.control_api_endpoint,
            authority.device_media_endpoint,
        ):
            if not isinstance(value, str) or not value or len(value) > 512:
                raise BindingConflict("binding authority returned an invalid result")
        if claim.status is ClaimStatus.COMMITTED:
            activation = self.store.latest_activation_for_device(claim.device_id)  # type: ignore[attr-defined]
            if activation is None:
                raise BindingConflict("committed claim is missing activation")
            return self._binding_commit_view(binding, activation)
        signing_key = self.server_signing_key
        if signing_key is None:
            raise IntegrationUnavailable("activation signing key is required in production")
        device = self._device(binding.device_id)
        activation_version = device.activation_version + 1
        now = self._now()
        expires_at = now + self.activation_ttl
        base_manifest: dict[str, object] = {
            "schema_version": 1,
            "activation_id": _id("act"),
            "activation_version": activation_version,
            "device_id": device.device_id,
            "binding_id": authority.binding_id,
            "binding_version": authority.binding_version,
            "persona_assignment_id": authority.persona_assignment_id,
            "service_profile_version": authority.service_profile_version,
            "policy_bundle_version": authority.policy_bundle_version,
            "runtime_profile_version": authority.runtime_profile_version,
            "locale": str(binding.initialization.service_preferences.get("locale", "zh-CN")),
            "timezone": str(
                binding.initialization.service_preferences.get("timezone", "Asia/Shanghai")
            ),
            "display": {
                "robot_name": authority.robot_name,
                "primary_subject_display_name": authority.primary_subject_display_name,
            },
            "endpoints": {
                "control_api": authority.control_api_endpoint,
                "device_media": authority.device_media_endpoint,
            },
            "issued_at": _rfc3339(now),
            "expires_at": _rfc3339(expires_at),
        }
        config_hash = sha256_hex(canonical_json_bytes(base_manifest))
        unsigned_manifest = {**base_manifest, "config_hash": config_hash}
        signature = b64url_encode(signing_key.sign(canonical_json_bytes(unsigned_manifest)))
        manifest = {**unsigned_manifest, "signature": signature}
        validate_activation_manifest(manifest)
        manifest_hash = sha256_hex(canonical_json_bytes(manifest))
        committed_binding, activation = self.store.commit_binding(  # type: ignore[attr-defined]
            claim_id=claim.claim_id,
            actor_id=actor_id,
            binding_id=authority.binding_id,
            binding_version=authority.binding_version,
            manifest=manifest,
            manifest_hash=manifest_hash,
            activation_id=str(manifest["activation_id"]),
            activation_version=activation_version,
            activation_expires_at=expires_at,
            now=now,
        )
        return self._binding_commit_view(committed_binding, activation)

    def binding_commit_with_authority(
        self,
        *,
        actor_id: str,
        claim_id: str,
        authority: BindingAuthorityResult,
    ) -> dict[str, object]:
        """Finalize Fleet after the async Identity authority has committed.

        The Control API owns the async Identity call.  Passing its immutable
        result here avoids a fake synchronous adapter and makes retries resume
        from the persisted ``binding_committing`` state.
        """

        claim = self._claim(claim_id)
        self._assert_actor(claim.actor_id, actor_id)
        binding = self.store.adopt_binding_authority(  # type: ignore[attr-defined]
            claim_id=claim_id,
            actor_id=actor_id,
            binding_id=authority.binding_id,
            binding_version=authority.binding_version,
        )
        if binding is None:
            raise BindingConflict("binding begin is required")
        return self._commit_binding_with_authority(
            actor_id=actor_id,
            claim=claim,
            binding=binding,
            authority=authority,
        )

    def get_binding_intent(self, *, actor_id: str, claim_id: str) -> BindingRecord | None:
        """Return the persisted Saga intent for an in-process Control adapter."""

        claim = self._claim(claim_id)
        self._assert_actor(claim.actor_id, actor_id)
        return self.store.get_binding_for_claim(claim_id)  # type: ignore[attr-defined,no-any-return]

    def binding_commit(self, *, actor_id: str, claim_id: str) -> dict[str, object]:
        claim = self._claim(claim_id)
        self._assert_actor(claim.actor_id, actor_id)
        binding = self.store.get_binding_for_claim(claim_id)  # type: ignore[attr-defined]
        if binding is None:
            raise BindingConflict("binding begin is required")
        if claim.status is ClaimStatus.COMMITTED:
            activation = self.store.latest_activation_for_device(claim.device_id)  # type: ignore[attr-defined]
            if activation is None:
                raise BindingConflict("committed claim is missing activation")
            return self._binding_commit_view(binding, activation)
        authority = self._binding_authority_result(binding=binding)
        return self._commit_binding_with_authority(
            actor_id=actor_id,
            claim=claim,
            binding=binding,
            authority=authority,
        )

    def create_binding(
        self,
        *,
        actor_id: str,
        claim_id: str,
        onboarding_session_id: str,
        initialization: BindingInitialization | Mapping[str, object],
        idempotency_key: str,
        expected_state_version: int | None = None,
    ) -> dict[str, object]:
        if (self.binding_authority is None and not self.offline_mock) or (
            self.server_signing_key is None
        ):
            raise IntegrationUnavailable(
                "Identity binding authority and activation signing key are required"
            )
        self.binding_begin(
            actor_id=actor_id,
            claim_id=claim_id,
            onboarding_session_id=onboarding_session_id,
            initialization=initialization,
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        return self.binding_commit(actor_id=actor_id, claim_id=claim_id)

    # Explicit aliases used by the main application integration.
    begin_binding = binding_begin
    release_binding = binding_release
    commit_binding = binding_commit
    create_device_binding = create_binding

    @staticmethod
    def _binding_commit_view(
        binding: BindingRecord, activation: ActivationRecord
    ) -> dict[str, object]:
        return {
            "binding_id": binding.binding_id,
            "binding_version": binding.binding_version,
            "claim_id": binding.claim_id,
            "status": "committed",
            "activation": {
                "activation_id": activation.activation_id,
                "activation_version": activation.activation_version,
                "status": activation.status.value,
                "config_hash": activation.manifest["config_hash"],
                "expires_at": _rfc3339(activation.expires_at),
            },
            "manifest": dict(activation.manifest),
        }

    # ------------------------------------------------------------------
    # Activation device/user APIs
    # ------------------------------------------------------------------

    def get_activation_status(self, *, actor_id: str, device_id: str) -> dict[str, object]:
        if not self.store.is_actor_bound_to_device(actor_id=actor_id, device_id=device_id):  # type: ignore[attr-defined]
            raise ActorMismatch()
        activation = self.store.latest_activation_for_device(device_id)  # type: ignore[attr-defined]
        if activation is None:
            raise IntegrationUnavailable("activation has not been created")
        return self._activation_status_view(activation)

    @staticmethod
    def _activation_status_view(activation: ActivationRecord) -> dict[str, object]:
        return {
            "activation_id": activation.activation_id,
            "device_id": activation.device_id,
            "binding_id": activation.binding_id,
            "binding_version": activation.binding_version,
            "activation_version": activation.activation_version,
            "status": activation.status.value,
            "config_hash": activation.manifest["config_hash"],
            "acknowledged_at": _rfc3339(activation.acknowledged_at)
            if activation.acknowledged_at
            else None,
        }

    @staticmethod
    def device_manifest_request_payload(device_id: str, certificate_id: str) -> dict[str, object]:
        return {
            "method": "GET",
            "path": f"/v1/devices/{device_id}/activation-manifest",
            "device_id": device_id,
            "certificate_id": certificate_id,
        }

    def get_activation_manifest(
        self,
        *,
        device_id: str,
        certificate_id: str,
        request_signature: bytes | None = None,
    ) -> dict[str, object]:
        device = self._device(device_id)
        if device.certificate_id != certificate_id:
            raise InvalidDeviceProof("certificate does not belong to device")
        if device.lifecycle_status is DeviceLifecycle.REVOKED:
            raise DeviceRevoked()
        if device.lifecycle_status is not DeviceLifecycle.BOUND:
            raise BindingConflict("device is not bound")
        if request_signature is None and not self.offline_mock:
            raise InvalidDeviceProof("device request signature is required")
        if request_signature is not None:
            verify_signed_payload(
                public_key=public_key_from_bytes(device.public_key),
                payload=self.device_manifest_request_payload(device_id, certificate_id),
                signature=request_signature,
            )
        activation = self.store.mark_activation_downloaded(  # type: ignore[attr-defined]
            device_id=device_id, now=self._now()
        )
        return dict(activation.manifest)

    def accept_activation_ack(
        self,
        *,
        device_id: str,
        ack: ActivationAck | Mapping[str, object],
    ) -> dict[str, object]:
        normalized = ack if isinstance(ack, ActivationAck) else ActivationAck.from_mapping(ack)
        device = self._device(device_id)
        if normalized.device_id != device_id or normalized.certificate_id != device.certificate_id:
            raise InvalidDeviceProof("activation ACK device identity does not match")
        if device.lifecycle_status is not DeviceLifecycle.BOUND:
            raise BindingConflict("device is not bound")
        verify_signed_payload(
            public_key=public_key_from_bytes(device.public_key),
            payload=normalized.signing_dict(),
            signature=normalized.signature,
        )
        payload = normalized.signing_dict()
        accepted = self.store.accept_activation_ack(  # type: ignore[attr-defined]
            ack_payload=payload,
            now=self._now(),
        )
        return self._activation_status_view(accepted)

    # ------------------------------------------------------------------
    # Post-activation device media authentication
    # ------------------------------------------------------------------

    @staticmethod
    def media_challenge_signing_payload(
        *,
        challenge_id: str,
        device_id: str,
        certificate_id: str,
        client_id: str,
        nonce: str,
        issued_at: datetime,
    ) -> dict[str, object]:
        return {
            "type": "memoria-device-media-auth",
            "version": 1,
            "challenge_id": challenge_id,
            "device_id": device_id,
            "certificate_id": certificate_id,
            "client_id": client_id,
            "nonce": nonce,
            "issued_at": _rfc3339(issued_at),
        }

    def _assert_device_media_ready(self, device: DeviceRecord) -> ActivationRecord:
        if device.lifecycle_status is DeviceLifecycle.REVOKED:
            raise DeviceRevoked()
        if (
            device.lifecycle_status is not DeviceLifecycle.BOUND
            or device.actor_id is None
            or device.binding_id is None
            or device.binding_version is None
        ):
            raise DeviceMediaNotReady("device has no active binding")
        activation = cast(
            ActivationRecord | None,
            self.store.latest_activation_for_device(device.device_id),  # type: ignore[attr-defined]
        )
        if (
            activation is None
            or activation.status is not ActivationStatus.READY_FOR_CONVERSATION
            or activation.binding_id != device.binding_id
            or activation.binding_version != device.binding_version
        ):
            raise DeviceMediaNotReady("device activation is not ready for conversation")
        return activation

    def issue_media_challenge(
        self,
        *,
        device_id: str,
        certificate_id: str,
        client_id: str,
    ) -> dict[str, object]:
        device_id = _safe_protocol_id(device_id, field="device_id")
        certificate_id = _safe_protocol_id(certificate_id, field="certificate_id")
        client_id = _safe_protocol_id(client_id, field="client_id")
        device = self._device(device_id)
        if device.certificate_id != certificate_id:
            raise InvalidDeviceProof("certificate does not belong to device")
        self._assert_device_media_ready(device)
        issued_at = self._now()
        nonce = b64url_encode(secrets.token_bytes(32))
        challenge = DeviceMediaChallenge(
            challenge_id=_id("media_chal"),
            device_id=device_id,
            certificate_id=certificate_id,
            client_id=client_id,
            nonce_hash=hash_b64url(nonce, field="media_challenge_nonce"),
            issued_at=issued_at,
            expires_at=issued_at + self.challenge_ttl,
            used_at=None,
        )
        self.store.issue_media_challenge(challenge)  # type: ignore[attr-defined]
        return {
            "challenge_id": challenge.challenge_id,
            "device_id": device_id,
            "certificate_id": certificate_id,
            "client_id": client_id,
            "nonce": nonce,
            "issued_at": _rfc3339(challenge.issued_at),
            "expires_at": _rfc3339(challenge.expires_at),
            "algorithm": "Ed25519",
        }

    def authenticate_media_challenge(
        self,
        *,
        device_id: str,
        certificate_id: str,
        client_id: str,
        challenge_id: str,
        nonce: str,
        signature: bytes,
    ) -> dict[str, object]:
        device_id = _safe_protocol_id(device_id, field="device_id")
        certificate_id = _safe_protocol_id(certificate_id, field="certificate_id")
        client_id = _safe_protocol_id(client_id, field="client_id")
        challenge_id = _safe_protocol_id(challenge_id, field="challenge_id")
        device = self._device(device_id)
        if device.certificate_id != certificate_id:
            raise InvalidDeviceProof("certificate does not belong to device")
        self._assert_device_media_ready(device)
        challenge = cast(
            DeviceMediaChallenge | None,
            self.store.get_media_challenge(challenge_id),  # type: ignore[attr-defined]
        )
        if challenge is None:
            raise ChallengeReplay("device media challenge was not found")
        if (
            challenge.device_id != device_id
            or challenge.certificate_id != certificate_id
            or challenge.client_id != client_id
            or challenge.nonce_hash != hash_b64url(nonce, field="media_challenge_nonce")
        ):
            raise InvalidDeviceProof("device media challenge does not match")
        if challenge.used_at is not None or challenge.expires_at <= self._now():
            raise ChallengeReplay("device media challenge is no longer usable")
        verify_signed_payload(
            public_key=public_key_from_bytes(device.public_key),
            payload=self.media_challenge_signing_payload(
                challenge_id=challenge.challenge_id,
                device_id=device_id,
                certificate_id=certificate_id,
                client_id=client_id,
                nonce=nonce,
                issued_at=challenge.issued_at,
            ),
            signature=signature,
        )
        self.store.consume_media_challenge(  # type: ignore[attr-defined]
            challenge_id=challenge.challenge_id,
            device_id=device_id,
            nonce_hash=challenge.nonce_hash,
            now=self._now(),
        )
        current = self._device(device_id)
        activation = self._assert_device_media_ready(current)
        binding = cast(
            BindingRecord | None,
            self.store.get_binding(current.binding_id),  # type: ignore[attr-defined]
        )
        if binding is None:
            raise BindingConflict("active device binding is unavailable")
        subject_id = str(binding.initialization.primary_subject.get("person_id", ""))
        if not subject_id:
            raise BindingConflict("active device binding has no primary subject")
        runtime_profile_version = int(cast(int, activation.manifest["runtime_profile_version"]))
        if runtime_profile_version < 1:
            raise BindingConflict("activation runtime profile version is invalid")
        assert current.actor_id is not None
        assert current.binding_id is not None
        assert current.binding_version is not None
        return {
            "actor_id": current.actor_id,
            "device_id": current.device_id,
            "binding_id": current.binding_id,
            "binding_version": current.binding_version,
            "subject_id": subject_id,
            "runtime_profile_version": runtime_profile_version,
            "activation_id": activation.activation_id,
            "activation_version": activation.activation_version,
            "firmware_version": current.firmware_version,
            "board_profile": current.product_model,
        }

    # ------------------------------------------------------------------
    # Controlled offline development registration
    # ------------------------------------------------------------------

    def register_offline_mock_device(
        self,
        *,
        device_id: str,
        certificate_id: str,
        public_key_b64: str,
        product_model: str,
        hardware_revision: str,
        firmware_version: str,
        firmware_security_version: int,
        capability_manifest_hash: str,
        minimum_firmware_security_version: int = 0,
    ) -> dict[str, object]:
        if not self.offline_mock:
            raise DevelopmentRegistrationDisabled()
        public_key = b64url_decode(public_key_b64, field="public_key", exact_length=32)
        if firmware_security_version < minimum_firmware_security_version:
            raise FirmwareBlocked()
        record = DeviceRecord(
            device_id=_safe_protocol_id(device_id, field="device_id"),
            certificate_id=_safe_protocol_id(certificate_id, field="certificate_id"),
            public_key=public_key,
            product_model=_safe_protocol_id(product_model, field="product_model"),
            hardware_revision=_safe_protocol_id(hardware_revision, field="hardware_revision"),
            firmware_version=_safe_protocol_id(firmware_version, field="firmware_version"),
            firmware_security_version=firmware_security_version,
            capability_manifest_hash=capability_manifest_hash,
            minimum_firmware_security_version=minimum_firmware_security_version,
            lifecycle_status=DeviceLifecycle.MANUFACTURED,
            last_monotonic_counter=0,
            binding_id=None,
            binding_version=None,
            actor_id=None,
            activation_version=0,
            last_activation_counter=0,
        )
        self.store.register_manufactured_device(record)  # type: ignore[attr-defined]
        return {
            "device_id": record.device_id,
            "certificate_id": record.certificate_id,
            "public_key": public_key_b64,
            "lifecycle_status": record.lifecycle_status.value,
        }


def create_device_onboarding_service(
    *,
    database_path: str | Path = ":memory:",
    server_signing_key: Ed25519PrivateKey | None = None,
    binding_authority: BindingAuthorityPort | None = None,
    offline_mock: bool = False,
    now_fn: Callable[[], datetime] | None = None,
    minimum_firmware_security_version: int = 0,
) -> DeviceOnboardingService:
    """Concise main.py factory; production needs explicit key + binding port."""

    store = SQLiteBootstrapStore(database_path)
    return DeviceOnboardingService(
        store,
        server_signing_key=server_signing_key,
        binding_authority=binding_authority,
        offline_mock=offline_mock,
        now_fn=now_fn,
        minimum_firmware_security_version=minimum_firmware_security_version,
    )


def install_device_onboarding(
    app: FastAPI,
    service: DeviceOnboardingService,
) -> DeviceOnboardingService:
    """Install the service under the route's sole application-state contract."""

    app.state.device_onboarding_service = service
    return service


def hash_b64url_from_qr_device_value(
    proof: DeviceOnlineProof,
    session: BootstrapSession,
) -> str:
    """Return the proof's bootstrap hash after checking its shape.

    The device proof intentionally carries only a hash.  The QR nonce itself
    is never returned by this service after introspection, so the session hash
    is the comparison authority.  Keeping this helper separate makes that
    boundary visible to future PG adapters.
    """

    # The proof's hash is compared by the caller; this helper exists to keep
    # the proof API explicit without accepting a raw QR nonce.
    del session
    return proof.bootstrap_nonce_hash


__all__ = [
    "BindingAuthorityPort",
    "BindingAuthorityResult",
    "DeviceOnboardingService",
    "create_device_onboarding_service",
    "encode_bootstrap_qr",
    "install_device_onboarding",
]
