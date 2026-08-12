"""Synchronous Device Onboarding store backed by PostgreSQL/asyncpg.

``DeviceOnboardingService`` intentionally has a synchronous API because its
Control API routes are synchronous request handlers.  This adapter keeps that
public contract while owning an asyncpg pool on a private event-loop thread.
Every public operation executes in one database transaction and installs a
transaction-local actor/device/lookup scope before touching a FORCE-RLS table.
SQLite remains the explicit development implementation.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar, cast

import asyncpg

from services.device_fleet.bootstrap_domain import (
    ActivationNotFound,
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
    ClaimExpired,
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
    b64url_decode,
    b64url_encode,
    now_utc,
    require_transition,
    validate_activation_manifest,
)
from services.device_fleet.bootstrap_store import (
    _DEVICE_ACTIVE_CLAIMS,
    _TERMINAL_SESSION_STATES,
    BootstrapStorePort,
)

BootstrapPostgresRole = Literal["api", "maintenance"]
_ROLE_NAMES: dict[BootstrapPostgresRole, str] = {
    "api": "memoria_device_onboarding_api",
    "maintenance": "memoria_device_onboarding_maintenance",
}
_SCHEMA_PATH = Path(__file__).with_name("bootstrap_postgres_schema.sql")
_T = TypeVar("_T")


class PostgresBootstrapStoreContextError(RuntimeError):
    """The adapter could not establish an authenticated operation scope."""


@dataclass(frozen=True, slots=True)
class _Scope:
    actor_id: str | None = None
    device_id: str | None = None
    lookup_kind: str | None = None
    lookup_id: str | None = None


_SCOPE: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "device_onboarding_postgres_scope", default=None
)


class _AsyncLoopRunner:
    """Run asyncpg calls without changing the synchronous service contract."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="device-onboarding-postgres",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("device onboarding PostgreSQL event loop did not start")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    def call(self, awaitable: Awaitable[_T]) -> _T:
        if self._closed:
            raise RuntimeError("device onboarding PostgreSQL store is closed")
        async def await_any() -> _T:
            return await awaitable

        future: concurrent.futures.Future[_T] = asyncio.run_coroutine_threadsafe(
            await_any(), self._loop
        )
        return future.result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)


def _timestamp(value: datetime) -> datetime:
    normalized = now_utc(value)
    return normalized


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: object) -> dict[str, object]:
    parsed: object
    if isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    if not isinstance(parsed, Mapping):
        raise RuntimeError("corrupt onboarding JSON object")
    return {str(key): item for key, item in parsed.items()}


