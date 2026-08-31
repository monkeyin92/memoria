"""Server-authoritative device control plane.

Extends the existing MemoryStore/identity/binding authorities with:

- a monotonic per-device runtime profile version ledger (PR-17),
- device settings with an acoustic-capability ladder (PR-18),
- server-side acoustic capability registration (plan section 10.4),
- short-lived device control intents (wifi reset).

No parallel media or voice state lives here; every field is projected from
the authorities above or derived deterministically from them.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from services.archive.domain import canonical_payload
from services.control_api.app.database import MemoryStore
from services.control_api.app.wake_words import DEFAULT_WAKE_WORD_ID, WAKE_WORD_IDS

AUDIO_MODES = ("full_duplex_verified", "interrupt_assist", "half_duplex_safe")
WAKE_MODES = ("button", "keyword", "button_or_keyword")
BARGE_IN_KINDS = ("none", "button", "keyword", "voice")
LEARNING_MODES = ("off", "tutor_english", "tutor_homework")

DEFAULT_DEVICE_SETTINGS: dict[str, object] = {
    "volume_limit": 72,
    "screen_brightness": 80,
    "night_mode": False,
    "do_not_disturb": False,
    "learning_mode": "off",
    "audio_mode": "half_duplex_safe",
    "wake_mode": "button_or_keyword",
    "wake_word_id": DEFAULT_WAKE_WORD_ID,
    "allowed_barge_in": ["button", "keyword"],
}

_STABLE_PROFILE_KEYS = (
    "binding_version",
    "active_subject_id",
    "subject_revision",
    "subject_category",
    "age_band",
    "speaker_state",
    "speaker_confidence",
    "service_mode",
    "persona_assignment_id",
    "persona",
    "policy_bundle_version",
    "capabilities",
    "obligations",
    "policy_receipt_ids",
)


class DeviceControlError(RuntimeError):
    """Base error for the device control plane."""


class DeviceSettingsConflictError(DeviceControlError):
    pass


class AudioModeGateError(DeviceControlError):
    pass


class ProfileAckConflictError(DeviceControlError):
    pass


def stable_profile_fingerprint(payload: Mapping[str, Any]) -> str:
    """Fingerprint only the semantic identity of a runtime profile.

    Volatile issuance fields (runtime_profile_id, session_epoch, issued_at,
    expires_at, signature) are excluded so a plain refresh does not advance
    the device-visible profile version.
    """

    stable = {key: payload.get(key) for key in _STABLE_PROFILE_KEYS}
    return hashlib.sha256(canonical_payload(stable).encode("utf-8")).hexdigest()


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class RuntimeProfileLedgerEntry:
    device_id: str
    profile_version: int
    runtime_profile_id: str | None
    content_fingerprint: str
    profile_fingerprint: str
    settings_fingerprint: str
    issued_at: datetime
    expires_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "profile_version": self.profile_version,
            "runtime_profile_id": self.runtime_profile_id,
            "content_fingerprint": self.content_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "settings_fingerprint": self.settings_fingerprint,
            "issued_at": _iso(self.issued_at),
            "expires_at": _iso(self.expires_at) if self.expires_at is not None else None,
        }


class RuntimeProfileLedger:
    """Monotonic per-device profile version with ACK regression gates."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def current(self, device_id: str) -> RuntimeProfileLedgerEntry | None:
        row = self._store.get_device_runtime_profile_ledger(device_id=device_id)
        if row is None:
            return None
        return _ledger_entry_from_row(row)

    def observe(
        self,
        *,
        device_id: str,
        runtime_profile_id: str,
        content_fingerprint: str,
        issued_at: datetime,
        expires_at: datetime,
        now: datetime,
    ) -> RuntimeProfileLedgerEntry:
        """Record a profile issuance; advance the version only on semantic change."""

        stored = self._store.upsert_device_runtime_profile_ledger(
            device_id=device_id,
            component="profile",
            runtime_profile_id=runtime_profile_id,
            component_fingerprint=content_fingerprint,
            issued_at=_iso(now),
            expires_at=_iso(expires_at),
        )
        return _ledger_entry_from_row(stored)

    def advance(
        self,
        *,
        device_id: str,
        reason_fingerprint: str,
        now: datetime,
    ) -> RuntimeProfileLedgerEntry:
        """Bump the shared device version for a non-profile config change."""

        stored = self._store.upsert_device_runtime_profile_ledger(
            device_id=device_id,
            component="settings",
            runtime_profile_id=None,
            component_fingerprint=reason_fingerprint,
            issued_at=_iso(now),
            expires_at=_iso(existing.expires_at)
            if (existing := self.current(device_id)) is not None and existing.expires_at
            else None,
        )
        return _ledger_entry_from_row(stored)

    def observe_config_change(
        self,
        *,
        device_id: str,
        content_fingerprint: str,
        now: datetime,
    ) -> RuntimeProfileLedgerEntry:
        """Bump the shared device profile version only when the change is new.

        Device settings (including learning_mode) are versioned here so every
        semantic change reaches the device as a new runtime_profile_version
        while idempotent replays keep the existing version.
        """

        existing = self.current(device_id)
        if existing is not None and existing.settings_fingerprint == content_fingerprint:
            return existing
        return self.advance(
            device_id=device_id,
            reason_fingerprint=content_fingerprint,
            now=now,
        )

    def record_ack(
        self,
        *,
        device_id: str,
        profile_version: int,
        runtime_profile_id: str | None,
        acked_by: str,
        accepted: bool,
        now: datetime,
    ) -> dict[str, object]:
        """Accept one device ACK with regression and future-version gates."""

        current = self.current(device_id)
        if current is None:
            raise ProfileAckConflictError("runtime_profile_not_issued")
        if profile_version > current.profile_version:
            raise ProfileAckConflictError("ack_ahead_of_issued_version")
        last = self._store.last_device_profile_ack(device_id=device_id)
        replayed = False
        if last is not None:
            last_version = int(last["profile_version"])
            if profile_version < last_version:
                raise ProfileAckConflictError("ack_regression")
            if profile_version == last_version:
                if bool(last["accepted"]) != accepted:
                    raise ProfileAckConflictError("ack_conflict")
                replayed = True
        stored = self._store.record_device_profile_ack(
            device_id=device_id,
            profile_version=profile_version,
            runtime_profile_id=runtime_profile_id,
            acked_by=acked_by,
            acked_at=_iso(now),
            accepted=accepted,
        )
        replayed = replayed or not stored
        return {
            "device_id": device_id,
            "profile_version": profile_version,
            "runtime_profile_id": runtime_profile_id,
            "accepted": accepted,
            "acked_by": acked_by,
            "acked_at": _iso(now),
            "replayed": replayed,
            "current_version": current.profile_version,
        }


