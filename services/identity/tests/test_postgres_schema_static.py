"""Static contract checks for ``postgres_schema.sql`` that run without a
PostgreSQL server: function parameter references, missing FROM clauses,
open RLS rules, SECURITY DEFINER exposure and policy/function wiring.
Live PostgreSQL compilation + negative RLS tests stay in
``test_postgres_store.py`` (skipped without ``MEMORIA_TEST_POSTGRES_DSN``)."""

from __future__ import annotations

import re
from pathlib import Path

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "postgres_schema.sql"
_TABLE_NAMES = {
    "identity_persons",
    "identity_relationships",
    "identity_device_bindings",
    "identity_device_binding_roles",
    "identity_audit_events",
    "identity_outbox",
    "identity_transfer_intents",
    "identity_idempotency_records",
    "identity_persona_assignments",
    "identity_custom_personas",
}

_AUTHORITATIVE_TABLES = frozenset(_TABLE_NAMES)


def _schema() -> str:
    return _SCHEMA_PATH.read_text(encoding="utf-8")


def _functions(sql: str) -> list[tuple[str, list[str], str]]:
    """(name, parameter names, body) for every CREATE FUNCTION."""
    functions: list[tuple[str, list[str], str]] = []
    for match in re.finditer(
        r"(?:CREATE OR REPLACE FUNCTION|CREATE FUNCTION)\s+(\w+)\s*\((.*?)\)\s*"
        r"RETURNS[^;]*?AS\s*\$\$\s*(.*?)\s*\$\$;",
        sql,
        flags=re.S,
    ):
        name = match.group(1)
        params = [
            part.strip().split()[0]
            for part in match.group(2).split(",")
            if part.strip()
        ]
        functions.append((name, params, match.group(3)))
    return functions


def test_every_qualified_parameter_reference_is_declared() -> None:
    sql = _schema()
    for name, params, body in _functions(sql):
        for reference in re.findall(rf"\b{re.escape(name)}\.(\w+)", body):
            assert reference in params, (
                f"function {name} references {name}.{reference} but {reference!r} "
                "is not a declared parameter"
            )


def test_no_table_qualified_reference_without_a_table_scope() -> None:
    sql = _schema()
    for name, _params, body in _functions(sql):
        for table in _TABLE_NAMES:
            if f"{table}." in body:
                assert re.search(
                    rf"(FROM|JOIN|UPDATE|INTO|DELETE FROM)\s+{table}\b", body
                ), (
                    f"function {name} references {table}. but never scopes the "
                    "table (missing FROM/JOIN/UPDATE/INTO/DELETE)"
                )


def test_no_open_rls_rules() -> None:
    sql = _schema()
    for pattern in (r"USING\s*\(\s*true\s*\)", r"WITH\s+CHECK\s*\(\s*true\s*\)"):
        assert not re.search(pattern, sql), f"open RLS rule found: {pattern}"


def test_every_authoritative_table_enables_and_forces_rls() -> None:
    sql = _schema()
    for table in sorted(_AUTHORITATIVE_TABLES):
        assert re.search(
            rf"ALTER TABLE\s+{table}\s+ENABLE ROW LEVEL SECURITY\s*;",
            sql,
            flags=re.I,
        ), f"{table} must enable row-level security"
        assert re.search(
            rf"ALTER TABLE\s+{table}\s+FORCE ROW LEVEL SECURITY\s*;",
            sql,
            flags=re.I,
        ), f"{table} must force row-level security"


def test_security_definer_functions_are_revoked_from_public() -> None:
    sql = _schema()
    revoke_names = set(
        re.findall(
            r"REVOKE ALL ON FUNCTION\s+(\w+)\(", sql
        )
    )
    definer_names = set(
        re.findall(
            r"(?:CREATE OR REPLACE FUNCTION|CREATE FUNCTION)\s+(\w+)\b"
            r"[^;]*?SECURITY DEFINER",
            sql,
            flags=re.S,
        )
    )
    for name in definer_names:
        if name not in {"identity_actor", "identity_scope"}:
            assert name in revoke_names, (
                f"SECURITY DEFINER function {name} is missing "
                "REVOKE ALL ... FROM PUBLIC"
            )
    assert "SET row_security = off" not in sql
    assert "SET row_security = on" in sql
    # The GUC accessors are not SECURITY DEFINER but are still revoked and
    # re-granted to the roles that need them (policies evaluate them).
    assert "identity_actor" in revoke_names
    assert "identity_scope" in revoke_names


def test_policies_use_only_authority_functions() -> None:
    sql = _schema()
    policy_bodies = "".join(
        re.findall(r"CREATE POLICY\s+[^;]+;", sql, flags=re.S)
    )
    for banned in (
        "identity_relationships WHERE",
        "identity_persons WHERE",
        "identity_device_bindings WHERE",
        "identity_device_binding_roles WHERE",
        "identity_transfer_intents WHERE",
        "identity_audit_events WHERE",
    ):
        assert banned not in policy_bodies, (
            f"policy re-implements data logic inline ({banned}); "
            "use the SECURITY DEFINER authority ports"
        )
    assert "identity_audit_visible(" in policy_bodies
    assert "identity_audit_visible(" in sql


