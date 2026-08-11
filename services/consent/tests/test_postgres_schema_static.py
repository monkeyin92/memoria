"""No-DSN PostgreSQL schema consistency and least-privilege contract tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from services.consent.tests.schema_static import (
    SchemaDef,
    grants_for,
    parse_schema,
    policy_column_errors,
    privilege_reference_errors,
)


@pytest.fixture(scope="module")
def schema_sql() -> str:
    return (Path(__file__).resolve().parents[1] / "postgres_schema.sql").read_text()


@pytest.fixture(scope="module")
def schema(schema_sql: str) -> SchemaDef:
    return parse_schema(schema_sql)


def test_policy_columns_and_privilege_targets_are_defined(schema: SchemaDef) -> None:
    assert policy_column_errors(schema) == ()
    assert privilege_reference_errors(schema) == ()


def test_validator_catches_missing_policy_column_and_unknown_grant_targets() -> None:
    missing_column = parse_schema(
        """
        CREATE TABLE consent_example (subject_id TEXT NOT NULL);
        CREATE ROLE memoria_example LOGIN NOSUPERUSER NOBYPASSRLS;
        CREATE POLICY example_policy ON consent_example
            TO memoria_example USING (actor_id = subject_id);
        """
    )
    assert policy_column_errors(missing_column) == (
        "policy example_policy: actor_id is not a column of consent_example",
    )

    unknown_grant = parse_schema(
        """
        CREATE TABLE consent_example (subject_id TEXT NOT NULL);
        CREATE ROLE memoria_example LOGIN NOSUPERUSER NOBYPASSRLS;
        GRANT SELECT ON consent_missing TO memoria_missing;
        """
    )
    assert privilege_reference_errors(unknown_grant) == (
        "GRANT references unknown table consent_missing",
        "GRANT references unknown role memoria_missing",
    )


def test_parser_retains_table_check_contracts(schema: SchemaDef) -> None:
    assert any(
        "db_role IN" in check and "'memoria_policy_projector'" in check
        for check in schema.tables["consent_authorization"].checks
    )
    assert any(
        "status IN ('pending', 'processing', 'delivered', 'dead_lettered'" in check
        for check in schema.tables["consent_outbox"].checks
    )


def test_consent_offer_is_append_only_versioned_and_hash_checked(
    schema: SchemaDef,
) -> None:
    offer = schema.tables["consent_offer"]
    assert {
        "offer_id",
        "version",
        "status",
        "capability",
        "actor_id",
        "subject_id",
        "resource_owner_id",
        "purpose",
        "params_json",
        "policy_version",
        "valid_from",
        "valid_until",
        "issued_at",
        "issuer",
        "canonical_hash",
        "supersedes_offer_id",
    } <= offer.columns
    assert any("jsonb_typeof(params_json" in check for check in offer.checks)
    assert any("canonical_hash ~ '^[a-f0-9]{64}$'" in check for check in offer.checks)
    assert any("'runtime_sensitive_action'" in check for check in offer.checks)


def test_consent_evidence_actor_id_is_a_real_indexed_column(
    schema: SchemaDef, schema_sql: str
) -> None:
    assert "actor_id" in schema.tables["consent_evidence"].columns
    active_index = re.search(
        r"CREATE\s+INDEX\s+[^;]+ON\s+consent_evidence\s*\((.*?)\)\s*;",
        schema_sql,
        re.I | re.S,
    )
    assert active_index is not None
    assert "actor_id" in active_index.group(1)


def test_binding_acceptance_snapshot_is_append_only_and_not_a_capability_grant(
    schema: SchemaDef,
) -> None:
    snapshot = schema.tables["binding_consent_snapshot"]
    assert {
        "snapshot_id",
        "binding_id",
        "binding_version",
        "actor_person_id",
        "account_owner_person_id",
        "device_id",
        "declared_mode",
        "catalog_version",
        "canonical_hash",
        "snapshot_json",
        "issued_at",
    } <= snapshot.columns
    assert "capability" not in snapshot.columns
    assert "grant" not in snapshot.columns
    assert any("jsonb_typeof(snapshot_json" in check for check in snapshot.checks)
    assert any("canonical_hash ~ '^[a-f0-9]{64}$'" in check for check in snapshot.checks)


def test_roles_are_non_superuser_non_bypassrls(schema: SchemaDef) -> None:
    expected = {
        "memoria_consent_owner",
        "memoria_consent",
        "memoria_policy_projector",
        "memoria_consent_outbox",
        "memoria_consent_audit",
        "memoria_consent_maintenance",
    }
    assert expected <= set(schema.roles)
    for role in expected:
        assert "nosuperuser" in schema.roles[role].attributes
        assert "nobypassrls" in schema.roles[role].attributes


def test_every_consent_table_forces_rls_including_authorization_mapping(
    schema: SchemaDef,
) -> None:
    assert "consent_authorization" in schema.tables
    expected = {
        table
        for table in schema.tables
        if table.startswith("consent_") or table == "binding_consent_snapshot"
    }
    assert expected == schema.force_rls_tables


def test_current_head_tables_are_narrow_mutable_linearization_points(
    schema: SchemaDef,
) -> None:
    assert {
        "subject_id",
        "binding_id",
        "binding_version",
        "capability",
        "purpose",
        "current_consent_id",
        "current_revision",
        "current_hash",
    } <= schema.tables["consent_evidence_head"].columns
    assert {
        "subject_id",
        "binding_id",
        "binding_version",
        "current_snapshot_id",
        "current_revision",
        "current_hash",
    } <= schema.tables["consent_snapshot_head"].columns
    assert {
        "offer_id",
        "current_version",
        "current_hash",
        "current_status",
    } <= schema.tables["consent_offer_head"].columns


def test_head_lock_functions_share_the_adapter_lock_protocol(schema: SchemaDef) -> None:
    expected_functions = {
        "consent_lock_authority_head",
        "consent_lock_snapshot_head",
        "consent_ensure_authority_head",
        "consent_advance_authority_head",
        "consent_ensure_snapshot_head",
        "consent_advance_snapshot_head",
        "consent_ensure_offer_head",
        "consent_advance_offer_head",
    }
    assert expected_functions <= set(schema.functions)
    for name in expected_functions:
        function = schema.functions[name]
        assert "security definer" in function.header.lower()
        assert "for update" in function.body.lower() or name.startswith("consent_advance_")
    for name in {
        "consent_ensure_authority_head",
        "consent_ensure_snapshot_head",
        "consent_ensure_offer_head",
    }:
        body = schema.functions[name].body.lower()
        assert "on conflict do nothing" in body


def test_lock_functions_separate_request_actor_from_evidence_actor(schema: SchemaDef) -> None:
    authority = schema.functions["consent_lock_authority_head"]
    snapshot = schema.functions["consent_lock_snapshot_head"]
    assert re.match(
        r"p_request_actor_id\s+text\s*,\s*p_evidence_actor_id\s+text\b",
        authority.arguments,
        re.I,
    )
    authority_body = authority.body.lower()
    assert "head.actor_id = p_evidence_actor_id" in authority_body
    assert "authz.db_role = session_user" in authority_body
    assert "authz.actor_id = p_request_actor_id" in authority_body
    assert "authz.subject_id = p_subject_id" in authority_body
    assert "authz.binding_id = p_binding_id" in authority_body

    assert re.match(r"p_request_actor_id\s+text\b", snapshot.arguments, re.I)
    snapshot_body = snapshot.body.lower()
    assert "head.actor_id" not in snapshot_body
    assert "authz.db_role = session_user" in snapshot_body
    assert "authz.actor_id = p_request_actor_id" in snapshot_body
    assert "authz.subject_id = p_subject_id" in snapshot_body
    assert "authz.binding_id = p_binding_id" in snapshot_body
    assert "evidence_json" in authority.header.lower()
    assert "snapshot_json" in snapshot.header.lower()


def test_evidence_head_is_actor_specific_but_snapshot_head_is_global(
    schema: SchemaDef, schema_sql: str
) -> None:
    assert "actor_id" in schema.tables["consent_evidence_head"].columns
    assert "actor_id" not in schema.tables["consent_snapshot_head"].columns
    expected_primary_keys = {
        "consent_evidence_head": (
            "actor_id",
            "subject_id",
            "binding_id",
            "binding_version",
            "capability",
            "purpose",
        ),
        "consent_snapshot_head": ("subject_id", "binding_id", "binding_version"),
    }
    for table, expected_columns in expected_primary_keys.items():
        match = re.search(
            rf"CREATE\s+TABLE.*?{table}\s*\((.*?)\n\);",
            schema_sql,
            re.I | re.S,
        )
        assert match is not None
        primary_key = re.search(r"PRIMARY\s+KEY\s*\((.*?)\)", match.group(1), re.I | re.S)
        assert primary_key is not None
        actual = tuple(item.strip() for item in primary_key.group(1).split(","))
        assert actual == expected_columns


def test_offer_check_contains_every_generated_memory_purpose(schema: SchemaDef) -> None:
    purpose_checks = " ".join(schema.tables["consent_offer"].checks)
    for purpose in (
        "memory_capture",
        "memory_promotion",
        "family_shared_memory_proposal",
        "family_shared_memory_approval",
        "family_shared_memory_promotion",
    ):
        assert f"'{purpose}'" in purpose_checks


def test_schema_has_no_forgeable_consent_guc(
    schema: SchemaDef, schema_sql: str
) -> None:
    lowered = schema_sql.lower()
    assert "app.consent_" not in lowered
    assert "set_config" not in lowered
    # Consent never writes GUCs and never reads a consent-owned GUC.  The
    # action-time discovery port is the single exception: it reads the
    # Session executor's transaction-local authenticated context, which is
    # set by the Session action transaction and pinned by the session_user
    # check inside the function.
    for name, function in schema.functions.items():
        function_text = f"{function.header}\n{function.body}".lower()
        if name == "consent_discover_action_fence":
            assert "current_setting" in function_text
            assert "app.authenticated_actor" in function_text
            assert "app.authenticated_device" in function_text
            assert "app.authenticated_binding" in function_text
        else:
            assert "current_setting" not in function_text


def test_authorization_mapping_is_not_writable_by_api_worker_or_auditor(
    schema: SchemaDef,
) -> None:
    for role in (
        "memoria_consent",
        "memoria_consent_outbox",
        "memoria_consent_audit",
    ):
        grants = grants_for(schema, role)
        mapping_grants = [grant for grant in grants if grant.target == "consent_authorization"]
        assert all(grant.privileges <= {"SELECT"} for grant in mapping_grants), (
            f"{role} can mutate consent_authorization"
        )
    authorize_grants = [
        grant
        for grant in grants_for(schema, "memoria_consent_owner")
        if grant.object_type == "FUNCTION" and grant.target == "consent_authorize"
    ]
    assert len(authorize_grants) == 1
    assert authorize_grants[0].privileges == {"EXECUTE"}


def test_api_worker_auditor_exact_privilege_matrix(schema: SchemaDef) -> None:
    api_table_grants = {
        (grant.target, privilege)
        for grant in grants_for(schema, "memoria_consent")
        if grant.object_type == "TABLE"
        for privilege in grant.privileges
    }
    assert api_table_grants == {
        ("consent_authorization", "SELECT"),
        ("consent_offer", "SELECT"),
        ("consent_offer", "INSERT"),
        ("consent_evidence", "SELECT"),
        ("consent_evidence", "INSERT"),
        ("consent_snapshot", "SELECT"),
        ("consent_snapshot", "INSERT"),
        ("binding_consent_snapshot", "SELECT"),
        ("binding_consent_snapshot", "INSERT"),
        ("consent_idempotency", "SELECT"),
        ("consent_idempotency", "INSERT"),
        ("consent_audit", "INSERT"),
        ("consent_outbox", "INSERT"),
    }

    worker_table_grants = [
        grant
        for grant in grants_for(schema, "memoria_consent_outbox")
        if grant.object_type == "TABLE"
    ]
    assert worker_table_grants == []

    projector_table_grants = {
        (grant.target, privilege)
        for grant in grants_for(schema, "memoria_policy_projector")
        if grant.object_type == "TABLE"
        for privilege in grant.privileges
    }
    assert projector_table_grants == set()
    projector_functions = {
        grant.target
        for grant in grants_for(schema, "memoria_policy_projector")
        if grant.object_type == "FUNCTION" and grant.privileges == {"EXECUTE"}
    }
    assert projector_functions == {
        "consent_lock_authority_head",
        "consent_lock_snapshot_head",
    }
    worker_functions = {
        grant.target
        for grant in grants_for(schema, "memoria_consent_outbox")
        if grant.object_type == "FUNCTION" and grant.privileges == {"EXECUTE"}
    }
    assert worker_functions == {"consent_claim_outbox", "consent_complete_outbox"}

    auditor_grants = grants_for(schema, "memoria_consent_audit")
    assert [(grant.object_type, grant.target, grant.privileges) for grant in auditor_grants] == [
        ("TABLE", "consent_audit", frozenset({"SELECT"}))
    ]

    forbidden = {"UPDATE", "DELETE", "TRUNCATE"}
    assert not [
        grant
        for grant in schema.privileges
        if grant.action == "GRANT" and grant.object_type == "TABLE" and grant.privileges & forbidden
    ]


def test_every_policy_has_an_explicit_non_permissive_boundary(schema: SchemaDef) -> None:
    for policy in schema.policies:
        expression = policy.expression.lower()
        guarded = (
            "consent_authorization" in expression
            or policy.command == "INSERT"
            or "current_user = 'memoria_consent_audit'" in expression
            or "current_user = 'memoria_consent_owner'" in expression
            or "current_user = 'memoria_consent'" in expression
            or policy.table == "consent_authorization"
        )
        assert guarded, f"policy {policy.name} lacks an explicit authorization boundary"
        assert not re.search(r"\b(?:using|with\s+check)\s*\(\s*true\s*\)", expression)


def test_outbox_worker_uses_only_monotonic_security_definer_functions(
    schema: SchemaDef,
) -> None:
    claim = schema.functions["consent_claim_outbox"]
    complete = schema.functions["consent_complete_outbox"]
    for function in (claim, complete):
        assert "security definer" in function.header.lower()
        assert "memoria_consent_outbox" in function.body

    claim_body = claim.body.lower()
    assert "status = 'pending'" in claim_body
    assert "status = 'processing'" in claim_body
    assert "for update skip locked" in claim_body

    complete_body = complete.body.lower()
    assert "status = 'processing'" in complete_body
    assert {"delivered", "dead_lettered"} <= set(
        re.findall(r"'(delivered|dead_lettered)'", complete_body)
    )
    assert "pending" not in complete_body
    update = re.search(
        r"update\s+(?:public\.)?consent_outbox(?:\s+as\s+[a-z_][a-z0-9_]*)?"
        r"\s+set\s+(.*?)\s+where\b",
        complete_body,
        re.S,
    )
    assert update is not None
    assigned_columns = set(re.findall(r"\b([a-z_][a-z0-9_]*)\s*=", update.group(1)))
    assert assigned_columns == {"status"}

    # Worker has no direct path to mutate immutable event content.
    immutable = {
        "event_id",
        "aggregate_type",
        "aggregate_id",
        "version",
        "subject_id",
        "binding_id",
        "actor_id",
        "payload_json",
        "created_at",
        "idempotency_key",
    }
    for function in (claim, complete):
        update_fragments = re.findall(
            r"update\s+(?:public\.)?consent_outbox(?:\s+as\s+[a-z_][a-z0-9_]*)?"
            r"\s+set\s+(.*?)\s+(?:from|where)\b",
            function.body.lower(),
            re.S,
        )
        for fragment in update_fragments:
            assert not immutable & set(re.findall(r"\b([a-z_][a-z0-9_]*)\s*=", fragment))


def test_worker_privileges_are_revoked_before_function_execute_grants(schema_sql: str) -> None:
    assert re.search(
        r"REVOKE\s+ALL\s+ON\s+(?:TABLE\s+)?consent_outbox\s+FROM\s+"
        r"memoria_consent_outbox\s*;",
        schema_sql,
        re.I,
    )
