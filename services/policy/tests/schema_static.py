"""Independent static validator for ``postgres_receipt_schema.sql``.

This is intentionally a small SQL-structure checker rather than a PostgreSQL
parser. It validates the security properties that must remain visible in the
deployable schema and catches policy expressions that reference absent table
columns before a live database is available.
"""

from __future__ import annotations

import re

TABLE = "policy_receipts_v2"
ROLES = frozenset(
    {
        "memoria_policy",
        "memoria_policy_api",
        "memoria_policy_projector",
        "memoria_policy_worker",
        "memoria_policy_audit",
        "memoria_policy_maintenance",
    }
)
EXPECTED_GRANTS = {
    "memoria_policy": frozenset({"SELECT", "INSERT"}),
    "memoria_policy_api": frozenset({"SELECT", "INSERT"}),
    "memoria_policy_projector": frozenset({"SELECT", "INSERT"}),
    "memoria_policy_worker": frozenset({"SELECT"}),
    "memoria_policy_audit": frozenset({"SELECT"}),
    "memoria_policy_maintenance": frozenset({"SELECT", "INSERT"}),
}
EXPECTED_POLICIES = {
    "policy_receipts_api_select": "memoria_policy",
    "policy_receipts_api_insert": "memoria_policy",
    "policy_receipts_v2_api_select": "memoria_policy_api",
    "policy_receipts_v2_api_insert": "memoria_policy_api",
    "policy_receipts_projector_select": "memoria_policy_projector",
    "policy_receipts_projector_insert": "memoria_policy_projector",
    "policy_receipts_worker_select": "memoria_policy_worker",
    "policy_receipts_audit_select": "memoria_policy_audit",
    "policy_receipts_maintenance_select": "memoria_policy_maintenance",
    "policy_receipts_maintenance_insert": "memoria_policy_maintenance",
}
FORBIDDEN_PRIVILEGES = frozenset({"UPDATE", "DELETE", "TRUNCATE"})
_SQL_WORDS = frozenset(
    {
        "and",
        "or",
        "not",
        "null",
        "is",
        "true",
        "false",
        "current_user",
        "current_setting",
        "nullif",
        "distinct",
        "from",
    }
)


def validate_policy_receipt_schema(sql: str) -> tuple[str, ...]:
    errors: list[str] = []
    columns = _table_columns(sql)
    if not columns:
        errors.append(f"missing CREATE TABLE {TABLE}")

    normalized = " ".join(sql.split()).lower()
    required_checks = {
        "context_hash format CHECK": (
            "context_hash text not null check (context_hash ~ '^[a-f0-9]{64}$')"
        ),
        "obligations array CHECK": (
            "jsonb_typeof(obligations) = 'array'"
        ),
        "consent_snapshot_ids array CHECK": (
            "jsonb_typeof(consent_snapshot_ids) = 'array'"
        ),
        "consent_snapshot_revisions array CHECK": (
            "jsonb_typeof(consent_snapshot_revisions) = 'array'"
        ),
        "relationship_snapshot_ids array CHECK": (
            "jsonb_typeof(relationship_snapshot_ids) = 'array'"
        ),
        "relationship_snapshot_revisions array CHECK": (
            "jsonb_typeof(relationship_snapshot_revisions) = 'array'"
        ),
        "binding_version lower-bound CHECK": (
            "binding_version integer not null check (binding_version >= 1)"
        ),
        "session_epoch lower-bound CHECK": (
            "session_epoch integer not null check (session_epoch >= 1)"
        ),
        "subject_revision lower-bound CHECK": (
            "subject_revision integer not null check (subject_revision >= 0)"
        ),
        "expires_at order CHECK": (
            "expires_at timestamptz not null check (expires_at > created_at)"
        ),
    }
    for label, fragment in required_checks.items():
        if fragment not in normalized:
            errors.append(f"missing {label}")
    for enum_column in (
        "capability",
        "purpose",
        "effect",
        "device_trust",
        "data_classification",
        "safety_state",
    ):
        if f"{enum_column} text not null check" not in normalized:
            errors.append(f"missing {enum_column} enum CHECK")

    for role in sorted(ROLES):
        role_pattern = re.compile(
            rf"(?:CREATE|ALTER)\s+ROLE\s+{role}\b[^;]*\bLOGIN\b"
            rf"[^;]*\bNOSUPERUSER\b[^;]*\bNOBYPASSRLS\b",
            re.IGNORECASE | re.DOTALL,
        )
        if not role_pattern.search(sql):
            errors.append(
                f"role {role} must be LOGIN NOSUPERUSER NOBYPASSRLS"
            )

    upper = sql.upper()
    if f"ALTER TABLE {TABLE.upper()} ENABLE ROW LEVEL SECURITY" not in upper:
        errors.append("missing ENABLE ROW LEVEL SECURITY")
    if f"ALTER TABLE {TABLE.upper()} FORCE ROW LEVEL SECURITY" not in upper:
        errors.append("missing FORCE ROW LEVEL SECURITY")
    for principal in ("PUBLIC", *(role.upper() for role in sorted(ROLES))):
        revoke = (
            f"REVOKE ALL PRIVILEGES ON TABLE {TABLE.upper()} FROM {principal}"
        )
        if revoke not in upper:
            errors.append(f"missing full privilege REVOKE from {principal.lower()}")

    grants = _grants(sql)
    for table, role, privileges in grants:
        if table != TABLE:
            errors.append(f"GRANT targets unexpected table {table}")
        if role not in ROLES:
            errors.append(f"GRANT targets undeclared role {role}")
        forbidden = privileges & FORBIDDEN_PRIVILEGES
        if forbidden:
            errors.append(f"forbidden GRANT to {role}: {sorted(forbidden)}")
    for role, expected in EXPECTED_GRANTS.items():
        actual = frozenset().union(
            *(privileges for table, target, privileges in grants if target == role)
        )
        if actual != expected:
            errors.append(
                f"GRANT matrix for {role} is {sorted(actual)}, expected {sorted(expected)}"
            )

    policies = _policies(sql)
    for name, role in EXPECTED_POLICIES.items():
        statement = policies.get(name)
        if statement is None:
            errors.append(f"missing policy {name}")
            continue
        if not re.search(rf"\bTO\s+{role}\b", statement, re.IGNORECASE):
            errors.append(f"policy {name} does not target {role}")
        if f"current_user = '{role}'" not in statement.lower():
            errors.append(f"policy {name} lacks explicit current_user role condition")
        for expression in _policy_expressions(statement):
            for identifier in _identifiers(expression):
                if identifier not in columns and identifier not in _SQL_WORDS:
                    errors.append(
                        f"policy {name} references missing column {identifier}"
                    )

    return tuple(errors)