def _ledger_entry_from_row(row: Mapping[str, Any]) -> RuntimeProfileLedgerEntry:
    return RuntimeProfileLedgerEntry(
        device_id=str(row["device_id"]),
        profile_version=int(row["profile_version"]),
        runtime_profile_id=(
            str(row["runtime_profile_id"]) if row["runtime_profile_id"] is not None else None
        ),
        content_fingerprint=str(row["content_fingerprint"]),
        profile_fingerprint=str(row.get("profile_fingerprint", "")),
        settings_fingerprint=str(row.get("settings_fingerprint", "")),
        issued_at=_utc(str(row["issued_at"])),
        expires_at=_utc(str(row["expires_at"])) if row["expires_at"] is not None else None,
    )


@dataclass(frozen=True, slots=True)
class DeviceSettings:
    device_id: str
    settings_version: int
    volume_limit: int
    screen_brightness: int
    night_mode: bool
    do_not_disturb: bool
    learning_mode: str
    audio_mode: str
    wake_mode: str
    wake_word_id: str
    allowed_barge_in: tuple[str, ...]
    updated_by: str
    updated_at: datetime
    update_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "settings_version": self.settings_version,
            "volume_limit": self.volume_limit,
            "screen_brightness": self.screen_brightness,
            "night_mode": self.night_mode,
            "do_not_disturb": self.do_not_disturb,
            "learning_mode": self.learning_mode,
            "audio_mode": self.audio_mode,
            "wake_mode": self.wake_mode,
            "wake_word_id": self.wake_word_id,
            "allowed_barge_in": list(self.allowed_barge_in),
            "updated_by": self.updated_by,
            "updated_at": _iso(self.updated_at),
            "update_reason": self.update_reason,
        }


def _settings_from_row(device_id: str, row: Mapping[str, Any]) -> DeviceSettings:
    raw = json.loads(str(row["settings_json"]))
    return DeviceSettings(
        device_id=device_id,
        settings_version=int(row["settings_version"]),
        volume_limit=int(raw["volume_limit"]),
        screen_brightness=int(raw["screen_brightness"]),
        night_mode=bool(raw["night_mode"]),
        do_not_disturb=bool(raw["do_not_disturb"]),
        learning_mode=str(raw.get("learning_mode", "off")),
        audio_mode=str(raw["audio_mode"]),
        wake_mode=str(raw["wake_mode"]),
        wake_word_id=str(raw.get("wake_word_id", DEFAULT_WAKE_WORD_ID)),
        allowed_barge_in=tuple(str(item) for item in raw["allowed_barge_in"]),
        updated_by=str(row["updated_by"]),
        updated_at=_utc(str(row["updated_at"])),
        update_reason=str(row["update_reason"]),
    )


