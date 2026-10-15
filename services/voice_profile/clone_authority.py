from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, assert_never, cast, final

import asyncpg

from services.voice_profile.domain import VoiceConsent, VoiceConsentRequiredError

_CURRENT_NOTICE: Final = "voice-clone-separate-notice-v1"
_CLONE_VALID_NOTICES: Final[frozenset[str]] = frozenset({_CURRENT_NOTICE})
_SEAL: Final = object()

ConsentStanding = Literal["valid", "absent", "revoked", "notice_required"]


class VoiceCloneDisabledError(PermissionError):
    code: Final = "voice_clone_disabled"

    def __init__(self) -> None:
        super().__init__("voice cloning is disabled")


@final
class CloneAdmission:
    __slots__ = ("notice_version",)

    def __init__(self, seal: object, notice_version: str) -> None:
        if seal is not _SEAL:
            raise TypeError("CloneAdmission can only be issued by VoiceCloneAuthority")
        self.notice_version = notice_version


@final
@dataclass(frozen=True, slots=True)
class VoiceCloneAuthority:
    enabled: bool

    def admit(self) -> CloneAdmission:
        if not self.enabled:
            raise VoiceCloneDisabledError
        return CloneAdmission(_SEAL, _CURRENT_NOTICE)


def consent_standing(consent: VoiceConsent | None) -> ConsentStanding:
    if consent is None:
        return "absent"
    if consent.revoked_at is not None:
        return "revoked"
    if consent.notice_version not in _CLONE_VALID_NOTICES:
        return "notice_required"
    return "valid"


def _refusal(standing: ConsentStanding) -> str:
    if standing == "absent":
        return "active voice-clone consent is required"
    if standing == "revoked":
        return "voice-clone consent was revoked"
    if standing == "notice_required":
        return "re-consent under the separate notice"
    if standing == "valid":
        raise RuntimeError("a valid consent is not a refusal")
    assert_never(standing)


def _consent_from_row(row: asyncpg.Record) -> VoiceConsent:
    return VoiceConsent(
        account_id=str(row["account_id"]),
        policy_version=str(row["policy_version"]),
        notice_version=str(row["notice_version"]),
        granted_at=cast(datetime, row["granted_at"]),
        revoked_at=cast("datetime | None", row["revoked_at"]),
    )


async def load_consent(
    connection: asyncpg.Connection,
    account_id: str,
    *,
    for_update: bool = False,
) -> VoiceConsent | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = await connection.fetchrow(
        "SELECT * FROM voice_clone_consents WHERE account_id = $1" + suffix,
        account_id,
    )
    if row is None:
        return None
    return _consent_from_row(row)


async def require_clone_consent(
    connection: asyncpg.Connection,
    account_id: str,
    *,
    for_update: bool = False,
) -> VoiceConsent:
    consent = await load_consent(connection, account_id, for_update=for_update)
    standing = consent_standing(consent)
    if standing != "valid":
        raise VoiceConsentRequiredError(_refusal(standing))
    assert consent is not None
    return consent


async def clone_consent_valid(
    connection: asyncpg.Connection,
    account_id: str,
    *,
    for_update: bool = False,
) -> bool:
    consent = await load_consent(connection, account_id, for_update=for_update)
    return consent_standing(consent) == "valid"


async def record_grant(
    connection: asyncpg.Connection,
    admission: CloneAdmission,
    *,
    account_id: str,
    policy_version: str,
    event_id: str,
    granted_at: datetime,
) -> VoiceConsent:
    row = await connection.fetchrow(
        """
        INSERT INTO voice_clone_consents (
            account_id, policy_version, granted_at, grant_event_id, notice_version
        ) VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT(account_id) DO UPDATE SET
            policy_version = excluded.policy_version,
            granted_at = excluded.granted_at,
            revoked_at = NULL,
            grant_event_id = excluded.grant_event_id,
            revoke_event_id = NULL,
            notice_version = excluded.notice_version
        RETURNING *
        """,
        account_id,
        policy_version,
        granted_at,
        event_id,
        admission.notice_version,
    )
    assert row is not None
    return _consent_from_row(row)


async def record_revocation(
    connection: asyncpg.Connection,
    *,
    account_id: str,
    event_id: str,
    revoked_at: datetime,
) -> None:
    await connection.execute(
        """
        UPDATE voice_clone_consents
        SET revoked_at = $1, revoke_event_id = $2
        WHERE account_id = $3
        """,
        revoked_at,
        event_id,
        account_id,
    )