def _table_columns(sql: str) -> frozenset[str]:
    match = re.search(
        rf"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+{TABLE}\s*\(",
        sql,
        re.IGNORECASE,
    )
    if match is None:
        return frozenset()
    body = _balanced_body(sql, match.end() - 1)
    columns: set[str] = set()
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        column = re.match(r"([a-z_][a-z0-9_]*)\s+", stripped, re.IGNORECASE)
        if column and column.group(1).upper() not in {
            "CHECK",
            "CONSTRAINT",
            "PRIMARY",
            "UNIQUE",
            "FOREIGN",
        }:
            columns.add(column.group(1).lower())
    return frozenset(columns)


def _balanced_body(sql: str, open_index: int) -> str:
    depth = 0
    in_quote = False
    for index in range(open_index, len(sql)):
        char = sql[index]
        if char == "'":
            in_quote = not in_quote
        elif not in_quote and char == "(":
            depth += 1
        elif not in_quote and char == ")":
            depth -= 1
            if depth == 0:
                return sql[open_index + 1 : index]
    return ""


def _grants(sql: str) -> tuple[tuple[str, str, frozenset[str]], ...]:
    result: list[tuple[str, str, frozenset[str]]] = []
    pattern = re.compile(
        r"GRANT\s+([^;]+?)\s+ON(?:\s+TABLE)?\s+(?!FUNCTION\b)([a-z_][a-z0-9_]*)"
        r"\s+TO\s+([a-z_][a-z0-9_]*)\s*;",
        re.IGNORECASE,
    )
    for privileges, table, role in pattern.findall(sql):
        result.append(
            (
                table.lower(),
                role.lower(),
                frozenset(item.strip().upper() for item in privileges.split(",")),
            )
        )
    return tuple(result)


def _policies(sql: str) -> dict[str, str]:
    pattern = re.compile(
        rf"CREATE\s+POLICY\s+([a-z_][a-z0-9_]*)\s+ON\s+{TABLE}\b(.*?);",
        re.IGNORECASE | re.DOTALL,
    )
    return {name.lower(): statement for name, statement in pattern.findall(sql)}


def _policy_expressions(statement: str) -> tuple[str, ...]:
    expressions: list[str] = []
    for match in re.finditer(
        r"(?:USING|WITH\s+CHECK)\s*\(",
        statement,
        re.IGNORECASE | re.DOTALL,
    ):
        start = match.end() - 1
        depth = 0
        in_quote = False
        for index in range(start, len(statement)):
            char = statement[index]
            if char == "'":
                in_quote = not in_quote
            elif not in_quote and char == "(":
                depth += 1
            elif not in_quote and char == ")":
                depth -= 1
                if depth == 0:
                    expressions.append(statement[start + 1 : index])
                    break
    return tuple(expressions)


def _identifiers(expression: str) -> frozenset[str]:
    without_literals = re.sub(r"'[^']*'", "", expression)
    return frozenset(
        token.lower()
        for token in re.findall(r"\b[a-z_][a-z0-9_]*\b", without_literals, re.IGNORECASE)
    )