def default_settings(device_id: str, *, now: datetime) -> DeviceSettings:
    volume_limit = DEFAULT_DEVICE_SETTINGS["volume_limit"]
    screen_brightness = DEFAULT_DEVICE_SETTINGS["screen_brightness"]
    allowed_barge_in = DEFAULT_DEVICE_SETTINGS["allowed_barge_in"]
    if not isinstance(volume_limit, int) or isinstance(volume_limit, bool):
        raise TypeError("default volume_limit must be an integer")
    if not isinstance(screen_brightness, int) or isinstance(screen_brightness, bool):
        raise TypeError("default screen_brightness must be an integer")
    if not isinstance(allowed_barge_in, list):
        raise TypeError("default allowed_barge_in must be a list")
    return DeviceSettings(
        device_id=device_id,
        settings_version=0,
        volume_limit=volume_limit,
        screen_brightness=screen_brightness,
        night_mode=bool(DEFAULT_DEVICE_SETTINGS["night_mode"]),
        do_not_disturb=bool(DEFAULT_DEVICE_SETTINGS["do_not_disturb"]),
        learning_mode=str(DEFAULT_DEVICE_SETTINGS["learning_mode"]),
        audio_mode=str(DEFAULT_DEVICE_SETTINGS["audio_mode"]),
        wake_mode=str(DEFAULT_DEVICE_SETTINGS["wake_mode"]),
        wake_word_id=str(DEFAULT_DEVICE_SETTINGS["wake_word_id"]),
        allowed_barge_in=tuple(str(item) for item in allowed_barge_in),
        updated_by="",
        updated_at=now,
        update_reason="defaults",
    )


def _validate_settings_changes(changes: Mapping[str, object]) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for key, value in changes.items():
        if key in {"volume_limit", "screen_brightness"}:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            if not 0 <= int(value) <= 100:
                raise ValueError(f"{key} must be between 0 and 100")
            cleaned[key] = int(value)
        elif key in {"night_mode", "do_not_disturb"}:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
            cleaned[key] = value
        elif key == "audio_mode":
            if value not in AUDIO_MODES:
                raise ValueError(f"audio_mode must be one of {AUDIO_MODES}")
            cleaned[key] = value
        elif key == "wake_mode":
            if value not in WAKE_MODES:
                raise ValueError(f"wake_mode must be one of {WAKE_MODES}")
            cleaned[key] = value
        elif key == "wake_word_id":
            wake_word_id = str(value)
            if wake_word_id not in WAKE_WORD_IDS:
                raise ValueError(f"wake_word_id must be one of {sorted(WAKE_WORD_IDS)}")
            cleaned[key] = wake_word_id
        elif key == "learning_mode":
            if value not in LEARNING_MODES:
                raise ValueError(f"learning_mode must be one of {LEARNING_MODES}")
            cleaned[key] = value
        elif key == "allowed_barge_in":
            if not isinstance(value, (list, tuple)):
                raise ValueError("allowed_barge_in must be a list")
            kinds = tuple(str(item) for item in value)
            if not kinds or len(set(kinds)) != len(kinds):
                raise ValueError("allowed_barge_in must be a non-empty unique list")
            if any(kind not in BARGE_IN_KINDS for kind in kinds):
                raise ValueError(f"allowed_barge_in values must be one of {BARGE_IN_KINDS}")
            if "none" in kinds and kinds != ("none",):
                raise ValueError("allowed_barge_in none must be the only value")
            cleaned[key] = list(kinds)
        else:
            raise ValueError(f"unknown settings key {key}")
    return cleaned