def test_audit_policy_carries_action_and_binding_manage_scope() -> None:
    sql = _schema()
    audit_function = re.search(
        r"(?:CREATE OR REPLACE FUNCTION|CREATE FUNCTION) "
        r"identity_audit_visible\b.*?\$\$;",
        sql,
        flags=re.S,
    )
    assert audit_function is not None
    audit_function = audit_function.group(0)
    assert "action text" in audit_function
    assert "binding.manage" in audit_function
    assert "LIKE 'binding.%'" in audit_function
    assert "LIKE 'transfer.%'" in audit_function
    # The policy invocation passes the action column as the last argument.
    assert re.search(
        r"identity_audit_visible\(\s*identity_actor\(\),\s*actor_person_id,"
        r"\s*person_id,\s*subject_person_id,\s*device_id,\s*action\s*\)",
        sql,
        flags=re.S,
    )


def test_transfer_policy_allows_target_accept_update() -> None:
    sql = _schema()
    assert re.search(
        r"to_account_owner_person_id = identity_actor\(\)\s*AND\s*status IN "
        r"\('accepted', 'expired', 'conflicted'\)",
        sql,
    )


def test_relationship_policy_writes_require_endpoint_or_parent() -> None:
    sql = _schema()
    assert re.search(
        r"WITH CHECK \(\s*identity_actor\(\) IS NOT NULL\s*AND\s*\("
        r"\s*source_person_id = identity_actor\(\)"
        r"\s*OR\s*target_person_id = identity_actor\(\)",
        sql,
        flags=re.S,
    )
    assert "identity_relationship_parent_managed(" in sql


def test_custom_persona_has_database_level_immutability_trigger() -> None:
    sql = _schema()
    # A BEFORE UPDATE trigger must refuse every update on the table.
    guard = re.search(
        r"CREATE OR REPLACE FUNCTION identity_custom_persona_immutable_guard\(\)"
        r".*?\$\$;",
        sql,
        flags=re.S,
    )
    assert guard is not None, "custom persona immutability guard function missing"
    assert "RAISE EXCEPTION 'identity custom persona is immutable'" in guard.group(0)
    assert re.search(
        r"CREATE TRIGGER identity_custom_persona_immutable\s+"
        r"BEFORE UPDATE ON identity_custom_personas",
        sql,
    ), "custom persona immutability trigger must fire BEFORE UPDATE"
    # DELETE is deliberately allowed: no delete guard on this table.
    assert not re.search(
        r"BEFORE DELETE ON identity_custom_personas", sql
    ), "custom personas must remain deletable"


def test_custom_persona_api_role_has_no_update_grant() -> None:
    sql = _schema()
    assert re.search(
        r"GRANT SELECT, INSERT, DELETE ON identity_custom_personas\s+TO memoria_identity",
        sql,
    ), "memoria_identity must hold SELECT/INSERT/DELETE on custom personas"
    assert not re.search(
        r"GRANT[^;]*UPDATE[^;]*ON identity_custom_personas", sql
    ), "memoria_identity must never be granted UPDATE on custom personas"


def test_persona_assignments_persona_column_is_indexed_not_foreign_keyed() -> None:
    sql = _schema()
    # Built-in ids are not custom rows: an FK would reject built-in
    # assignments, so the column is indexed and the delete path drops rows.
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS idx_identity_persona_assignments_persona\s+"
        r"ON identity_persona_assignments\(persona_id\)",
        sql,
    )
    assignments_table = re.search(
        r"CREATE TABLE IF NOT EXISTS identity_persona_assignments\s*\(.*?\);",
        sql,
        flags=re.S,
    )
    assert assignments_table is not None
    assert "REFERENCES identity_custom_personas" not in assignments_table.group(0)


def test_custom_persona_audit_trigger_is_security_definer_and_revoked() -> None:
    sql = _schema()
    audit = re.search(
        r"CREATE OR REPLACE FUNCTION identity_audit_custom_persona_event\(\)"
        r".*?\$\$;",
        sql,
        flags=re.S,
    )
    assert audit is not None
    assert "SECURITY DEFINER" in audit.group(0)
    assert "INSERT INTO identity_audit_events" in audit.group(0)
    assert re.search(
        r"AFTER INSERT OR DELETE ON identity_custom_personas", sql
    ), "custom persona audit trigger must fire on INSERT/DELETE only"
    assert (
        "REVOKE ALL ON FUNCTION identity_audit_custom_persona_event() FROM PUBLIC"
        in sql
    )
