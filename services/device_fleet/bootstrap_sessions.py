"""PostgreSQL bootstrap session and challenge mixin.

``PostgresBootstrapSessionMixin`` is composed into ``PostgresBootstrapStore``;
the sync/transaction facade, row decoders, scope builder and the state-versioned
event writer stay owned by the store and are only declared here for type
checking.  The module owns onboarding sessions and the challenge/proof
transactions that advance them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, TypeVar

import asyncpg

from services.device_fleet.bootstrap_domain import (
    BootstrapSession,
    BootstrapState,
    ChallengeReplay,
    ClaimConflict,
    DeviceChallenge,
    DeviceMediaChallenge,
    DeviceMediaChallengeExpired,
    DeviceMediaChallengeRateLimited,
    DeviceNotFound,
    InvalidOnboardingRequest,
    SessionNotFound,
    StateVersionConflict,
    require_transition,
)
from services.device_fleet.bootstrap_rows import _timestamp

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from services.device_fleet.bootstrap_sync import _Scope

_T = TypeVar("_T")


class PostgresBootstrapSessionMixin:
    """Create onboarding sessions and run their challenge transactions."""

    if TYPE_CHECKING:
        # Shared members owned by PostgresBootstrapStore and its base mixins.
        def _call(self, awaitable: Awaitable[_T]) -> _T: ...

        def _scope(
            self,
            *,
            actor_id: str | None = None,
            device_id: str | None = None,
            lookup_kind: str | None = None,
            lookup_id: str | None = None,
        ) -> _Scope: ...

        async def _transaction(
            self,
            scope: _Scope,
            operation: Callable[[asyncpg.Connection], Awaitable[_T]],
        ) -> _T: ...

        async def _authorize_locked_device_mutation(
            self,
            connection: asyncpg.Connection,
            *,
            device_id: str,
            lookup_kind: str,
            lookup_id: str,
        ) -> None: ...

        async def _event(
            self,
            connection: asyncpg.Connection,
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
        def _session(row: asyncpg.Record) -> BootstrapSession: ...

        @staticmethod
        def _challenge(row: asyncpg.Record) -> DeviceChallenge: ...

        @staticmethod
        def _media_challenge(row: asyncpg.Record) -> DeviceMediaChallenge: ...

    # ------------------------------------------------------------------
    # Bootstrap sessions and state-versioned events
    # ------------------------------------------------------------------

    def create_session(self, session: BootstrapSession) -> BootstrapSession:
        scope = self._scope(actor_id=session.actor_id, device_id=session.device_id)

        async def operation(connection: asyncpg.Connection) -> None:
            try:
                await connection.execute(
                    """
                    INSERT INTO device_onboarding_sessions (
                        onboarding_session_id, device_id, actor_id, client_onboarding_id,
                        qr_nonce_hash, pop_hash, mobile_nonce_hash, protocol_version,
                        ble_name, ble_service_uuid, state, state_version, first_seen_at,
                        expires_at, proximity_verified_at, wifi_connected_at,
                        device_online_at, cancelled_at, consumed_at, failure_code, purpose
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21
                    )
                    """,
                    session.onboarding_session_id,
                    session.device_id,
                    session.actor_id,
                    session.client_onboarding_id,
                    session.qr_nonce_hash,
                    session.pop_hash,
                    session.mobile_nonce_hash,
                    session.protocol_version,
                    session.ble_name,
                    session.ble_service_uuid,
                    session.state.value,
                    session.state_version,
                    _timestamp(session.first_seen_at),
                    _timestamp(session.expires_at),
                    _timestamp(session.proximity_verified_at)
                    if session.proximity_verified_at
                    else None,
                    _timestamp(session.wifi_connected_at) if session.wifi_connected_at else None,
                    _timestamp(session.device_online_at) if session.device_online_at else None,
                    _timestamp(session.cancelled_at) if session.cancelled_at else None,
                    _timestamp(session.consumed_at) if session.consumed_at else None,
                    session.failure_code,
                    session.purpose.value,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ClaimConflict("onboarding session identity already exists") from exc
            await self._event(
                connection,
                session=session,
                event_type="session_created",
                previous_state=None,
                next_state=session.state,
                actor_type="user",
                actor_id=session.actor_id,
                reason_code=None,
            )

        self._call(self._transaction(scope, operation))
        return session

    def find_session_by_client(
        self, *, actor_id: str, client_onboarding_id: str
    ) -> BootstrapSession | None:
        scope = self._scope(actor_id=actor_id)

        async def operation(connection: asyncpg.Connection) -> BootstrapSession | None:
            row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_sessions
                WHERE actor_id = $1 AND client_onboarding_id = $2
                """,
                actor_id,
                client_onboarding_id,
            )
            return self._session(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

    def find_session_by_qr(
        self, *, device_id: str, qr_nonce_hash: str
    ) -> BootstrapSession | None:
        scope = self._scope(device_id=device_id)

        async def operation(connection: asyncpg.Connection) -> BootstrapSession | None:
            row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_sessions
                WHERE device_id = $1 AND qr_nonce_hash = $2
                """,
                device_id,
                qr_nonce_hash,
            )
            return self._session(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

    def get_session(self, onboarding_session_id: str) -> BootstrapSession | None:
        scope = self._scope(
            lookup_kind="session", lookup_id=onboarding_session_id
        )

        async def operation(connection: asyncpg.Connection) -> BootstrapSession | None:
            row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = $1",
                onboarding_session_id,
            )
            return self._session(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

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
    ) -> BootstrapSession:
        allowed_fields = {
            "proximity_verified_at",
            "wifi_connected_at",
            "device_online_at",
            "cancelled_at",
            "consumed_at",
            "failure_code",
        }
        updates = dict(fields or {})
        if not set(updates).issubset(allowed_fields):
            raise InvalidOnboardingRequest("unsupported session update field")
        scope = self._scope(
            actor_id=actor_id if actor_type == "user" else None,
            device_id=actor_id if actor_type == "device" else None,
            lookup_kind="session",
            lookup_id=onboarding_session_id,
        )

        async def operation(connection: asyncpg.Connection) -> BootstrapSession:
            row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_sessions
                WHERE onboarding_session_id = $1 FOR UPDATE
                """,
                onboarding_session_id,
            )
            if row is None:
                raise SessionNotFound()
            current = self._session(row)
            if current.state_version != expected_state_version:
                raise StateVersionConflict("onboarding state version changed")
            require_transition(current.state, target)
            if actor_type == "system":
                await self._authorize_locked_device_mutation(
                    connection,
                    device_id=current.device_id,
                    lookup_kind="session",
                    lookup_id=onboarding_session_id,
                )
            assignments = ["state = $1", "state_version = state_version + 1"]
            values: list[object] = [target.value]
            parameter = 2
            for field, value in updates.items():
                assignments.append(f"{field} = ${parameter}")
                values.append(_timestamp(value) if isinstance(value, datetime) else value)
                parameter += 1
            values.extend([onboarding_session_id, expected_state_version])
            await connection.execute(
                f"UPDATE device_onboarding_sessions SET {', '.join(assignments)} "
                f"WHERE onboarding_session_id = ${parameter} AND state_version = ${parameter + 1}",
                *values,
            )
            updated_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = $1",
                onboarding_session_id,
            )
            if updated_row is None:
                raise SessionNotFound()
            updated = self._session(updated_row)
            await self._event(
                connection,
                session=updated,
                event_type=event_type,
                previous_state=current.state,
                next_state=target,
                actor_type=actor_type,
                actor_id=actor_id,
                reason_code=reason_code,
            )
            return updated

        return self._call(self._transaction(scope, operation))

    def expire_session(self, onboarding_session_id: str, *, now: datetime) -> BootstrapSession:
        session = self.get_session(onboarding_session_id)
        if session is None:
            raise SessionNotFound()
        if session.state in {
            BootstrapState.EXPIRED,
            BootstrapState.CANCELLED,
            BootstrapState.ACTIVATED,
        }:
            return session
        return self.transition_session(
            onboarding_session_id,
            expected_state_version=session.state_version,
            target=BootstrapState.EXPIRED,
            actor_type="system",
            actor_id=None,
            event_type="session_expired",
            reason_code="ttl",
            fields={"failure_code": "QR_SESSION_EXPIRED"},
        )

    # ------------------------------------------------------------------
    # Challenge and device proof transaction
    # ------------------------------------------------------------------

    def issue_challenge(self, challenge: DeviceChallenge) -> DeviceChallenge:
        scope = self._scope(device_id=challenge.device_id)

        async def operation(connection: asyncpg.Connection) -> None:
            try:
                await connection.execute(
                    """
                    INSERT INTO device_onboarding_challenges (
                        challenge_id, onboarding_session_id, device_id, nonce_hash,
                        issued_at, expires_at, used_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, NULL)
                    """,
                    challenge.challenge_id,
                    challenge.onboarding_session_id,
                    challenge.device_id,
                    challenge.nonce_hash,
                    _timestamp(challenge.issued_at),
                    _timestamp(challenge.expires_at),
                )
            except asyncpg.UniqueViolationError as exc:
                raise ChallengeReplay("device challenge already exists") from exc

        self._call(self._transaction(scope, operation))
        return challenge

    def get_challenge(self, challenge_id: str) -> DeviceChallenge | None:
        scope = self._scope(lookup_kind="challenge", lookup_id=challenge_id)

        async def operation(connection: asyncpg.Connection) -> DeviceChallenge | None:
            row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_challenges WHERE challenge_id = $1",
                challenge_id,
            )
            return self._challenge(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

    def issue_media_challenge(
        self,
        challenge: DeviceMediaChallenge,
        *,
        max_outstanding: int = 3,
    ) -> DeviceMediaChallenge:
        if max_outstanding < 1:
            raise ValueError("max_outstanding must be positive")
        scope = self._scope(device_id=challenge.device_id)
        current_time = _timestamp(challenge.issued_at)

        async def operation(connection: asyncpg.Connection) -> None:
            device = await connection.fetchval(
                "SELECT 1 FROM device_onboarding_devices WHERE device_id = $1 FOR UPDATE",
                challenge.device_id,
            )
            if device is None:
                raise DeviceNotFound()
            outstanding = await connection.fetchval(
                """
                SELECT COUNT(*) FROM device_media_challenges
                WHERE device_id = $1 AND used_at IS NULL AND expires_at > $2
                """,
                challenge.device_id,
                current_time,
            )
            if outstanding is not None and int(outstanding) >= max_outstanding:
                raise DeviceMediaChallengeRateLimited()
            try:
                await connection.execute(
                    """
                    INSERT INTO device_media_challenges (
                        challenge_id, device_id, certificate_id, client_id, nonce_hash,
                        issued_at, expires_at, used_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, NULL)
                    """,
                    challenge.challenge_id,
                    challenge.device_id,
                    challenge.certificate_id,
                    challenge.client_id,
                    challenge.nonce_hash,
                    _timestamp(challenge.issued_at),
                    _timestamp(challenge.expires_at),
                )
            except asyncpg.UniqueViolationError as exc:
                raise ChallengeReplay("device media challenge already exists") from exc

        self._call(self._transaction(scope, operation))
        return challenge

    def get_media_challenge(self, challenge_id: str) -> DeviceMediaChallenge | None:
        scope = self._scope(
            lookup_kind="media_challenge", lookup_id=challenge_id
        )

        async def operation(connection: asyncpg.Connection) -> DeviceMediaChallenge | None:
            row = await connection.fetchrow(
                "SELECT * FROM device_media_challenges WHERE challenge_id = $1",
                challenge_id,
            )
            return self._media_challenge(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

    def consume_media_challenge(
        self,
        *,
        challenge_id: str,
        device_id: str,
        nonce_hash: str,
        now: datetime,
    ) -> DeviceMediaChallenge:
        scope = self._scope(device_id=device_id)
        current_time = _timestamp(now)

        async def operation(connection: asyncpg.Connection) -> DeviceMediaChallenge:
            row = await connection.fetchrow(
                """
                SELECT * FROM device_media_challenges
                WHERE challenge_id = $1 FOR UPDATE
                """,
                challenge_id,
            )
            if row is None:
                raise ChallengeReplay("device media challenge was not found")
            challenge = self._media_challenge(row)
            if challenge.device_id != device_id or challenge.nonce_hash != nonce_hash:
                raise InvalidOnboardingRequest("device media challenge does not match")
            if challenge.used_at is not None:
                raise ChallengeReplay("device media challenge was already consumed")
            if challenge.expires_at <= current_time:
                raise DeviceMediaChallengeExpired()
            updated = await connection.execute(
                """
                UPDATE device_media_challenges SET used_at = $1
                WHERE challenge_id = $2 AND used_at IS NULL AND expires_at > $3
                """,
                current_time,
                challenge_id,
                current_time,
            )
            if updated != "UPDATE 1":
                raise ChallengeReplay("device media challenge was already consumed")
            accepted = await connection.fetchrow(
                "SELECT * FROM device_media_challenges WHERE challenge_id = $1",
                challenge_id,
            )
            if accepted is None:
                raise ChallengeReplay("device media challenge was not found")
            return self._media_challenge(accepted)

        return self._call(self._transaction(scope, operation))