class DeviceSettingsAuthority:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def current(self, device_id: str, *, now: datetime) -> DeviceSettings:
        row = self._store.get_device_settings(device_id=device_id)
        if row is None:
            return default_settings(device_id, now=now)
        return _settings_from_row(device_id, row)

    def update(
        self,
        *,
        device_id: str,
        actor_id: str,
        changes: Mapping[str, object],
        reason: str,
        now: datetime,
        acoustic_capability: DeviceAcousticCapability | None,
        expected_version: int | None = None,
    ) -> DeviceSettings:
        current = self.current(device_id, now=now)
        if expected_version is not None and expected_version != current.settings_version:
            raise DeviceSettingsConflictError("settings_version_conflict")
        cleaned = _validate_settings_changes(changes)
        if "audio_mode" in cleaned:
            if cleaned["audio_mode"] not in allowed_audio_modes(acoustic_capability):
                raise AudioModeGateError("audio_mode_requires_aec_evidence")
        merged: dict[str, object] = {
            "volume_limit": current.volume_limit,
            "screen_brightness": current.screen_brightness,
            "night_mode": current.night_mode,
            "do_not_disturb": current.do_not_disturb,
            "learning_mode": current.learning_mode,
            "audio_mode": current.audio_mode,
            "wake_mode": current.wake_mode,
            "wake_word_id": current.wake_word_id,
            "allowed_barge_in": list(current.allowed_barge_in),
            **cleaned,
        }
        settings_fingerprint = hashlib.sha256(canonical_payload(merged).encode("utf-8")).hexdigest()
        try:
            stored, _ledger = self._store.update_device_settings_and_profile_ledger(
                device_id=device_id,
                settings=merged,
                settings_version=current.settings_version + 1,
                expected_current_settings_version=current.settings_version,
                settings_fingerprint=settings_fingerprint,
                updated_by=actor_id,
                updated_at=_iso(now),
                update_reason=reason,
            )
        except ValueError as exc:
            if str(exc) == "settings_version_conflict":
                raise DeviceSettingsConflictError("settings_version_conflict") from exc
            raise
        return _settings_from_row(device_id, stored)


@dataclass(frozen=True, slots=True)
class DeviceAcousticCapability:
    device_id: str
    board_profile: str
    firmware_version_range: str
    acoustic_profile_version: int
    simultaneous_capture_playback: bool
    aec_reference_type: str
    aec_verified: bool
    max_barge_in_level: str
    tested_volume_range: str
    tested_distance_m: float | None
    test_report_uri: str
    approved_at: datetime
    approved_by: str
    revoked_at: datetime | None

    def is_active(self) -> bool:
        return self.revoked_at is None

    def to_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "board_profile": self.board_profile,
            "firmware_version_range": self.firmware_version_range,
            "acoustic_profile_version": self.acoustic_profile_version,
            "simultaneous_capture_playback": self.simultaneous_capture_playback,
            "aec_reference_type": self.aec_reference_type,
            "aec_verified": self.aec_verified,
            "max_barge_in_level": self.max_barge_in_level,
            "tested_volume_range": self.tested_volume_range,
            "tested_distance_m": self.tested_distance_m,
            "test_report_uri": self.test_report_uri,
            "approved_at": _iso(self.approved_at),
            "approved_by": self.approved_by,
            "revoked_at": _iso(self.revoked_at) if self.revoked_at is not None else None,
        }


def allowed_audio_modes(capability: DeviceAcousticCapability | None) -> frozenset[str]:
    # interrupt_assist is a safe requested ceiling: the Edge still intersects
    # it with the signed allowed_barge_in sources and the device's negotiated
    # physical/KWS/VAD capabilities. Full duplex additionally requires a
    # server-approved acoustic profile.
    if (
        capability is not None
        and capability.is_active()
        and capability.aec_verified
        and capability.simultaneous_capture_playback
        and capability.aec_reference_type not in {"", "none"}
    ):
        return frozenset(AUDIO_MODES)
    return frozenset({"half_duplex_safe", "interrupt_assist"})


