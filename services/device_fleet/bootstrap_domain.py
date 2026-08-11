"""Domain primitives for the Memoria device onboarding protocol.

This module deliberately has no FastAPI or database dependency.  It owns the
wire-level rules that must stay identical when the development SQLite store is
later replaced by the Device Fleet PostgreSQL adapter:

* QR data is an exact canonical JSON envelope signed by the manufactured
  device key;
* secrets that cross the bootstrap boundary are represented by hashes after
  they are accepted by the service;
* device proof and activation ACKs sign exact, versioned payloads; and
* state transitions are explicit and state-versioned.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, cast
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class OnboardingError(Exception):
    """Base error whose code is safe to expose to a client."""

    code: str = "device_onboarding_error"
    status_code: int = 400

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class InvalidOnboardingRequest(OnboardingError):
    code = "invalid_request"
    status_code = 422


class QRFormatError(InvalidOnboardingRequest):
    code = "QR_INVALID"


class QRSignatureError(OnboardingError):
    code = "QR_SIGNATURE_INVALID"
    status_code = 422


class DeviceNotFound(OnboardingError):
    code = "DEVICE_NOT_FOUND"
    status_code = 404


class DeviceRevoked(OnboardingError):
    code = "DEVICE_REVOKED"
    status_code = 409


class DeviceAlreadyBound(OnboardingError):
    code = "DEVICE_ALREADY_BOUND"
    status_code = 409


class SessionNotFound(OnboardingError):
    code = "ONBOARDING_SESSION_NOT_FOUND"
    status_code = 404


class ClaimNotFound(OnboardingError):
    code = "CLAIM_NOT_FOUND"
    status_code = 404


class ActivationNotFound(OnboardingError):
    code = "ACTIVATION_NOT_FOUND"
    status_code = 404


class ActorMismatch(OnboardingError):
    code = "ACTOR_MISMATCH"
    status_code = 403


class SessionExpired(OnboardingError):
    code = "QR_SESSION_EXPIRED"
    status_code = 410


class ClaimExpired(OnboardingError):
    code = "CLAIM_EXPIRED"
    status_code = 410


class ClaimConflict(OnboardingError):
    code = "CLAIM_CONFLICT"
    status_code = 409


class ChallengeReplay(OnboardingError):
    code = "CHALLENGE_REPLAYED"
    status_code = 409


class InvalidDeviceProof(OnboardingError):
    code = "DEVICE_PROOF_INVALID"
    status_code = 422


class FirmwareBlocked(OnboardingError):
    code = "DEVICE_FIRMWARE_BLOCKED"
    status_code = 409


class MonotonicCounterConflict(OnboardingError):
    code = "MONOTONIC_COUNTER_REPLAYED"
    status_code = 409


class ProximityRequired(OnboardingError):
    code = "PROXIMITY_REQUIRED"
    status_code = 409


class StateVersionConflict(OnboardingError):
    code = "STATE_VERSION_CONFLICT"
    status_code = 409


class InvalidStateTransition(OnboardingError):
    code = "INVALID_STATE_TRANSITION"
    status_code = 409


class BindingConflict(OnboardingError):
    code = "BINDING_CONFLICT"
    status_code = 409


class ActivationReplay(OnboardingError):
    code = "ACTIVATION_ACK_REPLAYED"
    status_code = 409


class IntegrationUnavailable(OnboardingError):
    code = "DEVICE_ONBOARDING_INTEGRATION_UNAVAILABLE"
    status_code = 503


class DeviceMediaNotReady(OnboardingError):
    code = "DEVICE_MEDIA_NOT_READY"
    status_code = 409


class DeviceMediaChallengeExpired(OnboardingError):
    code = "DEVICE_MEDIA_CHALLENGE_EXPIRED"
    status_code = 410


class DeviceMediaChallengeRateLimited(OnboardingError):
    code = "DEVICE_MEDIA_CHALLENGE_RATE_LIMITED"
    status_code = 429


class DevelopmentRegistrationDisabled(OnboardingError):
    code = "OFFLINE_MOCK_REQUIRED"
    status_code = 403


class BootstrapState(StrEnum):
    ISSUED = "issued"
    SCANNED = "scanned"
    QR_VERIFIED = "qr_verified"
    BLE_CONNECTING = "ble_connecting"
    PROXIMITY_VERIFIED = "proximity_verified"
    WIFI_CONFIGURING = "wifi_configuring"
    WIFI_CONNECTED = "wifi_connected"
    DEVICE_ONLINE = "device_online"
    CLAIM_RESERVED = "claim_reserved"
    BINDING_COMMITTING = "binding_committing"
    BOUND = "bound"
    ACTIVATING = "activating"
    ACTIVATED = "activated"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"
    REVOKED = "revoked"
    CONFLICT = "conflict"


class DeviceLifecycle(StrEnum):
    MANUFACTURED = "manufactured"
    PROVISIONED = "provisioned"
    BOUND = "bound"
    REVOKED = "revoked"


class ClaimStatus(StrEnum):
    RESERVED = "reserved"
    BINDING_COMMITTING = "binding_committing"
    BINDING_CREATED = "binding_created"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"
    CONFLICT = "conflict"


class ActivationStatus(StrEnum):
    PENDING_MANIFEST = "pending_manifest"
    MANIFEST_READY = "manifest_ready"
    DEVICE_DOWNLOADING = "device_downloading"
    DEVICE_APPLIED = "device_applied"
    DEVICE_ACKNOWLEDGED = "device_acknowledged"
    READY_FOR_CONVERSATION = "ready_for_conversation"
    FAILED = "failed"


_STATE_TRANSITIONS: Final[dict[BootstrapState, frozenset[BootstrapState]]] = {
    BootstrapState.ISSUED: frozenset(
        {BootstrapState.SCANNED, BootstrapState.EXPIRED, BootstrapState.CANCELLED}
    ),
    BootstrapState.SCANNED: frozenset(
        {BootstrapState.QR_VERIFIED, BootstrapState.EXPIRED, BootstrapState.CANCELLED}
    ),
    BootstrapState.QR_VERIFIED: frozenset(
        {
            BootstrapState.BLE_CONNECTING,
            BootstrapState.PROXIMITY_VERIFIED,
            BootstrapState.WIFI_CONFIGURING,
            BootstrapState.WIFI_CONNECTED,
            BootstrapState.DEVICE_ONLINE,
            BootstrapState.EXPIRED,
            BootstrapState.CANCELLED,
            BootstrapState.FAILED,
        }
    ),
    BootstrapState.BLE_CONNECTING: frozenset(
        {
            BootstrapState.PROXIMITY_VERIFIED,
            BootstrapState.WIFI_CONFIGURING,
            BootstrapState.WIFI_CONNECTED,
            BootstrapState.DEVICE_ONLINE,
            BootstrapState.EXPIRED,
            BootstrapState.CANCELLED,
            BootstrapState.FAILED,
        }
    ),
    BootstrapState.PROXIMITY_VERIFIED: frozenset(
        {
            BootstrapState.WIFI_CONFIGURING,
            BootstrapState.WIFI_CONNECTED,
            BootstrapState.DEVICE_ONLINE,
            BootstrapState.EXPIRED,
            BootstrapState.CANCELLED,
            BootstrapState.FAILED,
        }
    ),
    BootstrapState.WIFI_CONFIGURING: frozenset(
        {
            BootstrapState.WIFI_CONNECTED,
            BootstrapState.DEVICE_ONLINE,
            BootstrapState.EXPIRED,
            BootstrapState.CANCELLED,
            BootstrapState.FAILED,
        }
    ),
    BootstrapState.WIFI_CONNECTED: frozenset(
        {
            BootstrapState.DEVICE_ONLINE,
            BootstrapState.EXPIRED,
            BootstrapState.CANCELLED,
            BootstrapState.FAILED,
        }
    ),
    BootstrapState.DEVICE_ONLINE: frozenset(
        {
            BootstrapState.CLAIM_RESERVED,
            BootstrapState.EXPIRED,
            BootstrapState.CANCELLED,
            BootstrapState.FAILED,
        }
    ),
    BootstrapState.CLAIM_RESERVED: frozenset(
        {
            BootstrapState.BINDING_COMMITTING,
            BootstrapState.DEVICE_ONLINE,
            BootstrapState.EXPIRED,
            BootstrapState.CANCELLED,
            BootstrapState.CONFLICT,
        }
    ),
    BootstrapState.BINDING_COMMITTING: frozenset(
        {
            BootstrapState.BOUND,
            BootstrapState.CLAIM_RESERVED,
            BootstrapState.EXPIRED,
            BootstrapState.FAILED,
            BootstrapState.CONFLICT,
        }
    ),
    BootstrapState.BOUND: frozenset(
        {BootstrapState.ACTIVATING, BootstrapState.ACTIVATED, BootstrapState.FAILED}
    ),
    BootstrapState.ACTIVATING: frozenset(
        {BootstrapState.ACTIVATED, BootstrapState.FAILED}
    ),
    BootstrapState.ACTIVATED: frozenset(),
    BootstrapState.EXPIRED: frozenset(),
    BootstrapState.CANCELLED: frozenset(),
    BootstrapState.FAILED: frozenset(),
    BootstrapState.REVOKED: frozenset(),
    BootstrapState.CONFLICT: frozenset(),
}


_BASE64URL_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BLE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._:-]{1,32}$")
_QR_PREFIX: Final[str] = "memoria-bootstrap:v1:"
_QR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "typ",
        "ver",
        "device_id",
        "bootstrap_nonce",
        "ble_name",
        "ble_service_uuid",
        "certificate_id",
        "provisioning_protocol",
        "firmware_version",
        "pop",
    }
)
_ONLINE_PROOF_KEYS: Final[frozenset[str]] = frozenset(
    {
        "device_id",
        "certificate_id",
        "bootstrap_nonce_hash",
        "mobile_nonce_hash",
        "challenge_id",
        "challenge_nonce",
        "firmware_version",
        "firmware_security_version",
        "capability_manifest_hash",
        "network_result",
        "monotonic_counter",
        "signature",
    }
)
_ACK_KEYS: Final[frozenset[str]] = frozenset(
    {
        "device_id",
        "certificate_id",
        "binding_id",
        "binding_version",
        "activation_version",
        "config_hash",
        "firmware_version",
        "monotonic_counter",
        "applied_at",
        "signature",
    }
)
_BINDING_KEYS: Final[frozenset[str]] = frozenset(
    {
        "declared_mode",
        "account_owner_person_id",
        "primary_subject",
        "persona_selection",
        "service_preferences",
        "consent_offer_ids",
    }
)
_PRIMARY_SUBJECT_KEYS: Final[frozenset[str]] = frozenset(
    {"person_id", "relationship"}
)
_SERVICE_PREFERENCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "admin_visibility",
        "english_practice_enabled",
        "interview_frequency",
        "max_session_minutes",
        "quiet_hours",
        "robot_name",
        "locale",
        "timezone",
        "memory_level",
        "quiet_hours_start",
        "quiet_hours_end",
        "shared_persona_enabled",
        "speech_speed",
        "tutor_enabled",
        "voice_style",
        "speaker_enrollment_requested",
    }
)
_FORBIDDEN_SECRET_TOKENS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "ssid",
    "wifi",
    "wlan",
    "privatekey",
    "providerkey",
    "accesstoken",
    "refreshtoken",
    "secret",
)


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    """Encode a JSON object using the protocol's one canonical representation."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidOnboardingRequest("payload is not canonical JSON data") from exc
    return encoded.encode("utf-8")


