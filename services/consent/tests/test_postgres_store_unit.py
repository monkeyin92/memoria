"""No-DSN adapter tests for PostgreSQL SQL boundaries."""

from __future__ import annotations

from typing import Any

import pytest
from services.consent.postgres_store import PostgresConsentStore
from services.consent.tests.test_evidence import base_evidence, base_offer


class _FakeTransaction:
    def __init__(self) -> None:
        self.started = False
        self.committed = False
        self.rolled_back = False

    async def start(self) -> None:
        self.started = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeConnection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.fetches: list[tuple[str, tuple[object, ...]]] = []
        self.fetchrows: list[tuple[str, tuple[object, ...]]] = []
        self.tx = _FakeTransaction()
        self.fetchrow_result: Any = None
        self.fetchrow_results: dict[str, Any] = {}

    def transaction(self) -> _FakeTransaction:
        return self.tx

    async def execute(self, sql: str, *args: object) -> str:
        self.executions.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args: object) -> list[Any]:
        self.fetches.append((sql, args))
        return []

    async def fetchrow(self, sql: str, *args: object) -> Any:
        self.fetchrows.append((sql, args))
        for fragment, result in self.fetchrow_results.items():
            if fragment in sql:
                return result
        return self.fetchrow_result


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self.conn = conn
        self.released: list[_FakeConnection] = []

    async def acquire(self) -> _FakeConnection:
        return self.conn

    async def release(self, conn: _FakeConnection) -> None:
        self.released.append(conn)


def _store_with_fake_pool() -> tuple[PostgresConsentStore, _FakeConnection]:
    store = PostgresConsentStore("postgresql://unused")
    conn = _FakeConnection()
    store._pool = _FakePool(conn)  # type: ignore[assignment]
    return store, conn


@pytest.mark.asyncio
async def test_transaction_has_no_caller_forgeable_guc_setup() -> None:
    store, conn = _store_with_fake_pool()
    uow = await store.transaction()
    assert conn.tx.started
    assert not [sql for sql, _args in conn.executions if "set_config" in sql.lower()]
    await uow.rollback()


@pytest.mark.asyncio
async def test_evidence_insert_persists_actor_id_as_a_real_column() -> None:
    store, conn = _store_with_fake_pool()
    uow = await store.transaction()
    evidence = base_evidence()
    conn.fetchrow_results["consent_ensure_authority_head"] = {
        "current_revision": 0,
        "current_hash": None,
    }
    await uow.append_consent(evidence)
    sql, args = conn.executions[-1]
    normalized = " ".join(sql.split()).lower()
    assert "consent_id, version, actor_id, subject_id" in normalized
    assert args[2] == evidence.actor_id
    await uow.rollback()


@pytest.mark.asyncio
async def test_offer_insert_resolves_unforgeable_authorized_binding() -> None:
    store, conn = _store_with_fake_pool()
    conn.fetchrow_results["consent_ensure_offer_head"] = {
        "current_version": 0,
        "current_hash": None,
    }
    conn.fetchrow_results["FROM consent_authorization"] = {"binding_id": "bd_authorized"}
    conn.fetchrow_results["consent_advance_offer_head"] = {"current_version": 1}
    uow = await store.transaction()
    offer = base_offer(offer_id="offer-pg-unit")
    await uow.append_offer(offer)

    insert_sql, insert_args = conn.executions[-1]
    assert "insert into consent_offer" in " ".join(insert_sql.lower().split())
    assert insert_args[0:2] == (offer.offer_id, offer.version)
    authz_sql, authz_args = next(
        (sql, args) for sql, args in conn.fetchrows if "FROM consent_authorization" in sql
    )
    assert "from consent_authorization" in authz_sql.lower()
    assert authz_args == (offer.actor_id, offer.subject_id)
    await uow.rollback()


@pytest.mark.asyncio
async def test_worker_claim_calls_only_security_definer_function() -> None:
    store, conn = _store_with_fake_pool()
    events = await store.outbox_pending(limit=7)
    assert events == ()
    assert len(conn.fetches) == 1
    sql, args = conn.fetches[0]
    assert "consent_claim_outbox" in sql
    assert "from consent_outbox" not in sql.lower()
    assert args == (store.worker_id, 7)
    assert conn.executions == []


@pytest.mark.asyncio
async def test_worker_completion_calls_only_security_definer_function() -> None:
    store, conn = _store_with_fake_pool()
    await store.mark_outbox_processed("event-1", "dead_lettered")
    assert len(conn.fetchrows) == 1
    sql, args = conn.fetchrows[0]
    assert "consent_complete_outbox" in sql
    assert "update consent_outbox" not in sql.lower()
    assert args == ("event-1", "dead_lettered")
    assert conn.executions == []


@pytest.mark.asyncio
async def test_worker_completion_rejects_non_terminal_target_before_sql() -> None:
    store, conn = _store_with_fake_pool()
    with pytest.raises(ValueError, match="delivered or dead_lettered"):
        await store.mark_outbox_processed("event-1", "pending")
    assert conn.fetchrows == []
    assert conn.executions == []
