"""Canonical contract reuse and production-schema guards (PR-01/PR-15/PR-17).

The notification package must consume the generated ADR-0033 relationship /
binding-role contracts directly (no hand-maintained parallel enums) and its
PostgreSQL schema must never fall back to open pass-all RLS policies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRoleValue,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    RelationshipStatus as CanonicalRelationshipStatus,
)
from services.notification import domain
from services.notification.sqlite_store import DEV_TEST_ONLY as SQLITE_DEV_TEST_ONLY


def test_relationship_status_values_come_from_canonical_enum() -> None:
    canonical = {member.value for member in CanonicalRelationshipStatus}
    assert domain.RELATIONSHIP_ACTIVE == "active"
    assert {"revoked", "expired", "disputed"} <= canonical
    assert domain.INACTIVE_RELATIONSHIP_STATUSES == frozenset(
        {"revoked", "expired", "disputed"}
    )
    assert set(domain.CANCEL_REASON_BY_RELATIONSHIP_STATUS) == {
        "revoked",
        "expired",
        "disputed",
    }


def test_recipient_roles_are_a_canonical_binding_role_subset() -> None:
    from typing import get_args

    canonical = set(get_args(BindingRoleValue.__value__))  # type: ignore[attr-defined]
    assert {"guardian", "emergency_contact", "delegate"} <= canonical
    # Payer / device admin are canonical binding roles but must never be
    # notification recipients.
    assert "account_owner" in canonical
    assert "device_admin" in canonical
    assert not ({"account_owner", "device_admin"} & domain.ALL_RECIPIENT_ROLES)


def test_postgres_schema_never_uses_open_rls() -> None:
    schema = (
        Path(__file__).resolve().parents[1] / "postgres_schema.sql"
    ).read_text(encoding="utf-8")
    assert "USING (true)" not in schema
    assert "WITH CHECK (true)" not in schema
    assert "current_setting('app.notification." in schema
    assert "FORCE ROW LEVEL SECURITY" in schema


def test_postgres_schema_has_subject_owner_join_policies() -> None:
    """Fifth review: subject-scoped recipients/receipts reads must be
    allowed through a narrow intent-subject join - never by borrowing the
    worker role."""
    schema = (
        Path(__file__).resolve().parents[1] / "postgres_schema.sql"
    ).read_text(encoding="utf-8")
    recipients_section = schema[
        schema.index("notification_recipients_select") :
    ]
    assert "app.authenticated_subject" in recipients_section
    assert "app.authenticated_actor" in recipients_section
    receipts_section = schema[
        schema.index("notification_receipts_select") :
    ]
    assert "app.authenticated_subject" in receipts_section
    assert "app.authenticated_actor" in receipts_section
    # The subject-owner join must appear in the SELECT USING branch.
    assert "SELECT 1 FROM notification_intents i" in recipients_section
    assert "SELECT 1 FROM notification_intents i" in receipts_section
    # P0-E: receipt INSERT is worker+recipient-scoped only (subject API can
    # never fabricate a delivery receipt), and receipts are split by
    # command.
    assert "notification_receipts_insert" in schema
    assert "notification_receipts_update" in schema
    assert "notification_attempts_insert" in schema
    assert "notification_audit_select" in schema
    assert "notification_outbox_select" in schema


def test_sqlite_adapter_is_marked_dev_test_only() -> None:
    assert SQLITE_DEV_TEST_ONLY is True


def test_schema_has_no_guc_service_role_and_old_role() -> None:
    """P0-2: command authority comes from real roles only - the schema must
    not grant on service_role GUCs and must not reference the old single
    role; the stale broad outbox policy must be gone."""
    schema = (
        Path(__file__).resolve().parents[1] / "postgres_schema.sql"
    ).read_text(encoding="utf-8")
    assert "app.notification.service_role" not in schema
    assert "TO memoria_notification;" not in schema
    assert "notification_outbox_access" not in schema
    assert "notification_audit_access" not in schema
    assert "memoria_notification_worker_api" not in schema
    assert "memoria_notification_worker_worker" not in schema
    assert "pg_has_role(current_user, 'memoria_notification_worker'" in schema
    # Column-level grants: the API can only move state on its own rows.
    assert "GRANT UPDATE (status, cancelled_reason, cancelled_at, updated_at)" in schema
    assert "ON notification_recipients TO memoria_notification_api;" in schema
    # The delivery-internals grant is worker-only: the API must never touch
    # attempts/leases/fencing on its own.
    assert "GRANT UPDATE (relationship_status, channel_index, status, attempts," in schema
    assert "ON notification_recipients TO memoria_notification_worker;" in schema
    assert "ON notification_recipients TO memoria_notification_api" not in schema.split(
        "GRANT UPDATE (relationship_status", 1
    )[1].split("ON notification_recipients TO memoria_notification_worker;")[0]
    assert "GRANT SELECT ON notification_outbox" in schema
    assert "ON notification_outbox\n            TO memoria_notification_worker;" in schema
    assert "GRANT SELECT ON notification_audit_events" in schema
    assert "GRANT INSERT ON notification_receipts" in schema
    # Outbox rows are subject-scoped; the API insert branch requires the
    # row's subject to equal the current subject context.
    assert "subject_person_id = current_setting(" in schema


def test_role_gates_reject_wrong_adapter_role() -> None:
    """P0-2 addendum: the API adapter refuses worker commands before any SQL
    and the worker adapter refuses to impersonate the subject API path."""
    from services.notification.postgres_store import PostgresNotificationStore

    api = PostgresNotificationStore(
        "postgresql://memoria_notification_api@localhost/x"
    )
    worker = PostgresNotificationStore(
        "postgresql://memoria_notification_worker@localhost/x"
    )
    api.with_role("api")
    worker.with_role("worker")
    with pytest.raises(RuntimeError, match="worker database role"):
        api._require_worker()  # noqa: SLF001
    with pytest.raises(RuntimeError, match="subject-API only"):
        worker._require_api()  # noqa: SLF001
    # The gates fire before any SQL: awaiting a worker method on the API
    # adapter raises the same error.
    import asyncio

    async def _probe() -> None:
        with pytest.raises(RuntimeError, match="worker database role"):
            await api.claim_recipient("r", object(), __import__("datetime").datetime.now())  # type: ignore[arg-type]

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_probe())
    finally:
        # Explicit close: never leave an unclosed event loop behind that a
        # ``-W error`` full-suite run would surface as a ResourceWarning.
        loop.close()
