"""SQLite media-challenge and claim-reservation mixin for the bootstrap store.

``SQLiteBootstrapStoreClaimsMixin`` is composed into ``SQLiteBootstrapStore``;
the connection lock, transaction/read helpers, row decoders and event writer
stay owned by the store and are only declared here for type checking.  The
module owns device media challenges, online-proof acceptance and claim
reservation/expiry.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Final

from services.device_fleet.bootstrap_domain import (
    ActorMismatch,
    BootstrapPurpose,
    BootstrapSession,
    BootstrapState,
    ChallengeReplay,
    ClaimConflict,
    ClaimNotFound,
    ClaimReservation,
    ClaimStatus,
    DeviceAlreadyBound,
    DeviceChallenge,
    DeviceLifecycle,
    DeviceMediaChallenge,
    DeviceMediaChallengeExpired,
    DeviceMediaChallengeRateLimited,
    DeviceNotFound,
    DeviceRecord,
    DeviceRevoked,
    InvalidOnboardingRequest,
    SessionExpired,
    SessionNotFound,
    StateVersionConflict,
    now_utc,
    require_bootstrap_device,
    require_transition,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import contextmanager


_DEVICE_ACTIVE_CLAIMS: Final[tuple[str, ...]] = (
    ClaimStatus.RESERVED.value,
    ClaimStatus.BINDING_COMMITTING.value,
)


def _timestamp(value: datetime) -> str:
    return now_utc(value).isoformat().replace("+00:00", "Z")


def _in_clause(values: tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


class SQLiteBootstrapStoreClaimsMixin:
    """Issue media challenges, accept online proof and reserve claims."""

    if TYPE_CHECKING:
        # Shared members owned by SQLiteBootstrapStore.
        @contextmanager
        def _write(self) -> Iterator[sqlite3.Connection]: ...

        @contextmanager
        def _read(self) -> Iterator[sqlite3.Connection]: ...

        def _event(
            self,
            connection: sqlite3.Connection,
            *,
            session: BootstrapSession,
            event_type: str,
            previous_state: BootstrapState | None,
            next_state: BootstrapState | None,
            actor_type: str,
            actor_id: str | None,
            reason_code: str | None,
        ) -> None: ...

        @staticmethod
        def _device(row: sqlite3.Row) -> DeviceRecord: ...

        @staticmethod
        def _session(row: sqlite3.Row) -> BootstrapSession: ...

        @staticmethod
        def _challenge(row: sqlite3.Row) -> DeviceChallenge: ...

        @staticmethod
        def _media_challenge(row: sqlite3.Row) -> DeviceMediaChallenge: ...

        @staticmethod
        def _claim(row: sqlite3.Row) -> ClaimReservation: ...

    def issue_media_challenge(
        self,
        challenge: DeviceMediaChallenge,
        *,
        max_outstanding: int = 3,
    ) -> DeviceMediaChallenge:
        current_time = now_utc(challenge.issued_at)
        with self._write() as connection:
            connection.execute(
                """
                DELETE FROM device_media_challenges
                WHERE device_id = ? AND (expires_at <= ? OR used_at IS NOT NULL)
                """,
                (challenge.device_id, _timestamp(current_time)),
            )
            outstanding = connection.execute(
                """
                SELECT COUNT(*) AS count FROM device_media_challenges
                WHERE device_id = ? AND used_at IS NULL AND expires_at > ?
                """,
                (challenge.device_id, _timestamp(current_time)),
            ).fetchone()
            if outstanding is not None and int(outstanding["count"]) >= max_outstanding:
                raise DeviceMediaChallengeRateLimited()
            connection.execute(
                """
                INSERT INTO device_media_challenges (
                    challenge_id, device_id, certificate_id, client_id, nonce_hash,
                    issued_at, expires_at, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    challenge.challenge_id,
                    challenge.device_id,
                    challenge.certificate_id,
                    challenge.client_id,
                    challenge.nonce_hash,
                    _timestamp(challenge.issued_at),
                    _timestamp(challenge.expires_at),
                ),
            )
        return challenge

    def get_media_challenge(self, challenge_id: str) -> DeviceMediaChallenge | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM device_media_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
        return self._media_challenge(row) if row is not None else None

    def consume_media_challenge(
        self,
        *,
        challenge_id: str,
        device_id: str,
        nonce_hash: str,
        now: datetime,
    ) -> DeviceMediaChallenge:
        current_time = now_utc(now)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM device_media_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if row is None:
                raise ChallengeReplay("device media challenge was not found")
            challenge = self._media_challenge(row)
            if challenge.device_id != device_id or challenge.nonce_hash != nonce_hash:
                raise InvalidOnboardingRequest("device media challenge does not match")
            if challenge.used_at is not None:
                raise ChallengeReplay("device media challenge was already consumed")
            if challenge.expires_at <= current_time:
                raise DeviceMediaChallengeExpired()
            updated = connection.execute(
                """
                UPDATE device_media_challenges SET used_at = ?
                WHERE challenge_id = ? AND used_at IS NULL AND expires_at > ?
                """,
                (
                    _timestamp(current_time),
                    challenge_id,
                    _timestamp(current_time),
                ),
            )
            if updated.rowcount != 1:
                raise ChallengeReplay("device media challenge was already consumed")
            accepted = connection.execute(
                "SELECT * FROM device_media_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if accepted is None:
                raise ChallengeReplay("device media challenge was not found")
        return self._media_challenge(accepted)

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
    ) -> BootstrapSession:
        current_time = now_utc(now)
        with self._write() as connection:
            challenge_row = connection.execute(
                "SELECT * FROM device_onboarding_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if challenge_row is None:
                raise ChallengeReplay("device challenge not found")
            challenge = self._challenge(challenge_row)
            if challenge.onboarding_session_id != onboarding_session_id:
                raise ChallengeReplay("device challenge does not belong to session")
            if challenge.device_id != device_id:
                raise ChallengeReplay("device challenge does not belong to device")
            if challenge.used_at is not None:
                raise ChallengeReplay("device challenge was already consumed")
            if challenge.expires_at <= current_time:
                raise SessionExpired("device challenge expired")
            session_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (onboarding_session_id,),
            ).fetchone()
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            if session.expires_at <= current_time:
                raise SessionExpired()
            device_row = connection.execute(
                "SELECT * FROM device_onboarding_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if device_row is None:
                raise DeviceNotFound()
            device = self._device(device_row)
            lifecycle = require_bootstrap_device(device, session)
            reprovision = session.purpose is BootstrapPurpose.REPROVISION
            if monotonic_counter <= device.last_monotonic_counter:
                raise ClaimConflict("device monotonic counter is not increasing")
            timestamp = _timestamp(current_time)
            connection.execute(
                "UPDATE device_onboarding_challenges SET used_at = ? WHERE challenge_id = ?",
                (timestamp, challenge_id),
            )
            connection.execute(
                """
                UPDATE device_onboarding_devices
                SET lifecycle_status = ?, last_monotonic_counter = ?,
                    firmware_version = ?, firmware_security_version = ?, updated_at = ?
                WHERE device_id = ?
                """,
                (
                    lifecycle.value,
                    monotonic_counter,
                    firmware_version,
                    firmware_security_version,
                    timestamp,
                    device_id,
                ),
            )
            target = BootstrapState.DEVICE_ONLINE
            require_transition(session.state, target)
            connection.execute(
                """
                UPDATE device_onboarding_sessions
                SET state = ?, state_version = state_version + 1,
                    proximity_verified_at = COALESCE(proximity_verified_at, ?),
                    wifi_connected_at = COALESCE(wifi_connected_at, ?),
                    device_online_at = ?,
                    consumed_at = COALESCE(consumed_at, ?)
                WHERE onboarding_session_id = ? AND state_version = ?
                """,
                (
                    target.value,
                    timestamp, timestamp, timestamp,
                    timestamp if reprovision else None,
                    onboarding_session_id,
                    session.state_version,
                ),
            )
            updated_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (onboarding_session_id,),
            ).fetchone()
            if updated_row is None:
                raise SessionNotFound()
            updated = self._session(updated_row)
            self._event(
                connection,
                session=updated,
                event_type=f"{'reprovision' if reprovision else 'device'}_online_proof_accepted",
                previous_state=session.state,
                next_state=target,
                actor_type="device",
                actor_id=device_id,
                reason_code=None,
            )
        return updated

    # ------------------------------------------------------------------
    # Claim reservation
    # ------------------------------------------------------------------

    def reserve_claim(
        self,
        claim: ClaimReservation,
        *,
        expected_state_version: int,
        now: datetime,
    ) -> ClaimReservation:
        current_time = now_utc(now)
        with self._write() as connection:
            session_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (claim.onboarding_session_id,),
            ).fetchone()
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            if session.state_version != expected_state_version:
                raise StateVersionConflict("onboarding state version changed")
            if session.device_id != claim.device_id or session.actor_id != claim.actor_id:
                raise ActorMismatch()
            if session.purpose is BootstrapPurpose.REPROVISION:
                raise ClaimConflict("reprovision session cannot reserve a claim")
            if session.expires_at <= current_time:
                raise SessionExpired()
            if not session.proximity_verified_at or not session.device_online_at:
                raise ClaimConflict("device online proof and proximity are required")
            existing_session_row = connection.execute(
                """
                SELECT * FROM device_onboarding_claims
                WHERE onboarding_session_id = ?
                """,
                (claim.onboarding_session_id,),
            ).fetchone()
            if existing_session_row is not None:
                existing = self._claim(existing_session_row)
                if existing.actor_id != claim.actor_id:
                    raise ClaimConflict("onboarding session belongs to another actor")
                if existing.idempotency_key == claim.idempotency_key:
                    return existing
                if existing.status is ClaimStatus.COMMITTED:
                    return existing
                raise ClaimConflict("claim idempotency key conflicts")
            active_rows = connection.execute(
                f"""
                SELECT * FROM device_onboarding_claims
                WHERE device_id = ? AND status IN ({_in_clause(_DEVICE_ACTIVE_CLAIMS)})
                  AND expires_at > ?
                ORDER BY reserved_at DESC
                """,
                (claim.device_id, *_DEVICE_ACTIVE_CLAIMS, _timestamp(current_time)),
            ).fetchall()
            if active_rows:
                active = self._claim(active_rows[0])
                if active.actor_id != claim.actor_id:
                    raise ClaimConflict("device is being claimed by another actor")
                raise ClaimConflict("device already has an active claim")
            device_row = connection.execute(
                "SELECT * FROM device_onboarding_devices WHERE device_id = ?",
                (claim.device_id,),
            ).fetchone()
            if device_row is None:
                raise DeviceNotFound()
            device = self._device(device_row)
            if device.lifecycle_status is DeviceLifecycle.REVOKED:
                raise DeviceRevoked()
            if device.lifecycle_status is DeviceLifecycle.BOUND:
                raise DeviceAlreadyBound()
            try:
                connection.execute(
                    """
                    INSERT INTO device_onboarding_claims (
                        claim_id, onboarding_session_id, device_id, actor_id, status,
                        idempotency_key, reserved_at, expires_at, binding_id,
                        binding_version, committed_at, released_at, failure_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)
                    """,
                    (
                        claim.claim_id,
                        claim.onboarding_session_id,
                        claim.device_id,
                        claim.actor_id,
                        claim.status.value,
                        claim.idempotency_key,
                        _timestamp(claim.reserved_at),
                        _timestamp(claim.expires_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ClaimConflict("device claim already exists") from exc
            require_transition(session.state, BootstrapState.CLAIM_RESERVED)
            connection.execute(
                """
                UPDATE device_onboarding_sessions
                SET state = ?, state_version = state_version + 1
                WHERE onboarding_session_id = ? AND state_version = ?
                """,
                (
                    BootstrapState.CLAIM_RESERVED.value,
                    claim.onboarding_session_id,
                    expected_state_version,
                ),
            )
            updated_session_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (claim.onboarding_session_id,),
            ).fetchone()
            if updated_session_row is None:
                raise SessionNotFound()
            self._event(
                connection,
                session=self._session(updated_session_row),
                event_type="claim_reserved",
                previous_state=session.state,
                next_state=BootstrapState.CLAIM_RESERVED,
                actor_type="user",
                actor_id=claim.actor_id,
                reason_code=None,
            )
        return claim

    def get_claim(self, claim_id: str) -> ClaimReservation | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
        return self._claim(row) if row is not None else None

    def get_claim_for_session(self, onboarding_session_id: str) -> ClaimReservation | None:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT * FROM device_onboarding_claims
                WHERE onboarding_session_id = ?
                """,
                (onboarding_session_id,),
            ).fetchone()
        return self._claim(row) if row is not None else None

    def expire_claim_if_needed(self, claim_id: str, *, now: datetime) -> ClaimReservation:
        current_time = now_utc(now)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if row is None:
                raise ClaimNotFound()
            claim = self._claim(row)
            if claim.status in {ClaimStatus.COMMITTED, ClaimStatus.RELEASED, ClaimStatus.EXPIRED}:
                return claim
            if claim.expires_at > current_time:
                return claim
            connection.execute(
                """
                UPDATE device_onboarding_claims
                SET status = ?, released_at = ?, failure_code = ?
                WHERE claim_id = ? AND status IN (?, ?)
                """,
                (
                    ClaimStatus.EXPIRED.value,
                    _timestamp(current_time),
                    "CLAIM_EXPIRED",
                    claim_id,
                    ClaimStatus.RESERVED.value,
                    ClaimStatus.BINDING_COMMITTING.value,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if updated is None:
                raise ClaimNotFound()
        return self._claim(updated)