class AcousticCapabilityAuthority:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def current(self, device_id: str) -> DeviceAcousticCapability | None:
        row = self._store.get_device_acoustic_capability(device_id=device_id)
        if row is None:
            return None
        return DeviceAcousticCapability(
            device_id=str(row["device_id"]),
            board_profile=str(row["board_profile"]),
            firmware_version_range=str(row["firmware_version_range"]),
            acoustic_profile_version=int(row["acoustic_profile_version"]),
            simultaneous_capture_playback=bool(row["simultaneous_capture_playback"]),
            aec_reference_type=str(row["aec_reference_type"]),
            aec_verified=bool(row["aec_verified"]),
            max_barge_in_level=str(row["max_barge_in_level"]),
            tested_volume_range=str(row["tested_volume_range"]),
            tested_distance_m=(
                float(row["tested_distance_m"]) if row["tested_distance_m"] is not None else None
            ),
            test_report_uri=str(row["test_report_uri"]),
            approved_at=_utc(str(row["approved_at"])),
            approved_by=str(row["approved_by"]),
            revoked_at=_utc(str(row["revoked_at"])) if row["revoked_at"] is not None else None,
        )

    def register(
        self,
        *,
        device_id: str,
        board_profile: str,
        firmware_version_range: str,
        acoustic_profile_version: int,
        simultaneous_capture_playback: bool,
        aec_reference_type: str,
        aec_verified: bool,
        max_barge_in_level: str,
        tested_volume_range: str,
        tested_distance_m: float | None,
        test_report_uri: str,
        approved_by: str,
        now: datetime,
    ) -> DeviceAcousticCapability:
        stored = self._store.upsert_device_acoustic_capability(
            device_id=device_id,
            board_profile=board_profile,
            firmware_version_range=firmware_version_range,
            acoustic_profile_version=acoustic_profile_version,
            simultaneous_capture_playback=simultaneous_capture_playback,
            aec_reference_type=aec_reference_type,
            aec_verified=aec_verified,
            max_barge_in_level=max_barge_in_level,
            tested_volume_range=tested_volume_range,
            tested_distance_m=tested_distance_m,
            test_report_uri=test_report_uri,
            approved_at=_iso(now),
            approved_by=approved_by,
        )
        return DeviceAcousticCapability(
            device_id=str(stored["device_id"]),
            board_profile=str(stored["board_profile"]),
            firmware_version_range=str(stored["firmware_version_range"]),
            acoustic_profile_version=int(stored["acoustic_profile_version"]),
            simultaneous_capture_playback=bool(stored["simultaneous_capture_playback"]),
            aec_reference_type=str(stored["aec_reference_type"]),
            aec_verified=bool(stored["aec_verified"]),
            max_barge_in_level=str(stored["max_barge_in_level"]),
            tested_volume_range=str(stored["tested_volume_range"]),
            tested_distance_m=(
                float(stored["tested_distance_m"])
                if stored["tested_distance_m"] is not None
                else None
            ),
            test_report_uri=str(stored["test_report_uri"]),
            approved_at=_utc(str(stored["approved_at"])),
            approved_by=str(stored["approved_by"]),
            revoked_at=None,
        )

    def revoke(self, *, device_id: str, now: datetime) -> bool:
        return self._store.revoke_device_acoustic_capability(
            device_id=device_id, revoked_at=_iso(now)
        )


@dataclass(frozen=True, slots=True)
class DeviceControlIntent:
    intent_id: str
    device_id: str
    intent_type: str
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "device_id": self.device_id,
            "intent_type": self.intent_type,
            "requested_by": self.requested_by,
            "requested_at": _iso(self.requested_at),
            "expires_at": _iso(self.expires_at),
            "status": self.status,
        }


def _intent_from_row(row: Mapping[str, Any]) -> DeviceControlIntent:
    return DeviceControlIntent(
        intent_id=str(row["intent_id"]),
        device_id=str(row["device_id"]),
        intent_type=str(row["intent_type"]),
        requested_by=str(row["requested_by"]),
        requested_at=_utc(str(row["requested_at"])),
        expires_at=_utc(str(row["expires_at"])),
        status=str(row["status"]),
    )


class DeviceControlIntentAuthority:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def create(
        self,
        *,
        device_id: str,
        intent_type: str,
        requested_by: str,
        idempotency_key: str | None,
        now: datetime,
        ttl: timedelta = timedelta(minutes=10),
    ) -> DeviceControlIntent:
        if ttl <= timedelta(0):
            raise ValueError("intent ttl must be positive")
        digest_source = (
            f"{device_id}|{intent_type}|{idempotency_key}" if idempotency_key else uuid.uuid4().hex
        )
        intent_id = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:32]
        row = self._store.create_device_control_intent(
            intent_id=intent_id,
            device_id=device_id,
            intent_type=intent_type,
            requested_by=requested_by,
            requested_at=_iso(now),
            expires_at=_iso(now + ttl),
        )
        return _intent_from_row(row)

    def list_pending(self, *, device_id: str, now: datetime) -> tuple[DeviceControlIntent, ...]:
        intents = [
            _intent_from_row(row)
            for row in self._store.list_device_control_intents(device_id=device_id)
        ]
        return tuple(
            intent for intent in intents if intent.status == "pending" and intent.expires_at > now
        )


__all__ = [
    "AUDIO_MODES",
    "AcousticCapabilityAuthority",
    "AudioModeGateError",
    "BARGE_IN_KINDS",
    "DeviceAcousticCapability",
    "DeviceControlError",
    "DeviceControlIntent",
    "DeviceControlIntentAuthority",
    "DeviceSettings",
    "DeviceSettingsAuthority",
    "DeviceSettingsConflictError",
    "LEARNING_MODES",
    "ProfileAckConflictError",
    "RuntimeProfileLedger",
    "RuntimeProfileLedgerEntry",
    "WAKE_MODES",
    "allowed_audio_modes",
    "stable_profile_fingerprint",
]