def _strict_object(raw: bytes, *, error_type: type[OnboardingError]) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise error_type("duplicate JSON field")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, OnboardingError) as exc:
        raise error_type("invalid JSON") from exc
    if not isinstance(value, dict):
        raise error_type("JSON object required")
    return cast(dict[str, object], value)


def _require_exact_keys(
    value: Mapping[str, object], expected: frozenset[str], *, context: str
) -> None:
    if set(value) != expected:
        raise InvalidOnboardingRequest(f"{context} contains an unsupported field")


def _text(value: object, *, field: str, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise InvalidOnboardingRequest(f"{field} is invalid")
    if value != value.strip():
        raise InvalidOnboardingRequest(f"{field} is invalid")
    return value


def _identifier(value: object, *, field: str) -> str:
    result = _text(value, field=field, max_length=128)
    if _ID_RE.fullmatch(result) is None:
        raise InvalidOnboardingRequest(f"{field} is invalid")
    return result


def _integer(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidOnboardingRequest(f"{field} is invalid")
    return value


def _b64url_decode(
    value: object,
    *,
    field: str,
    exact_length: int | None = None,
    minimum_length: int | None = None,
) -> bytes:
    text = _text(value, field=field, max_length=4096)
    if "=" in text or _BASE64URL_RE.fullmatch(text) is None:
        raise InvalidOnboardingRequest(f"{field} is invalid")
    try:
        decoded = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise InvalidOnboardingRequest(f"{field} is invalid") from exc
    if b64url_encode(decoded) != text:
        raise InvalidOnboardingRequest(f"{field} is not canonical")
    if exact_length is not None and len(decoded) != exact_length:
        raise InvalidOnboardingRequest(f"{field} is invalid")
    if minimum_length is not None and len(decoded) < minimum_length:
        raise InvalidOnboardingRequest(f"{field} is invalid")
    return decoded


def b64url_encode(value: bytes) -> str:
    """Encode without padding; this is the only base64 form accepted by QR."""

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(
    value: object,
    *,
    field: str = "value",
    exact_length: int | None = None,
    minimum_length: int | None = None,
) -> bytes:
    """Public strict decoder used by device proof and development tooling."""

    return _b64url_decode(
        value,
        field=field,
        exact_length=exact_length,
        minimum_length=minimum_length,
    )


def hash_b64url(value: object, *, field: str = "value") -> str:
    """Hash decoded nonce/PoP bytes, never their transport spelling."""

    return sha256_hex(b64url_decode(value, field=field))


def sha256_hex(value: bytes | str) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _sha256(value: object, *, field: str) -> str:
    result = _text(value, field=field, max_length=64)
    if _SHA256_RE.fullmatch(result) is None:
        raise InvalidOnboardingRequest(f"{field} is invalid")
    return result


def _rfc3339(value: object, *, field: str) -> str:
    result = _text(value, field=field, max_length=64)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidOnboardingRequest(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidOnboardingRequest(f"{field} is invalid")
    return result


def parse_rfc3339(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidOnboardingRequest("timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidOnboardingRequest("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _reject_sensitive_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise InvalidOnboardingRequest("object field name is invalid")
            normalized = key.lower().replace("_", "").replace("-", "")
            if any(token in normalized for token in _FORBIDDEN_SECRET_TOKENS):
                raise InvalidOnboardingRequest("sensitive field is not accepted")
            _reject_sensitive_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_sensitive_keys(child)


def _validate_preference_value(value: object, *, depth: int = 0) -> None:
    """Keep Fleet's audit copy bounded without duplicating Consent policy.

    ``CreateDeviceBindingRequest`` and ``BINDING_OFFER_CATALOG`` remain the
    authority for mode-specific preference values.  Fleet only needs a safe,
    JSON-compatible snapshot and must accept the canonical nested
    ``quiet_hours`` object used by that authority.
    """

    if depth > 3:
        raise InvalidOnboardingRequest("service_preferences is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > 256:
            raise InvalidOnboardingRequest("service_preferences is invalid")
        return
    if isinstance(value, Mapping):
        if len(value) > 16:
            raise InvalidOnboardingRequest("service_preferences is invalid")
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise InvalidOnboardingRequest("service_preferences is invalid")
            _validate_preference_value(child, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 16:
            raise InvalidOnboardingRequest("service_preferences is invalid")
        for child in value:
            _validate_preference_value(child, depth=depth + 1)
        return
    raise InvalidOnboardingRequest("service_preferences is invalid")


@dataclass(frozen=True, slots=True)
class BootstrapQRPayload:
    typ: str
    ver: int
    device_id: str
    bootstrap_nonce: str
    ble_name: str
    ble_service_uuid: str
    certificate_id: str
    provisioning_protocol: str
    firmware_version: str
    pop: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BootstrapQRPayload:
        _require_exact_keys(value, _QR_KEYS, context="QR payload")
        typ = _text(value["typ"], field="typ", max_length=64)
        ver = _integer(value["ver"], field="ver", minimum=1)
        device_id = _identifier(value["device_id"], field="device_id")
        bootstrap_nonce = _text(value["bootstrap_nonce"], field="bootstrap_nonce")
        _b64url_decode(bootstrap_nonce, field="bootstrap_nonce", exact_length=16)
        ble_name = _text(value["ble_name"], field="ble_name", max_length=32)
        if _BLE_NAME_RE.fullmatch(ble_name) is None:
            raise InvalidOnboardingRequest("ble_name is invalid")
        ble_service_uuid = _text(
            value["ble_service_uuid"], field="ble_service_uuid", max_length=36
        )
        try:
            UUID(ble_service_uuid)
        except (ValueError, AttributeError) as exc:
            raise InvalidOnboardingRequest("ble_service_uuid is invalid") from exc
        certificate_id = _identifier(value["certificate_id"], field="certificate_id")
        provisioning_protocol = _text(
            value["provisioning_protocol"], field="provisioning_protocol", max_length=64
        )
        firmware_version = _text(
            value["firmware_version"], field="firmware_version", max_length=64
        )
        pop = _text(value["pop"], field="pop")
        _b64url_decode(pop, field="pop", minimum_length=16)
        if typ != "memoria-device-bootstrap" or ver != 1:
            raise InvalidOnboardingRequest("unsupported bootstrap QR version")
        if provisioning_protocol != "memoria-provisioning/1":
            raise InvalidOnboardingRequest("unsupported provisioning protocol")
        return cls(
            typ=typ,
            ver=ver,
            device_id=device_id,
            bootstrap_nonce=bootstrap_nonce,
            ble_name=ble_name,
            ble_service_uuid=ble_service_uuid,
            certificate_id=certificate_id,
            provisioning_protocol=provisioning_protocol,
            firmware_version=firmware_version,
            pop=pop,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "typ": self.typ,
            "ver": self.ver,
            "device_id": self.device_id,
            "bootstrap_nonce": self.bootstrap_nonce,
            "ble_name": self.ble_name,
            "ble_service_uuid": self.ble_service_uuid,
            "certificate_id": self.certificate_id,
            "provisioning_protocol": self.provisioning_protocol,
            "firmware_version": self.firmware_version,
            "pop": self.pop,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True, slots=True)
class SignedBootstrapQR:
    payload: BootstrapQRPayload
    signature: bytes


def encode_bootstrap_qr(
    payload: BootstrapQRPayload | Mapping[str, object],
    signing_key: Ed25519PrivateKey,
) -> str:
    normalized = (
        payload
        if isinstance(payload, BootstrapQRPayload)
        else BootstrapQRPayload.from_mapping(payload)
    )
    encoded_payload = b64url_encode(normalized.canonical_bytes())
    signature = b64url_encode(signing_key.sign(normalized.canonical_bytes()))
    return f"{_QR_PREFIX}{encoded_payload}.{signature}"


def parse_bootstrap_qr(value: str) -> SignedBootstrapQR:
    if not isinstance(value, str) or not value.startswith(_QR_PREFIX):
        raise QRFormatError("QR prefix is invalid")
    body = value[len(_QR_PREFIX) :]
    if any(character.isspace() for character in body) or body.count(".") != 1:
        raise QRFormatError("QR envelope is invalid")
    encoded_payload, encoded_signature = body.split(".", 1)
    if not encoded_payload or not encoded_signature:
        raise QRFormatError("QR envelope is invalid")
    try:
        payload_bytes = _b64url_decode(
            encoded_payload,
            field="qr_payload",
            minimum_length=2,
        )
        signature = _b64url_decode(
            encoded_signature,
            field="qr_signature",
            exact_length=64,
        )
    except InvalidOnboardingRequest as exc:
        raise QRFormatError("QR envelope is invalid") from exc
    payload_mapping = _strict_object(payload_bytes, error_type=QRFormatError)
    try:
        payload = BootstrapQRPayload.from_mapping(payload_mapping)
    except InvalidOnboardingRequest as exc:
        raise QRFormatError("QR payload is invalid") from exc
    if payload.canonical_bytes() != payload_bytes:
        raise QRFormatError("QR payload is not canonical")
    return SignedBootstrapQR(payload=payload, signature=signature)


def verify_bootstrap_qr(
    value: str,
    public_key: Ed25519PublicKey,
) -> BootstrapQRPayload:
    parsed = parse_bootstrap_qr(value)
    try:
        public_key.verify(parsed.signature, parsed.payload.canonical_bytes())
    except (InvalidSignature, ValueError) as exc:
        raise QRSignatureError("QR signature is invalid") from exc
    return parsed.payload


@dataclass(frozen=True, slots=True)
class DeviceOnlineProof:
    device_id: str
    certificate_id: str
    bootstrap_nonce_hash: str
    mobile_nonce_hash: str
    challenge_id: str
    challenge_nonce: str
    firmware_version: str
    firmware_security_version: int
    capability_manifest_hash: str
    network_result: Mapping[str, bool]
    monotonic_counter: int
    signature: bytes

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DeviceOnlineProof:
        _require_exact_keys(value, _ONLINE_PROOF_KEYS, context="online proof")
        network_raw = value["network_result"]
        if not isinstance(network_raw, Mapping):
            raise InvalidOnboardingRequest("network_result is invalid")
        network = cast(Mapping[str, object], network_raw)
        _require_exact_keys(
            network,
            frozenset({"got_ip", "dns_ready", "tls_ready"}),
            context="network_result",
        )
        if any(not isinstance(network[key], bool) for key in network):
            raise InvalidOnboardingRequest("network_result is invalid")
        challenge_nonce = _text(value["challenge_nonce"], field="challenge_nonce")
        _b64url_decode(challenge_nonce, field="challenge_nonce", exact_length=32)
        signature = _b64url_decode(value["signature"], field="signature", exact_length=64)
        return cls(
            device_id=_identifier(value["device_id"], field="device_id"),
            certificate_id=_identifier(value["certificate_id"], field="certificate_id"),
            bootstrap_nonce_hash=_sha256(
                value["bootstrap_nonce_hash"], field="bootstrap_nonce_hash"
            ),
            mobile_nonce_hash=_sha256(value["mobile_nonce_hash"], field="mobile_nonce_hash"),
            challenge_id=_identifier(value["challenge_id"], field="challenge_id"),
            challenge_nonce=challenge_nonce,
            firmware_version=_text(
                value["firmware_version"], field="firmware_version", max_length=64
            ),
            firmware_security_version=_integer(
                value["firmware_security_version"],
                field="firmware_security_version",
                minimum=0,
            ),
            capability_manifest_hash=_sha256(
                value["capability_manifest_hash"], field="capability_manifest_hash"
            ),
            network_result={
                "got_ip": cast(bool, network["got_ip"]),
                "dns_ready": cast(bool, network["dns_ready"]),
                "tls_ready": cast(bool, network["tls_ready"]),
            },
            monotonic_counter=_integer(
                value["monotonic_counter"], field="monotonic_counter", minimum=1
            ),
            signature=signature,
        )

    def signing_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "certificate_id": self.certificate_id,
            "bootstrap_nonce_hash": self.bootstrap_nonce_hash,
            "mobile_nonce_hash": self.mobile_nonce_hash,
            "challenge_id": self.challenge_id,
            "challenge_nonce": self.challenge_nonce,
            "firmware_version": self.firmware_version,
            "firmware_security_version": self.firmware_security_version,
            "capability_manifest_hash": self.capability_manifest_hash,
            "network_result": dict(self.network_result),
            "monotonic_counter": self.monotonic_counter,
        }


@dataclass(frozen=True, slots=True)
class ActivationAck:
    device_id: str
    certificate_id: str
    binding_id: str
    binding_version: int
    activation_version: int
    config_hash: str
    firmware_version: str
    monotonic_counter: int
    applied_at: str
    signature: bytes

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ActivationAck:
        _require_exact_keys(value, _ACK_KEYS, context="activation ACK")
        return cls(
            device_id=_identifier(value["device_id"], field="device_id"),
            certificate_id=_identifier(value["certificate_id"], field="certificate_id"),
            binding_id=_identifier(value["binding_id"], field="binding_id"),
            binding_version=_integer(value["binding_version"], field="binding_version", minimum=1),
            activation_version=_integer(
                value["activation_version"], field="activation_version", minimum=1
            ),
            config_hash=_sha256(value["config_hash"], field="config_hash"),
            firmware_version=_text(
                value["firmware_version"], field="firmware_version", max_length=64
            ),
            monotonic_counter=_integer(
                value["monotonic_counter"], field="monotonic_counter", minimum=1
            ),
            applied_at=_rfc3339(value["applied_at"], field="applied_at"),
            signature=_b64url_decode(value["signature"], field="signature", exact_length=64),
        )

    def signing_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "certificate_id": self.certificate_id,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "activation_version": self.activation_version,
            "config_hash": self.config_hash,
            "firmware_version": self.firmware_version,
            "monotonic_counter": self.monotonic_counter,
            "applied_at": self.applied_at,
        }


@dataclass(frozen=True, slots=True)
class BindingInitialization:
    declared_mode: str
    account_owner_person_id: str
    primary_subject: Mapping[str, str]
    persona_selection: str
    service_preferences: Mapping[str, object]
    consent_offer_ids: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BindingInitialization:
        _reject_sensitive_keys(value)
        _require_exact_keys(value, _BINDING_KEYS, context="binding initialization")
        declared_mode = _text(value["declared_mode"], field="declared_mode", max_length=32)
        if declared_mode not in {
            "parent_for_child",
            "self_use",
            "child_for_parent",
            "family_shared",
        }:
            raise InvalidOnboardingRequest("declared_mode is invalid")
        owner = _identifier(value["account_owner_person_id"], field="account_owner_person_id")
        subject_raw = value["primary_subject"]
        if not isinstance(subject_raw, Mapping):
            raise InvalidOnboardingRequest("primary_subject is invalid")
        subject = cast(Mapping[str, object], subject_raw)
        _require_exact_keys(subject, _PRIMARY_SUBJECT_KEYS, context="primary_subject")
        subject_id = _identifier(subject["person_id"], field="primary_subject.person_id")
        relationship = _text(
            subject["relationship"], field="primary_subject.relationship", max_length=32
        )
        preferences_raw = value["service_preferences"]
        if not isinstance(preferences_raw, Mapping):
            raise InvalidOnboardingRequest("service_preferences is invalid")
        preferences = cast(Mapping[str, object], preferences_raw)
        if not set(preferences).issubset(_SERVICE_PREFERENCE_KEYS):
            raise InvalidOnboardingRequest("service_preferences contains an unsupported field")
        for key, preference in preferences.items():
            if not isinstance(key, str):
                raise InvalidOnboardingRequest("service_preferences is invalid")
            _validate_preference_value(preference)
        consent_raw = value["consent_offer_ids"]
        if not isinstance(consent_raw, (list, tuple)):
            raise InvalidOnboardingRequest("consent_offer_ids is invalid")
        consent_ids = tuple(
            _identifier(item, field="consent_offer_ids") for item in consent_raw
        )
        if len(consent_ids) > 32 or len(set(consent_ids)) != len(consent_ids):
            raise InvalidOnboardingRequest("consent_offer_ids is invalid")
        return cls(
            declared_mode=declared_mode,
            account_owner_person_id=owner,
            primary_subject={"person_id": subject_id, "relationship": relationship},
            persona_selection=_identifier(value["persona_selection"], field="persona_selection"),
            service_preferences=dict(preferences),
            consent_offer_ids=consent_ids,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "declared_mode": self.declared_mode,
            "account_owner_person_id": self.account_owner_person_id,
            "primary_subject": dict(self.primary_subject),
            "persona_selection": self.persona_selection,
            "service_preferences": dict(self.service_preferences),
            "consent_offer_ids": list(self.consent_offer_ids),
        }


def validate_activation_manifest(value: Mapping[str, object]) -> dict[str, object]:
    """Validate a signed manifest without accepting arbitrary client fields."""

    _reject_sensitive_keys(value)
    expected = frozenset(
        {
            "schema_version",
            "activation_id",
            "activation_version",
            "device_id",
            "binding_id",
            "binding_version",
            "persona_assignment_id",
            "service_profile_version",
            "policy_bundle_version",
            "runtime_profile_version",
            "locale",
            "timezone",
            "display",
            "endpoints",
            "config_hash",
            "issued_at",
            "expires_at",
            "signature",
        }
    )
    _require_exact_keys(value, expected, context="activation manifest")
    if value["schema_version"] != 1:
        raise InvalidOnboardingRequest("unsupported activation manifest version")
    for field in (
        "activation_id",
        "device_id",
        "binding_id",
        "persona_assignment_id",
        "service_profile_version",
        "policy_bundle_version",
        "locale",
        "timezone",
    ):
        _text(value[field], field=field, max_length=256)
    for field in ("binding_version", "activation_version", "runtime_profile_version"):
        _integer(value[field], field=field, minimum=1)
    _sha256(value["config_hash"], field="config_hash")
    _rfc3339(value["issued_at"], field="issued_at")
    _rfc3339(value["expires_at"], field="expires_at")
    _b64url_decode(value["signature"], field="signature", exact_length=64)
    display = value["display"]
    if not isinstance(display, Mapping):
        raise InvalidOnboardingRequest("display is invalid")
    _require_exact_keys(
        cast(Mapping[str, object], display),
        frozenset({"robot_name", "primary_subject_display_name"}),
        context="display",
    )
    for field in ("robot_name", "primary_subject_display_name"):
        _text(cast(Mapping[str, object], display)[field], field=field, max_length=128)
    endpoints = value["endpoints"]
    if not isinstance(endpoints, Mapping):
        raise InvalidOnboardingRequest("endpoints is invalid")
    _require_exact_keys(
        cast(Mapping[str, object], endpoints),
        frozenset({"control_api", "device_media"}),
        context="endpoints",
    )
    for field in ("control_api", "device_media"):
        _text(cast(Mapping[str, object], endpoints)[field], field=field, max_length=512)
    return dict(value)


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    device_id: str
    certificate_id: str
    public_key: bytes
    product_model: str
    hardware_revision: str
    firmware_version: str
    firmware_security_version: int
    capability_manifest_hash: str
    minimum_firmware_security_version: int
    lifecycle_status: DeviceLifecycle
    last_monotonic_counter: int
    binding_id: str | None
    binding_version: int | None
    actor_id: str | None
    activation_version: int
    last_activation_counter: int


@dataclass(frozen=True, slots=True)
class BootstrapSession:
    onboarding_session_id: str
    device_id: str
    actor_id: str
    client_onboarding_id: str
    qr_nonce_hash: str
    pop_hash: str
    mobile_nonce_hash: str
    protocol_version: int
    ble_name: str
    ble_service_uuid: str
    state: BootstrapState
    state_version: int
    first_seen_at: datetime
    expires_at: datetime
    proximity_verified_at: datetime | None
    wifi_connected_at: datetime | None
    device_online_at: datetime | None
    cancelled_at: datetime | None
    consumed_at: datetime | None
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class DeviceChallenge:
    challenge_id: str
    onboarding_session_id: str
    device_id: str
    nonce_hash: str
    issued_at: datetime
    expires_at: datetime
    used_at: datetime | None


@dataclass(frozen=True, slots=True)
class DeviceMediaChallenge:
    challenge_id: str
    device_id: str
    certificate_id: str
    client_id: str
    nonce_hash: str
    issued_at: datetime
    expires_at: datetime
    used_at: datetime | None


@dataclass(frozen=True, slots=True)
class ClaimReservation:
    claim_id: str
    onboarding_session_id: str
    device_id: str
    actor_id: str
    status: ClaimStatus
    idempotency_key: str
    reserved_at: datetime
    expires_at: datetime
    binding_id: str | None
    binding_version: int | None
    committed_at: datetime | None
    released_at: datetime | None


@dataclass(frozen=True, slots=True)
class BindingRecord:
    binding_id: str
    claim_id: str
    device_id: str
    actor_id: str
    binding_version: int
    status: str
    initialization: BindingInitialization
    created_at: datetime
    committed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    activation_id: str
    device_id: str
    claim_id: str
    binding_id: str
    binding_version: int
    activation_version: int
    manifest: Mapping[str, object]
    manifest_hash: str
    status: ActivationStatus
    issued_at: datetime
    expires_at: datetime
    downloaded_at: datetime | None
    applied_at: datetime | None
    acknowledged_at: datetime | None
    ack_counter: int | None


def public_key_from_bytes(value: bytes) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(value)
    except ValueError as exc:
        raise InvalidOnboardingRequest("Ed25519 public key is invalid") from exc


def verify_signed_payload(
    *,
    public_key: Ed25519PublicKey,
    payload: Mapping[str, object],
    signature: bytes,
    error: type[OnboardingError] = InvalidDeviceProof,
) -> None:
    try:
        public_key.verify(signature, canonical_json_bytes(payload))
    except (InvalidSignature, ValueError) as exc:
        raise error("device signature is invalid") from exc


def can_transition(current: BootstrapState, target: BootstrapState) -> bool:
    return current == target or target in _STATE_TRANSITIONS[current]


def require_transition(current: BootstrapState, target: BootstrapState) -> None:
    if not can_transition(current, target):
        raise InvalidStateTransition(f"cannot transition {current.value} to {target.value}")


def now_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise InvalidOnboardingRequest("clock must return a timezone-aware datetime")
    return current.astimezone(UTC)


__all__ = [
    "ActivationAck",
    "ActivationNotFound",
    "ActivationRecord",
    "ActivationReplay",
    "ActivationStatus",
    "ActorMismatch",
    "BindingConflict",
    "BindingInitialization",
    "BindingRecord",
    "BootstrapQRPayload",
    "BootstrapSession",
    "BootstrapState",
    "ChallengeReplay",
    "ClaimConflict",
    "ClaimExpired",
    "ClaimNotFound",
    "ClaimReservation",
    "ClaimStatus",
    "DeviceAlreadyBound",
    "DeviceChallenge",
    "DeviceLifecycle",
    "DeviceMediaChallenge",
    "DeviceMediaChallengeExpired",
    "DeviceMediaChallengeRateLimited",
    "DeviceMediaNotReady",
    "DeviceNotFound",
    "DeviceOnlineProof",
    "DeviceRecord",
    "DeviceRevoked",
    "DevelopmentRegistrationDisabled",
    "FirmwareBlocked",
    "IntegrationUnavailable",
    "InvalidDeviceProof",
    "InvalidOnboardingRequest",
    "InvalidStateTransition",
    "MonotonicCounterConflict",
    "OnboardingError",
    "ProximityRequired",
    "QRFormatError",
    "QRSignatureError",
    "SessionExpired",
    "SessionNotFound",
    "SignedBootstrapQR",
    "StateVersionConflict",
    "b64url_encode",
    "b64url_decode",
    "can_transition",
    "canonical_json_bytes",
    "encode_bootstrap_qr",
    "hash_b64url",
    "now_utc",
    "parse_bootstrap_qr",
    "parse_rfc3339",
    "public_key_from_bytes",
    "require_transition",
    "sha256_hex",
    "validate_activation_manifest",
    "verify_bootstrap_qr",
    "verify_signed_payload",
]