def _db_time(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise RuntimeError("corrupt onboarding timestamp")
        return _timestamp(parsed)
    raise RuntimeError("corrupt onboarding timestamp")


def _required_time(value: object) -> datetime:
    parsed = _db_time(value)
    if parsed is None:
        raise RuntimeError("corrupt onboarding timestamp")
    return parsed


def _row_value(row: asyncpg.Record, key: str) -> object:
    return row[key]


def _int_value(row: asyncpg.Record, key: str) -> int:
    return int(cast(int, _row_value(row, key)))


class PostgresBootstrapStore(BootstrapStorePort):
    """Production Device Bootstrap/Claim/Activation authority.

    The default ``api`` role is the only runtime role.  ``maintenance`` is
    available for the deployment migration and controlled manufacturing
    registration; it is still ``NOBYPASSRLS`` and is explicitly represented in
    the schema policy rather than relying on ownership or a GUC impersonation.
    """

    def __init__(
        self,
        dsn: str,
        *,
        role: BootstrapPostgresRole = "api",
        schema_path: str | Path | None = None,
    ) -> None:
        if role not in _ROLE_NAMES:
            raise ValueError(f"unsupported Device Onboarding PostgreSQL role: {role}")
        self._dsn = dsn
        self.role = role
        self._schema_path = Path(schema_path) if schema_path is not None else _SCHEMA_PATH
        self._runner = _AsyncLoopRunner()
        self._pool: asyncpg.Pool | None = None
        self._initialized = False

    def initialize(
        self,
        *,
        bootstrap_dsn: str | None = None,
        role_password: str | None = None,
    ) -> None:
        """Run the forward-only schema as an administrator, then pin runtime role."""

        self._runner.call(
            self._initialize_async(
                bootstrap_dsn=bootstrap_dsn,
                role_password=role_password,
            )
        )

    async def _initialize_async(
        self,
        *,
        bootstrap_dsn: str | None,
        role_password: str | None,
    ) -> None:
        if self._initialized:
            return
        if bootstrap_dsn is not None:
            await self._bootstrap(bootstrap_dsn, role_password=role_password)
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=8,
            command_timeout=30,
        )
        assert self._pool is not None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT current_user AS name, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
            expected = _ROLE_NAMES[self.role]
            if (
                row is None
                or str(row["name"]) != expected
                or bool(row["rolsuper"])
                or bool(row["rolbypassrls"])
            ):
                await self._pool.close()
                self._pool = None
                raise PostgresBootstrapStoreContextError(
                    f"Device Onboarding DSN must authenticate as {expected} "
                    "with NOSUPERUSER NOBYPASSRLS"
                )
            required_tables = (
                "device_onboarding_devices",
                "device_onboarding_sessions",
                "device_onboarding_events",
                "device_onboarding_challenges",
                "device_media_challenges",
                "device_onboarding_claims",
                "device_onboarding_bindings",
                "device_onboarding_activations",
            )
            security_rows = await connection.fetch(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])",
                list(required_tables),
            )
            security = {str(item["relname"]): item for item in security_rows}
            if set(security) != set(required_tables) or any(
                not bool(security[name]["relrowsecurity"])
                or not bool(security[name]["relforcerowsecurity"])
                for name in required_tables
            ):
                await self._pool.close()
                self._pool = None
                raise PostgresBootstrapStoreContextError(
                    "Device Onboarding schema is not fully FORCE RLS protected"
                )
        self._initialized = True

    async def _bootstrap(self, dsn: str, *, role_password: str | None) -> None:
        connection = await asyncpg.connect(dsn)
        try:
            row = await connection.fetchrow(
                "SELECT current_user AS name, rolsuper, rolcreaterole "
                "FROM pg_roles WHERE rolname = current_user"
            )
            if row is None or (not bool(row["rolsuper"]) and not bool(row["rolcreaterole"])):
                raise PostgresBootstrapStoreContextError(
                    "Device Onboarding schema requires an administrator/CREATEROLE DSN"
                )
            await connection.execute(self._schema_path.read_text(encoding="utf-8"))
            if role_password is not None:
                for role_name in _ROLE_NAMES.values():
                    statement = await connection.fetchval(
                        "SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', "
                        "$1::text, $2::text)",
                        role_name,
                        role_password,
                    )
                    assert isinstance(statement, str)
                    await connection.execute(statement)
        finally:
            await connection.close()

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None or not self._initialized:
            raise RuntimeError("PostgresBootstrapStore.initialize() must run first")
        return self._pool

    @staticmethod
    def _validate_context_value(value: str, *, field: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise PostgresBootstrapStoreContextError(
                f"app.device_onboarding.{field} must be a bounded non-empty value"
            )
        return normalized

    @contextmanager
    def actor_context(self, actor_id: str) -> Iterator[PostgresBootstrapStore]:
        """Bind an actor for operations whose service call has an actor."""

        actor_id = self._validate_context_value(actor_id, field="actor_id")
        current = _SCOPE.get()
        if current is not None and current.actor_id not in (None, actor_id):
            raise PostgresBootstrapStoreContextError("actor context cannot be switched implicitly")
        token = _SCOPE.set(
            _Scope(
                actor_id=actor_id,
                device_id=current.device_id if current is not None else None,
            )
        )
        try:
            yield self
        finally:
            _SCOPE.reset(token)

    @contextmanager
    def device_context(self, device_id: str) -> Iterator[PostgresBootstrapStore]:
        """Bind the device identity for device-originated operations."""

        device_id = self._validate_context_value(device_id, field="device_id")
        current = _SCOPE.get()
        if current is not None and current.device_id not in (None, device_id):
            raise PostgresBootstrapStoreContextError("device context cannot be switched implicitly")
        token = _SCOPE.set(
            _Scope(
                actor_id=current.actor_id if current is not None else None,
                device_id=device_id,
            )
        )
        try:
            yield self
        finally:
            _SCOPE.reset(token)

    def _scope(
        self,
        *,
        actor_id: str | None = None,
        device_id: str | None = None,
        lookup_kind: str | None = None,
        lookup_id: str | None = None,
    ) -> _Scope:
        ambient = _SCOPE.get()
        if actor_id is not None:
            actor_id = self._validate_context_value(actor_id, field="actor_id")
        if device_id is not None:
            device_id = self._validate_context_value(device_id, field="device_id")
        if ambient is not None:
            if actor_id is not None and ambient.actor_id not in (None, actor_id):
                raise PostgresBootstrapStoreContextError("actor context mismatch")
            if device_id is not None and ambient.device_id not in (None, device_id):
                raise PostgresBootstrapStoreContextError("device context mismatch")
            actor_id = actor_id or ambient.actor_id
            device_id = device_id or ambient.device_id
            if lookup_kind is None:
                lookup_kind = ambient.lookup_kind
                lookup_id = ambient.lookup_id
        if actor_id is None and device_id is None and lookup_kind is None:
            if lookup_id is not None:
                raise PostgresBootstrapStoreContextError("lookup kind is required")
            raise PostgresBootstrapStoreContextError(
                "actor/device/lookup context is required (fail closed)"
            )
        if lookup_kind is not None:
            if lookup_id is None:
                raise PostgresBootstrapStoreContextError("lookup id is required")
            lookup_id = self._validate_context_value(lookup_id, field="lookup_id")
        return _Scope(actor_id, device_id, lookup_kind, lookup_id)

    def _device_read_scope(self, device_id: str) -> _Scope:
        """Prefer an ambient actor for user reads; otherwise use device scope.

        ``DeviceOnboardingService._device()`` has a legacy narrow signature
        containing only ``device_id``.  With no ambient request principal it
        therefore uses a device lookup for the signed device protocol.  When a
        caller has explicitly installed an actor context, the same read must
        not silently add a broader device context and expose another actor's
        bound device.
        """

        ambient = _SCOPE.get()
        if ambient is not None and ambient.actor_id is not None and ambient.device_id is None:
            return self._scope(actor_id=ambient.actor_id)
        return self._scope(device_id=device_id, lookup_kind="device", lookup_id=device_id)

    async def _set_scope(self, connection: asyncpg.Connection, scope: _Scope) -> None:
        values = {
            "actor_id": scope.actor_id or "",
            "device_id": scope.device_id or "",
            "lookup_kind": scope.lookup_kind or "",
            "lookup_id": scope.lookup_id or "",
        }
        for name, value in values.items():
            await connection.execute(
                "SELECT set_config('app.device_onboarding.' || $1, $2, true)",
                name,
                value,
            )

    async def _authorize_locked_device_mutation(
        self,
        connection: asyncpg.Connection,
        *,
        device_id: str,
        lookup_kind: str,
        lookup_id: str,
    ) -> None:
        """Upgrade an exact read scope using identity from its locked row.

        Lookup GUCs intentionally authorize reads only. System-owned expiry
        transitions may mutate a row only after it has been selected and
        locked, and must derive the device write authority from that row rather
        than from caller input.
        """

        await self._set_scope(
            connection,
            self._scope(
                device_id=device_id,
                lookup_kind=lookup_kind,
                lookup_id=lookup_id,
            ),
        )

    async def _transaction(
        self,
        scope: _Scope,
        operation: Callable[[asyncpg.Connection], Awaitable[_T]],
    ) -> _T:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_scope(connection, scope)
                return await operation(connection)

    def _call(self, awaitable: Awaitable[_T]) -> _T:
        try:
            return self._runner.call(awaitable)
        except concurrent.futures.CancelledError as exc:
            raise RuntimeError("device onboarding PostgreSQL operation was cancelled") from exc

    def close(self) -> None:
        if self._pool is not None:
            self._call(self._close_pool())
        self._runner.close()

    async def _close_pool(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        self._initialized = False

    @staticmethod
    def _device(row: asyncpg.Record) -> DeviceRecord:
        return DeviceRecord(
            device_id=str(_row_value(row, "device_id")),
            certificate_id=str(_row_value(row, "certificate_id")),
            public_key=b64url_decode(
                str(_row_value(row, "public_key_b64")), field="public_key", exact_length=32
            ),
            product_model=str(_row_value(row, "product_model")),
            hardware_revision=str(_row_value(row, "hardware_revision")),
            firmware_version=str(_row_value(row, "firmware_version")),
            firmware_security_version=_int_value(row, "firmware_security_version"),
            capability_manifest_hash=str(_row_value(row, "capability_manifest_hash")),
            minimum_firmware_security_version=_int_value(
                row, "minimum_firmware_security_version"
            ),
            lifecycle_status=DeviceLifecycle(str(_row_value(row, "lifecycle_status"))),
            last_monotonic_counter=_int_value(row, "last_monotonic_counter"),
            binding_id=(
                str(_row_value(row, "binding_id"))
                if _row_value(row, "binding_id") is not None
                else None
            ),
            binding_version=(
                _int_value(row, "binding_version")
                if _row_value(row, "binding_version") is not None
                else None
            ),
            actor_id=(
                str(_row_value(row, "actor_id"))
                if _row_value(row, "actor_id") is not None
                else None
            ),
            activation_version=_int_value(row, "activation_version"),
            last_activation_counter=_int_value(row, "last_activation_counter"),
        )

    @staticmethod
    def _session(row: asyncpg.Record) -> BootstrapSession:
        return BootstrapSession(
            onboarding_session_id=str(_row_value(row, "onboarding_session_id")),
            device_id=str(_row_value(row, "device_id")),
            actor_id=str(_row_value(row, "actor_id")),
            client_onboarding_id=str(_row_value(row, "client_onboarding_id")),
            qr_nonce_hash=str(_row_value(row, "qr_nonce_hash")),
            pop_hash=str(_row_value(row, "pop_hash")),
            mobile_nonce_hash=str(_row_value(row, "mobile_nonce_hash")),
            protocol_version=_int_value(row, "protocol_version"),
            ble_name=str(_row_value(row, "ble_name")),
            ble_service_uuid=str(_row_value(row, "ble_service_uuid")),
            state=BootstrapState(str(_row_value(row, "state"))),
            state_version=_int_value(row, "state_version"),
            first_seen_at=_required_time(_row_value(row, "first_seen_at")),
            expires_at=_required_time(_row_value(row, "expires_at")),
            proximity_verified_at=_db_time(_row_value(row, "proximity_verified_at")),
            wifi_connected_at=_db_time(_row_value(row, "wifi_connected_at")),
            device_online_at=_db_time(_row_value(row, "device_online_at")),
            cancelled_at=_db_time(_row_value(row, "cancelled_at")),
            consumed_at=_db_time(_row_value(row, "consumed_at")),
            failure_code=(
                str(_row_value(row, "failure_code"))
                if _row_value(row, "failure_code") is not None
                else None
            ),
        )

    @staticmethod
    def _challenge(row: asyncpg.Record) -> DeviceChallenge:
        return DeviceChallenge(
            challenge_id=str(_row_value(row, "challenge_id")),
            onboarding_session_id=str(_row_value(row, "onboarding_session_id")),
            device_id=str(_row_value(row, "device_id")),
            nonce_hash=str(_row_value(row, "nonce_hash")),
            issued_at=_required_time(_row_value(row, "issued_at")),
            expires_at=_required_time(_row_value(row, "expires_at")),
            used_at=_db_time(_row_value(row, "used_at")),
        )

    @staticmethod
    def _media_challenge(row: asyncpg.Record) -> DeviceMediaChallenge:
        return DeviceMediaChallenge(
            challenge_id=str(_row_value(row, "challenge_id")),
            device_id=str(_row_value(row, "device_id")),
            certificate_id=str(_row_value(row, "certificate_id")),
            client_id=str(_row_value(row, "client_id")),
            nonce_hash=str(_row_value(row, "nonce_hash")),
            issued_at=_required_time(_row_value(row, "issued_at")),
            expires_at=_required_time(_row_value(row, "expires_at")),
            used_at=_db_time(_row_value(row, "used_at")),
        )

    @staticmethod
    def _claim(row: asyncpg.Record) -> ClaimReservation:
        return ClaimReservation(
            claim_id=str(_row_value(row, "claim_id")),
            onboarding_session_id=str(_row_value(row, "onboarding_session_id")),
            device_id=str(_row_value(row, "device_id")),
            actor_id=str(_row_value(row, "actor_id")),
            status=ClaimStatus(str(_row_value(row, "status"))),
            idempotency_key=str(_row_value(row, "idempotency_key")),
            reserved_at=_required_time(_row_value(row, "reserved_at")),
            expires_at=_required_time(_row_value(row, "expires_at")),
            binding_id=(
                str(_row_value(row, "binding_id"))
                if _row_value(row, "binding_id") is not None
                else None
            ),
            binding_version=(
                _int_value(row, "binding_version")
                if _row_value(row, "binding_version") is not None
                else None
            ),
            committed_at=_db_time(_row_value(row, "committed_at")),
            released_at=_db_time(_row_value(row, "released_at")),
        )

    @staticmethod
    def _binding(row: asyncpg.Record) -> BindingRecord:
        return BindingRecord(
            binding_id=str(_row_value(row, "binding_id")),
            claim_id=str(_row_value(row, "claim_id")),
            device_id=str(_row_value(row, "device_id")),
            actor_id=str(_row_value(row, "actor_id")),
            binding_version=_int_value(row, "binding_version"),
            status=str(_row_value(row, "status")),
            initialization=BindingInitialization.from_mapping(
                _json_object(_row_value(row, "initialization_json"))
            ),
            created_at=_required_time(_row_value(row, "created_at")),
            committed_at=_db_time(_row_value(row, "committed_at")),
        )

    @staticmethod
    def _activation(row: asyncpg.Record) -> ActivationRecord:
        manifest = _json_object(_row_value(row, "manifest_json"))
        validate_activation_manifest(manifest)
        return ActivationRecord(
            activation_id=str(_row_value(row, "activation_id")),
            device_id=str(_row_value(row, "device_id")),
            claim_id=str(_row_value(row, "claim_id")),
            binding_id=str(_row_value(row, "binding_id")),
            binding_version=_int_value(row, "binding_version"),
            activation_version=_int_value(row, "activation_version"),
            manifest=manifest,
            manifest_hash=str(_row_value(row, "manifest_hash")),
            status=ActivationStatus(str(_row_value(row, "status"))),
            issued_at=_required_time(_row_value(row, "issued_at")),
            expires_at=_required_time(_row_value(row, "expires_at")),
            downloaded_at=_db_time(_row_value(row, "downloaded_at")),
            applied_at=_db_time(_row_value(row, "applied_at")),
            acknowledged_at=_db_time(_row_value(row, "acknowledged_at")),
            ack_counter=(
                _int_value(row, "ack_counter")
                if _row_value(row, "ack_counter") is not None
                else None
            ),
        )

    # ------------------------------------------------------------------
    # Device identity authority
    # ------------------------------------------------------------------

    def register_manufactured_device(self, device: DeviceRecord) -> DeviceRecord:
        """Register public identity from the controlled manufacturing role."""

        if self.role != "maintenance":
            raise InvalidOnboardingRequest(
                "manufactured device registration requires the maintenance role"
            )
        if len(device.public_key) != 32:
            raise InvalidOnboardingRequest("Ed25519 public key is invalid")

        async def operation(connection: asyncpg.Connection) -> None:
            try:
                await connection.execute(
                    """
                    INSERT INTO device_onboarding_devices (
                        device_id, certificate_id, public_key_b64, product_model,
                        hardware_revision, firmware_version, firmware_security_version,
                        capability_manifest_hash, minimum_firmware_security_version,
                        lifecycle_status, last_monotonic_counter, binding_id,
                        binding_version, actor_id, activation_version,
                        last_activation_counter, created_at, updated_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12, $13, $14, $15, $16, $17, $17
                    )
                    """,
                    device.device_id,
                    device.certificate_id,
                    b64url_encode(device.public_key),
                    device.product_model,
                    device.hardware_revision,
                    device.firmware_version,
                    device.firmware_security_version,
                    device.capability_manifest_hash,
                    device.minimum_firmware_security_version,
                    device.lifecycle_status.value,
                    device.last_monotonic_counter,
                    device.binding_id,
                    device.binding_version,
                    device.actor_id,
                    device.activation_version,
                    device.last_activation_counter,
                    datetime.now(UTC),
                )
            except asyncpg.UniqueViolationError as exc:
                raise ClaimConflict("device identity already exists") from exc

        self._call(self._transaction(_Scope(), operation))
        return device

    def get_device(self, device_id: str) -> DeviceRecord | None:
        scope = self._device_read_scope(device_id)

        async def operation(connection: asyncpg.Connection) -> DeviceRecord | None:
            row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_devices WHERE device_id = $1",
                device_id,
            )
            return self._device(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

    def active_claim_for_device(
        self, *, device_id: str, now: datetime
    ) -> ClaimReservation | None:
        scope = self._scope(device_id=device_id)
        active = tuple(item for item in _DEVICE_ACTIVE_CLAIMS)

        async def operation(connection: asyncpg.Connection) -> ClaimReservation | None:
            row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_claims
                WHERE device_id = $1 AND status = ANY($2::text[]) AND expires_at > $3
                ORDER BY reserved_at DESC LIMIT 1
                """,
                device_id,
                list(active),
                _timestamp(now),
            )
            return self._claim(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

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
                        device_online_at, cancelled_at, consumed_at, failure_code
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14, $15, $16, $17, $18, $19, $20
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
    ) -> None:
        await connection.execute(
            """
            INSERT INTO device_onboarding_events (
                event_id, onboarding_session_id, device_id, event_type,
                previous_state, next_state, actor_type, actor_id, reason_code,
                payload_redacted, occurred_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, '{}'::jsonb, $10)
            """,
            f"evt_{uuid.uuid4().hex}",
            session.onboarding_session_id,
            session.device_id,
            event_type,
            previous_state.value if previous_state else None,
            next_state.value if next_state else None,
            actor_type,
            actor_id,
            reason_code,
            datetime.now(UTC),
        )

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
        scope = self._scope(device_id=device_id)
        current_time = _timestamp(now)

        async def operation(connection: asyncpg.Connection) -> BootstrapSession:
            challenge_row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_challenges
                WHERE challenge_id = $1 FOR UPDATE
                """,
                challenge_id,
            )
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
            session_row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_sessions
                WHERE onboarding_session_id = $1 FOR UPDATE
                """,
                onboarding_session_id,
            )
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            if session.expires_at <= current_time:
                raise SessionExpired()
            device_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_devices WHERE device_id = $1 FOR UPDATE",
                device_id,
            )
            if device_row is None:
                raise DeviceNotFound()
            device = self._device(device_row)
            if device.lifecycle_status is DeviceLifecycle.REVOKED:
                raise DeviceRevoked()
            if device.lifecycle_status is DeviceLifecycle.BOUND:
                raise DeviceAlreadyBound()
            if monotonic_counter <= device.last_monotonic_counter:
                raise ClaimConflict("device monotonic counter is not increasing")
            await connection.execute(
                "UPDATE device_onboarding_challenges SET used_at = $1 WHERE challenge_id = $2",
                current_time,
                challenge_id,
            )
            await connection.execute(
                """
                UPDATE device_onboarding_devices
                SET lifecycle_status = $1, last_monotonic_counter = $2,
                    firmware_version = $3, firmware_security_version = $4, updated_at = $5
                WHERE device_id = $6
                """,
                DeviceLifecycle.PROVISIONED.value,
                monotonic_counter,
                firmware_version,
                firmware_security_version,
                current_time,
                device_id,
            )
            target = BootstrapState.DEVICE_ONLINE
            require_transition(session.state, target)
            await connection.execute(
                """
                UPDATE device_onboarding_sessions
                SET state = $1, state_version = state_version + 1,
                    proximity_verified_at = COALESCE(proximity_verified_at, $2),
                    wifi_connected_at = COALESCE(wifi_connected_at, $2),
                    device_online_at = $2
                WHERE onboarding_session_id = $3 AND state_version = $4
                """,
                target.value,
                current_time,
                onboarding_session_id,
                session.state_version,
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
                event_type="device_online_proof_accepted",
                previous_state=session.state,
                next_state=target,
                actor_type="device",
                actor_id=device_id,
                reason_code=None,
            )
            return updated

        return self._call(self._transaction(scope, operation))

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
        scope = self._scope(actor_id=claim.actor_id, device_id=claim.device_id)
        current_time = _timestamp(now)
        active = tuple(item for item in _DEVICE_ACTIVE_CLAIMS)

        async def operation(connection: asyncpg.Connection) -> ClaimReservation:
            session_row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_sessions
                WHERE onboarding_session_id = $1 FOR UPDATE
                """,
                claim.onboarding_session_id,
            )
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            if session.state_version != expected_state_version:
                raise StateVersionConflict("onboarding state version changed")
            if session.device_id != claim.device_id or session.actor_id != claim.actor_id:
                raise ActorMismatch()
            if session.expires_at <= current_time:
                raise SessionExpired()
            if not session.proximity_verified_at or not session.device_online_at:
                raise ClaimConflict("device online proof and proximity are required")

            existing_session_row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_claims
                WHERE onboarding_session_id = $1 FOR UPDATE
                """,
                claim.onboarding_session_id,
            )
            if existing_session_row is not None:
                existing = self._claim(existing_session_row)
                if existing.actor_id != claim.actor_id:
                    raise ClaimConflict("onboarding session belongs to another actor")
                if existing.idempotency_key == claim.idempotency_key:
                    return existing
                if existing.status is ClaimStatus.COMMITTED:
                    return existing
                raise ClaimConflict("claim idempotency key conflicts")

            # All sessions for one device serialize on the same device row.
            # This closes the window between the active-claim check and INSERT.
            device_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_devices WHERE device_id = $1 FOR UPDATE",
                claim.device_id,
            )
            if device_row is None:
                raise DeviceNotFound()
            device = self._device(device_row)
            if device.lifecycle_status is DeviceLifecycle.REVOKED:
                raise DeviceRevoked()
            if device.lifecycle_status is DeviceLifecycle.BOUND:
                raise DeviceAlreadyBound()
            active_row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_claims
                WHERE device_id = $1 AND status = ANY($2::text[]) AND expires_at > $3
                ORDER BY reserved_at DESC LIMIT 1 FOR UPDATE
                """,
                claim.device_id,
                list(active),
                current_time,
            )
            if active_row is not None:
                active_claim = self._claim(active_row)
                if active_claim.actor_id != claim.actor_id:
                    raise ClaimConflict("device is being claimed by another actor")
                raise ClaimConflict("device already has an active claim")
            try:
                await connection.execute(
                    """
                    INSERT INTO device_onboarding_claims (
                        claim_id, onboarding_session_id, device_id, actor_id, status,
                        idempotency_key, reserved_at, expires_at, binding_id,
                        binding_version, committed_at, released_at, failure_code
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL, NULL, NULL, NULL, NULL)
                    """,
                    claim.claim_id,
                    claim.onboarding_session_id,
                    claim.device_id,
                    claim.actor_id,
                    claim.status.value,
                    claim.idempotency_key,
                    _timestamp(claim.reserved_at),
                    _timestamp(claim.expires_at),
                )
            except asyncpg.UniqueViolationError as exc:
                raise ClaimConflict("device claim already exists") from exc
            require_transition(session.state, BootstrapState.CLAIM_RESERVED)
            await connection.execute(
                """
                UPDATE device_onboarding_sessions
                SET state = $1, state_version = state_version + 1
                WHERE onboarding_session_id = $2 AND state_version = $3
                """,
                BootstrapState.CLAIM_RESERVED.value,
                claim.onboarding_session_id,
                expected_state_version,
            )
            updated_session_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = $1",
                claim.onboarding_session_id,
            )
            if updated_session_row is None:
                raise SessionNotFound()
            await self._event(
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

        return self._call(self._transaction(scope, operation))

    def get_claim(self, claim_id: str) -> ClaimReservation | None:
        scope = self._scope(lookup_kind="claim", lookup_id=claim_id)

        async def operation(connection: asyncpg.Connection) -> ClaimReservation | None:
            row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = $1",
                claim_id,
            )
            return self._claim(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

    def get_claim_for_session(self, onboarding_session_id: str) -> ClaimReservation | None:
        scope = self._scope(lookup_kind="session", lookup_id=onboarding_session_id)

        async def operation(connection: asyncpg.Connection) -> ClaimReservation | None:
            row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_claims
                WHERE onboarding_session_id = $1
                """,
                onboarding_session_id,
            )
            return self._claim(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

    def expire_claim_if_needed(self, claim_id: str, *, now: datetime) -> ClaimReservation:
        scope = self._scope(lookup_kind="claim", lookup_id=claim_id)
        current_time = _timestamp(now)

        async def operation(connection: asyncpg.Connection) -> ClaimReservation:
            row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = $1 FOR UPDATE",
                claim_id,
            )
            if row is None:
                raise ClaimNotFound()
            claim = self._claim(row)
            if claim.status in {ClaimStatus.COMMITTED, ClaimStatus.RELEASED, ClaimStatus.EXPIRED}:
                return claim
            if claim.expires_at > current_time:
                return claim
            await self._authorize_locked_device_mutation(
                connection,
                device_id=claim.device_id,
                lookup_kind="claim",
                lookup_id=claim_id,
            )
            await connection.execute(
                """
                UPDATE device_onboarding_claims
                SET status = $1, released_at = $2, failure_code = $3
                WHERE claim_id = $4 AND status IN ($5, $6)
                """,
                ClaimStatus.EXPIRED.value,
                current_time,
                "CLAIM_EXPIRED",
                claim_id,
                ClaimStatus.RESERVED.value,
                ClaimStatus.BINDING_COMMITTING.value,
            )
            updated = await connection.fetchrow(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = $1",
                claim_id,
            )
            if updated is None:
                raise ClaimNotFound()
            return self._claim(updated)

        return self._call(self._transaction(scope, operation))

    # ------------------------------------------------------------------
    # Binding Saga operations
    # ------------------------------------------------------------------

    def begin_binding(
        self,
        *,
        binding: BindingRecord,
        idempotency_key: str,
        expected_state_version: int,
        now: datetime,
    ) -> BindingRecord:
        scope = self._scope(actor_id=binding.actor_id, device_id=binding.device_id)
        current_time = _timestamp(now)

        async def operation(connection: asyncpg.Connection) -> BindingRecord:
            claim_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = $1 FOR UPDATE",
                binding.claim_id,
            )
            if claim_row is None:
                raise ClaimNotFound()
            claim = self._claim(claim_row)
            if claim.actor_id != binding.actor_id:
                raise ActorMismatch()
            if claim.expires_at <= current_time:
                raise ClaimExpired()
            if claim.status is ClaimStatus.COMMITTED:
                existing = await connection.fetchrow(
                    "SELECT * FROM device_onboarding_bindings WHERE claim_id = $1",
                    binding.claim_id,
                )
                if existing is None:
                    raise BindingConflict("committed claim is missing binding")
                return self._binding(existing)
            if claim.status is ClaimStatus.BINDING_COMMITTING:
                existing = await connection.fetchrow(
                    "SELECT * FROM device_onboarding_bindings WHERE claim_id = $1",
                    binding.claim_id,
                )
                if existing is not None:
                    current = self._binding(existing)
                    if current.initialization != binding.initialization:
                        raise BindingConflict("binding initialization conflicts")
                    return current
                raise BindingConflict("binding transaction is already in progress")
            if claim.status is not ClaimStatus.RESERVED:
                raise BindingConflict("claim is not reservable")
            session_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = $1 FOR UPDATE",
                claim.onboarding_session_id,
            )
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            if session.state_version != expected_state_version:
                raise StateVersionConflict("onboarding state version changed")
            require_transition(session.state, BootstrapState.BINDING_COMMITTING)
            try:
                await connection.execute(
                    """
                    INSERT INTO device_onboarding_bindings (
                        binding_id, claim_id, device_id, actor_id, binding_version,
                        status, initialization_json, idempotency_key, created_at, committed_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, NULL)
                    """,
                    binding.binding_id,
                    binding.claim_id,
                    binding.device_id,
                    binding.actor_id,
                    binding.binding_version,
                    binding.status,
                    _json(binding.initialization.to_dict()),
                    idempotency_key,
                    _timestamp(binding.created_at),
                )
            except asyncpg.UniqueViolationError as exc:
                raise BindingConflict("binding idempotency already exists") from exc
            await connection.execute(
                """
                UPDATE device_onboarding_claims
                SET status = $1, binding_id = $2, binding_version = $3
                WHERE claim_id = $4 AND status = $5
                """,
                ClaimStatus.BINDING_COMMITTING.value,
                binding.binding_id,
                binding.binding_version,
                binding.claim_id,
                ClaimStatus.RESERVED.value,
            )
            await connection.execute(
                """
                UPDATE device_onboarding_sessions
                SET state = $1, state_version = state_version + 1
                WHERE onboarding_session_id = $2 AND state_version = $3
                """,
                BootstrapState.BINDING_COMMITTING.value,
                claim.onboarding_session_id,
                expected_state_version,
            )
            updated = await connection.fetchrow(
                "SELECT * FROM device_onboarding_bindings WHERE binding_id = $1",
                binding.binding_id,
            )
            updated_session = await connection.fetchrow(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = $1",
                claim.onboarding_session_id,
            )
            if updated is None or updated_session is None:
                raise BindingConflict("binding transaction could not be created")
            await self._event(
                connection,
                session=self._session(updated_session),
                event_type="binding_begin",
                previous_state=session.state,
                next_state=BootstrapState.BINDING_COMMITTING,
                actor_type="user",
                actor_id=binding.actor_id,
                reason_code=None,
            )
            return self._binding(updated)

        return self._call(self._transaction(scope, operation))

    def get_binding(self, binding_id: str) -> BindingRecord | None:
        scope = self._scope(lookup_kind="binding", lookup_id=binding_id)

        async def operation(connection: asyncpg.Connection) -> BindingRecord | None:
            row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_bindings WHERE binding_id = $1",
                binding_id,
            )
            return self._binding(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

    def get_binding_for_claim(self, claim_id: str) -> BindingRecord | None:
        scope = self._scope(lookup_kind="claim", lookup_id=claim_id)

        async def operation(connection: asyncpg.Connection) -> BindingRecord | None:
            row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_bindings WHERE claim_id = $1",
                claim_id,
            )
            return self._binding(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

    def adopt_binding_authority(
        self,
        *,
        claim_id: str,
        actor_id: str,
        binding_id: str,
        binding_version: int,
    ) -> BindingRecord:
        scope = self._scope(actor_id=actor_id)

        async def operation(connection: asyncpg.Connection) -> BindingRecord:
            claim_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = $1 FOR UPDATE",
                claim_id,
            )
            if claim_row is None:
                raise ClaimNotFound()
            claim = self._claim(claim_row)
            if claim.actor_id != actor_id:
                raise ActorMismatch()
            if claim.status is ClaimStatus.COMMITTED:
                existing = await connection.fetchrow(
                    "SELECT * FROM device_onboarding_bindings WHERE claim_id = $1",
                    claim_id,
                )
                if existing is None:
                    raise BindingConflict("committed claim is missing binding")
                adopted = self._binding(existing)
                if adopted.binding_id != binding_id or adopted.binding_version != binding_version:
                    raise BindingConflict("binding authority result conflicts")
                return adopted
            if claim.status is not ClaimStatus.BINDING_COMMITTING:
                raise BindingConflict("binding is not waiting for authority")
            binding_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_bindings WHERE claim_id = $1 FOR UPDATE",
                claim_id,
            )
            if binding_row is None:
                raise BindingConflict("binding begin is required")
            binding = self._binding(binding_row)
            if binding.status != "draft":
                raise BindingConflict("binding draft cannot adopt authority")
            if binding.binding_id == binding_id and binding.binding_version == binding_version:
                return binding
            try:
                await connection.execute(
                    """
                    UPDATE device_onboarding_bindings
                    SET binding_id = $1, binding_version = $2
                    WHERE claim_id = $3 AND status = 'draft'
                    """,
                    binding_id,
                    binding_version,
                    claim_id,
                )
            except asyncpg.UniqueViolationError as exc:
                raise BindingConflict("binding authority id already exists") from exc
            await connection.execute(
                """
                UPDATE device_onboarding_claims
                SET binding_id = $1, binding_version = $2
                WHERE claim_id = $3 AND status = $4
                """,
                binding_id,
                binding_version,
                claim_id,
                ClaimStatus.BINDING_COMMITTING.value,
            )
            adopted_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_bindings WHERE claim_id = $1",
                claim_id,
            )
            if adopted_row is None:
                raise BindingConflict("binding authority adoption failed")
            return self._binding(adopted_row)

        return self._call(self._transaction(scope, operation))

    def release_binding(
        self,
        *,
        claim_id: str,
        actor_id: str,
        reason_code: str,
        now: datetime,
    ) -> ClaimReservation:
        scope = self._scope(actor_id=actor_id)
        current_time = _timestamp(now)

        async def operation(connection: asyncpg.Connection) -> ClaimReservation:
            claim_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = $1 FOR UPDATE",
                claim_id,
            )
            if claim_row is None:
                raise ClaimNotFound()
            claim = self._claim(claim_row)
            if claim.actor_id != actor_id:
                raise ActorMismatch()
            if claim.status is ClaimStatus.COMMITTED:
                raise BindingConflict("committed binding cannot be released")
            if claim.status in {ClaimStatus.RELEASED, ClaimStatus.EXPIRED}:
                return claim
            await connection.execute(
                """
                UPDATE device_onboarding_claims
                SET status = $1, released_at = $2, failure_code = $3
                WHERE claim_id = $4
                """,
                ClaimStatus.RELEASED.value,
                current_time,
                reason_code,
                claim_id,
            )
            await connection.execute(
                "UPDATE device_onboarding_bindings SET status = 'released' WHERE claim_id = $1",
                claim_id,
            )
            session_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = $1 FOR UPDATE",
                claim.onboarding_session_id,
            )
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            if (
                session.state not in _TERMINAL_SESSION_STATES
                and session.state is not BootstrapState.DEVICE_ONLINE
            ):
                require_transition(session.state, BootstrapState.DEVICE_ONLINE)
                await connection.execute(
                    """
                    UPDATE device_onboarding_sessions
                    SET state = $1, state_version = state_version + 1
                    WHERE onboarding_session_id = $2
                    """,
                    BootstrapState.DEVICE_ONLINE.value,
                    claim.onboarding_session_id,
                )
                updated_session_row = await connection.fetchrow(
                    "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = $1",
                    claim.onboarding_session_id,
                )
                if updated_session_row is None:
                    raise SessionNotFound()
                await self._event(
                    connection,
                    session=self._session(updated_session_row),
                    event_type="binding_released",
                    previous_state=session.state,
                    next_state=BootstrapState.DEVICE_ONLINE,
                    actor_type="user",
                    actor_id=actor_id,
                    reason_code=reason_code,
                )
            updated = await connection.fetchrow(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = $1",
                claim_id,
            )
            if updated is None:
                raise ClaimNotFound()
            return self._claim(updated)

        return self._call(self._transaction(scope, operation))

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
    ) -> tuple[BindingRecord, ActivationRecord]:
        validate_activation_manifest(manifest)
        scoped_claim = self.get_claim(claim_id)
        if scoped_claim is None:
            raise ClaimNotFound()
        if scoped_claim.actor_id != actor_id:
            raise ActorMismatch()
        # The manufactured row intentionally has no actor until this commit.
        # Resolve its immutable device id through the exact opaque claim lookup,
        # then bind actor + device in the authoritative locking transaction.
        scope = self._scope(actor_id=actor_id, device_id=scoped_claim.device_id)
        current_time = _timestamp(now)

        async def operation(
            connection: asyncpg.Connection,
        ) -> tuple[BindingRecord, ActivationRecord]:
            claim_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = $1 FOR UPDATE",
                claim_id,
            )
            if claim_row is None:
                raise ClaimNotFound()
            claim = self._claim(claim_row)
            if claim.actor_id != actor_id:
                raise ActorMismatch()
            binding_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_bindings WHERE claim_id = $1 FOR UPDATE",
                claim_id,
            )
            if binding_row is None:
                raise BindingConflict("binding begin is required")
            binding = self._binding(binding_row)
            if binding.binding_id != binding_id or binding.binding_version != binding_version:
                raise BindingConflict("binding authority result does not match reservation")
            session_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = $1 FOR UPDATE",
                claim.onboarding_session_id,
            )
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            device_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_devices WHERE device_id = $1 FOR UPDATE",
                claim.device_id,
            )
            if device_row is None:
                raise DeviceNotFound()
            device = self._device(device_row)
            existing_activation_row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_activations
                WHERE device_id = $1 AND activation_version = $2 FOR UPDATE
                """,
                claim.device_id,
                activation_version,
            )
            if claim.status is ClaimStatus.COMMITTED:
                if existing_activation_row is None:
                    raise BindingConflict("committed claim is missing activation")
                return binding, self._activation(existing_activation_row)
            if claim.status is not ClaimStatus.BINDING_COMMITTING:
                raise BindingConflict("claim is not in binding commit state")
            if binding.status == "committed":
                if existing_activation_row is None:
                    raise BindingConflict("committed binding is missing activation")
                return binding, self._activation(existing_activation_row)
            if device.lifecycle_status is DeviceLifecycle.REVOKED:
                raise DeviceRevoked()
            if device.lifecycle_status is DeviceLifecycle.BOUND:
                raise DeviceAlreadyBound()
            if activation_version != device.activation_version + 1:
                raise BindingConflict("activation version is not monotonic")
            require_transition(session.state, BootstrapState.BOUND)
            await connection.execute(
                """
                UPDATE device_onboarding_bindings
                SET status = $1, committed_at = $2
                WHERE binding_id = $3 AND status = 'draft'
                """,
                "committed",
                current_time,
                binding_id,
            )
            await connection.execute(
                """
                UPDATE device_onboarding_claims
                SET status = $1, binding_id = $2, binding_version = $3, committed_at = $4
                WHERE claim_id = $5 AND status = $6
                """,
                ClaimStatus.COMMITTED.value,
                binding_id,
                binding_version,
                current_time,
                claim_id,
                ClaimStatus.BINDING_COMMITTING.value,
            )
            await connection.execute(
                """
                UPDATE device_onboarding_devices
                SET lifecycle_status = $1, binding_id = $2, binding_version = $3,
                    actor_id = $4, activation_version = $5, updated_at = $6
                WHERE device_id = $7 AND lifecycle_status IN ($8, $9)
                """,
                DeviceLifecycle.BOUND.value,
                binding_id,
                binding_version,
                actor_id,
                activation_version,
                current_time,
                claim.device_id,
                DeviceLifecycle.MANUFACTURED.value,
                DeviceLifecycle.PROVISIONED.value,
            )
            await connection.execute(
                """
                UPDATE device_onboarding_sessions
                SET state = $1, state_version = state_version + 1, consumed_at = $2
                WHERE onboarding_session_id = $3
                """,
                BootstrapState.BOUND.value,
                current_time,
                claim.onboarding_session_id,
            )
            try:
                await connection.execute(
                    """
                    INSERT INTO device_onboarding_activations (
                        activation_id, device_id, actor_id, claim_id, binding_id,
                        binding_version, activation_version, manifest_json, manifest_hash,
                        status, issued_at, expires_at, downloaded_at, applied_at,
                        acknowledged_at, ack_counter, ack_json
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12,
                        NULL, NULL, NULL, NULL, NULL
                    )
                    """,
                    activation_id,
                    claim.device_id,
                    actor_id,
                    claim_id,
                    binding_id,
                    binding_version,
                    activation_version,
                    _json(manifest),
                    manifest_hash,
                    ActivationStatus.MANIFEST_READY.value,
                    current_time,
                    _timestamp(activation_expires_at),
                )
            except asyncpg.UniqueViolationError as exc:
                raise BindingConflict("activation id already exists") from exc
            updated_binding_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_bindings WHERE binding_id = $1",
                binding_id,
            )
            updated_activation_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_activations WHERE activation_id = $1",
                activation_id,
            )
            updated_session_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = $1",
                claim.onboarding_session_id,
            )
            if (
                updated_binding_row is None
                or updated_activation_row is None
                or updated_session_row is None
            ):
                raise BindingConflict("binding commit did not persist")
            await self._event(
                connection,
                session=self._session(updated_session_row),
                event_type="binding_committed",
                previous_state=session.state,
                next_state=BootstrapState.BOUND,
                actor_type="user",
                actor_id=actor_id,
                reason_code=None,
            )
            return self._binding(updated_binding_row), self._activation(updated_activation_row)

        return self._call(self._transaction(scope, operation))

    # ------------------------------------------------------------------
    # Activation and device ACK
    # ------------------------------------------------------------------

    def get_activation(self, activation_id: str) -> ActivationRecord | None:
        scope = self._scope(lookup_kind="activation", lookup_id=activation_id)

        async def operation(connection: asyncpg.Connection) -> ActivationRecord | None:
            row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_activations WHERE activation_id = $1",
                activation_id,
            )
            return self._activation(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

    def latest_activation_for_device(self, device_id: str) -> ActivationRecord | None:
        scope = self._device_read_scope(device_id)

        async def operation(connection: asyncpg.Connection) -> ActivationRecord | None:
            row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_activations
                WHERE device_id = $1 ORDER BY activation_version DESC LIMIT 1
                """,
                device_id,
            )
            return self._activation(row) if row is not None else None

        return self._call(self._transaction(scope, operation))

    def mark_activation_downloaded(
        self, *, device_id: str, now: datetime
    ) -> ActivationRecord:
        scope = self._scope(device_id=device_id)
        current_time = _timestamp(now)

        async def operation(connection: asyncpg.Connection) -> ActivationRecord:
            row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_activations
                WHERE device_id = $1 ORDER BY activation_version DESC LIMIT 1 FOR UPDATE
                """,
                device_id,
            )
            if row is None:
                raise ActivationNotFound()
            activation = self._activation(row)
            if activation.status is ActivationStatus.MANIFEST_READY:
                await connection.execute(
                    """
                    UPDATE device_onboarding_activations
                    SET status = $1, downloaded_at = $2 WHERE activation_id = $3
                    """,
                    ActivationStatus.DEVICE_DOWNLOADING.value,
                    current_time,
                    activation.activation_id,
                )
            updated = await connection.fetchrow(
                "SELECT * FROM device_onboarding_activations WHERE activation_id = $1",
                activation.activation_id,
            )
            if updated is None:
                raise ActivationNotFound()
            return self._activation(updated)

        return self._call(self._transaction(scope, operation))

    def accept_activation_ack(
        self,
        *,
        ack_payload: Mapping[str, object],
        now: datetime,
    ) -> ActivationRecord:
        device_id = str(ack_payload["device_id"])
        activation_version = int(cast(int, ack_payload["activation_version"]))
        scope = self._scope(device_id=device_id)
        current_time = _timestamp(now)

        async def operation(connection: asyncpg.Connection) -> ActivationRecord:
            activation_row = await connection.fetchrow(
                """
                SELECT * FROM device_onboarding_activations
                WHERE device_id = $1 AND activation_version = $2 FOR UPDATE
                """,
                device_id,
                activation_version,
            )
            if activation_row is None:
                raise ActivationNotFound()
            activation = self._activation(activation_row)
            if activation.status in {
                ActivationStatus.DEVICE_ACKNOWLEDGED,
                ActivationStatus.READY_FOR_CONVERSATION,
            }:
                stored = activation_row["ack_json"]
                if stored is not None and _json_object(stored) == dict(ack_payload):
                    return activation
                raise ChallengeReplay("activation was already acknowledged")
            if activation.expires_at <= current_time:
                raise ClaimExpired("activation manifest expired")
            if (
                device_id != activation.device_id
                or str(ack_payload["binding_id"]) != activation.binding_id
                or int(cast(int, ack_payload["binding_version"])) != activation.binding_version
                or activation_version != activation.activation_version
                or str(ack_payload["config_hash"]) != str(activation.manifest["config_hash"])
            ):
                raise BindingConflict("activation ACK does not match manifest")
            claim_row = await connection.fetchrow(
                "SELECT onboarding_session_id FROM device_onboarding_claims WHERE claim_id = $1",
                activation.claim_id,
            )
            session: BootstrapSession | None = None
            session_id: str | None = None
            if claim_row is not None:
                session_id = str(claim_row["onboarding_session_id"])
                session_row = await connection.fetchrow(
                    "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = $1 FOR UPDATE",
                    session_id,
                )
                if session_row is not None:
                    session = self._session(session_row)
            device_row = await connection.fetchrow(
                "SELECT * FROM device_onboarding_devices WHERE device_id = $1 FOR UPDATE",
                activation.device_id,
            )
            if device_row is None:
                raise DeviceNotFound()
            device = self._device(device_row)
            counter = int(cast(int, ack_payload["monotonic_counter"]))
            if counter <= max(device.last_monotonic_counter, device.last_activation_counter):
                raise ChallengeReplay("activation ACK counter was already consumed")
            applied_at = _db_time(ack_payload["applied_at"])
            if applied_at is None:
                raise InvalidOnboardingRequest("activation ACK applied_at is invalid")
            await connection.execute(
                """
                UPDATE device_onboarding_activations
                SET status = $1, applied_at = $2, acknowledged_at = $3,
                    ack_counter = $4, ack_json = $5::jsonb
                WHERE activation_id = $6 AND status IN ($7, $8)
                """,
                ActivationStatus.READY_FOR_CONVERSATION.value,
                applied_at,
                current_time,
                counter,
                _json(ack_payload),
                activation.activation_id,
                ActivationStatus.MANIFEST_READY.value,
                ActivationStatus.DEVICE_DOWNLOADING.value,
            )
            await connection.execute(
                """
                UPDATE device_onboarding_devices
                SET last_monotonic_counter = $1, last_activation_counter = $2,
                    updated_at = $3
                WHERE device_id = $4
                """,
                counter,
                counter,
                current_time,
                activation.device_id,
            )
            if session is not None and session_id is not None:
                if session.state is BootstrapState.BOUND:
                    require_transition(session.state, BootstrapState.ACTIVATED)
                    await connection.execute(
                        """
                        UPDATE device_onboarding_sessions
                        SET state = $1, state_version = state_version + 1
                        WHERE onboarding_session_id = $2
                        """,
                        BootstrapState.ACTIVATED.value,
                        session_id,
                    )
                    updated_session_row = await connection.fetchrow(
                        "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = $1",
                        session_id,
                    )
                    if updated_session_row is not None:
                        await self._event(
                            connection,
                            session=self._session(updated_session_row),
                            event_type="activation_ack_accepted",
                            previous_state=session.state,
                            next_state=BootstrapState.ACTIVATED,
                            actor_type="device",
                            actor_id=activation.device_id,
                            reason_code=None,
                        )
            updated = await connection.fetchrow(
                "SELECT * FROM device_onboarding_activations WHERE activation_id = $1",
                activation.activation_id,
            )
            if updated is None:
                raise ActivationNotFound()
            return self._activation(updated)

        return self._call(self._transaction(scope, operation))

    def is_actor_bound_to_device(self, *, actor_id: str, device_id: str) -> bool:
        scope = self._scope(actor_id=actor_id, device_id=device_id)

        async def operation(connection: asyncpg.Connection) -> bool:
            row = await connection.fetchrow(
                """
                SELECT 1 FROM device_onboarding_devices
                WHERE device_id = $1 AND lifecycle_status = $2 AND actor_id = $3
                """,
                device_id,
                DeviceLifecycle.BOUND.value,
                actor_id,
            )
            return row is not None

        return self._call(self._transaction(scope, operation))


__all__ = [
    "BootstrapPostgresRole",
    "PostgresBootstrapStore",
    "PostgresBootstrapStoreContextError",
]
