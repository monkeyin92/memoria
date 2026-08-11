"""Static contract for the deployable policy receipt PostgreSQL schema."""

from __future__ import annotations

from pathlib import Path

from services.policy.postgres_receipt_repository import (
    POLICY_RECEIPTS_V2_SCHEMA_SQL,
)
from services.policy.tests.schema_static import validate_policy_receipt_schema

SCHEMA_PATH = Path(__file__).parents[1] / "postgres_receipt_schema.sql"


def test_repository_ddl_is_loaded_from_the_authoritative_sql_file() -> None:
    assert SCHEMA_PATH.is_file()
    assert POLICY_RECEIPTS_V2_SCHEMA_SQL == SCHEMA_PATH.read_text(encoding="utf-8")


def test_authoritative_schema_passes_structural_security_validation() -> None:
    assert validate_policy_receipt_schema(POLICY_RECEIPTS_V2_SCHEMA_SQL) == ()


def test_static_validator_rejects_policy_reference_to_missing_column() -> None:
    broken = POLICY_RECEIPTS_V2_SCHEMA_SQL.replace(
        "current_user = 'memoria_policy'",
        "missing_column = 'memoria_policy'",
        1,
    )

    errors = validate_policy_receipt_schema(broken)

    assert any("missing_column" in error for error in errors)


def test_schema_contains_all_receipt_integrity_checks() -> None:
    normalized = " ".join(POLICY_RECEIPTS_V2_SCHEMA_SQL.split()).lower()

    expected_fragments = (
        "context_hash text not null check (context_hash ~ '^[a-f0-9]{64}$')",
        "effect text not null check (effect in ( 'deny', 'allow', 'allow_with_obligations' ))",
        "jsonb_typeof(obligations) = 'array'",
        "jsonb_typeof(consent_snapshot_ids) = 'array'",
        "jsonb_typeof(consent_snapshot_revisions) = 'array'",
        "jsonb_typeof(relationship_snapshot_ids) = 'array'",
        "jsonb_typeof(relationship_snapshot_revisions) = 'array'",
        "binding_version integer not null check (binding_version >= 1)",
        "binding_canonical_hash text check ( binding_canonical_hash is null or binding_canonical_hash ~ '^[a-f0-9]{64}$' )",
        "session_epoch integer not null check (session_epoch >= 1)",
        "subject_revision integer not null check (subject_revision >= 0)",
        "expires_at timestamptz not null check (expires_at > created_at)",
        "receipt_id text primary key check ( char_length(receipt_id) between 1 and 192 )",
        "subject_id text check ( subject_id is null or char_length(subject_id) between 1 and 128 )",
    )
    for fragment in expected_fragments:
        assert fragment in normalized

    for column in (
        "capability",
        "purpose",
        "device_trust",
        "data_classification",
        "safety_state",
    ):
        assert f"{column} text not null check ({column} in (" in normalized
    assert "policy_valid_obligations_v2(obligations)" in normalized
    assert "action_resource_fence - array[" in normalized
    assert "policy_jsonb_string_array_v2(consent_snapshot_ids)" in normalized
    assert "policy_jsonb_positive_int_array_v2(consent_snapshot_revisions)" in normalized


def test_static_validator_rejects_removed_integrity_check() -> None:
    broken = POLICY_RECEIPTS_V2_SCHEMA_SQL.replace(
        "CHECK (expires_at > created_at)",
        "",
        1,
    )

    errors = validate_policy_receipt_schema(broken)

    assert "missing expires_at order CHECK" in errors


def test_exact_fence_schema_requires_only_binding_hash() -> None:
    normalized = " ".join(POLICY_RECEIPTS_V2_SCHEMA_SQL.split()).lower()

    assert "not exact_fence or binding_canonical_hash is not null" in normalized
    assert "jsonb_array_length(consent_snapshot_ids)" not in normalized
    assert "jsonb_array_length(relationship_snapshot_ids)" not in normalized


def test_schema_declares_minimal_append_only_runtime_roles() -> None:
    normalized = " ".join(POLICY_RECEIPTS_V2_SCHEMA_SQL.split()).lower()
    for role in (
        "memoria_policy_api",
        "memoria_policy_projector",
        "memoria_policy_worker",
        "memoria_policy_audit",
        "memoria_policy_maintenance",
    ):
        assert f"create role {role} login nosuperuser nobypassrls" in normalized
        assert f"revoke all privileges on table policy_receipts_v2 from {role}" in normalized
    assert "grant update" not in normalized
    assert "grant delete" not in normalized
    assert "grant truncate" not in normalized


def test_api_rls_is_scoped_to_transaction_local_actor_and_subject() -> None:
    normalized = " ".join(POLICY_RECEIPTS_V2_SCHEMA_SQL.split()).lower()

    for _role in ("memoria_policy", "memoria_policy_api"):
        assert "nullif(current_setting('app.authenticated_actor', true), '')" in normalized
        assert (
            "actor_id = nullif( current_setting('app.authenticated_actor', true), '' )"
            in normalized
        )
        assert (
            "subject_id is not distinct from nullif( current_setting('app.authenticated_subject', true), '' )"
            in normalized
        )

    assert (
        "authenticated_subject', true), '' ) is not null"
        not in normalized
    )

    for role in (
        "memoria_policy_projector",
        "memoria_policy_worker",
        "memoria_policy_audit",
        "memoria_policy_maintenance",
    ):
        assert "create policy" in normalized
        assert f"to {role}" in normalized
        assert f"using (current_user = '{role}')" in normalized
    assert "with check (current_user = 'memoria_policy_projector')" in normalized
    assert "with check (current_user = 'memoria_policy_maintenance')" in normalized


def test_lock_function_binds_scoped_callers_and_action_bridge_context() -> None:
    normalized = " ".join(POLICY_RECEIPTS_V2_SCHEMA_SQL.split()).lower()

    assert "session_user" in normalized
    assert "current_setting('app.authenticated_actor', true)" in normalized
    assert "current_setting('app.authenticated_subject', true)" in normalized
    assert "policy receipt actor context mismatch" in normalized
    assert "policy receipt subject context mismatch" in normalized
    assert "memoria_action_executor" in normalized


def test_action_executor_never_receives_direct_receipt_table_privilege() -> None:
    normalized = " ".join(POLICY_RECEIPTS_V2_SCHEMA_SQL.split()).lower()
    assert "grant select, insert on table policy_receipts_v2 to memoria_action_executor" not in normalized
    assert "grant select on table policy_receipts_v2 to memoria_action_executor" not in normalized
