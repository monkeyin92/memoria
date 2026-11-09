"""Sync-over-async facade for the device-bootstrap PostgreSQL store.

``DeviceOnboardingService`` intentionally has a synchronous API because its
Control API routes are synchronous request handlers.  ``BootstrapSyncFacadeMixin``
owns the private event-loop thread, the transaction-local actor/device/lookup
scope, and the transaction/close plumbing that make the asyncpg adapter look
synchronous.  Schema bootstrap and runtime-role pinning stay in the store
because they are coupled to the deployment SQL.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar, cast

import asyncpg

if TYPE_CHECKING:
    from services.device_fleet.bootstrap_postgres_store import PostgresBootstrapStore

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


class BootstrapSyncFacadeMixin:
    """Own the loop runner, request scope, transaction plumbing, and close."""

    if TYPE_CHECKING:
        # Shared state owned by PostgresBootstrapStore.__init__.
        _runner: _AsyncLoopRunner
        _pool: asyncpg.Pool | None
        _initialized: bool

        async def _initialize_async(
            self,
            *,
            bootstrap_dsn: str | None,
            role_password: str | None,
        ) -> None: ...

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
            yield cast("PostgresBootstrapStore", self)
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
            yield cast("PostgresBootstrapStore", self)
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
