"""Small, dependency-free PostgreSQL schema contract parser for consent tests.

This is deliberately narrower than a general SQL parser: it understands the
DDL forms used by ``postgres_schema.sql`` (CREATE TABLE / ROLE / POLICY /
FUNCTION and GRANT / REVOKE).  Its output lets static tests validate policy
column references and least-privilege grants without a live PostgreSQL DSN.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TableDef:
    name: str
    columns: frozenset[str]
    checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoleDef:
    name: str
    attributes: frozenset[str]


@dataclass(frozen=True, slots=True)
class PolicyDef:
    name: str
    table: str
    command: str
    roles: tuple[str, ...]
    expression: str


@dataclass(frozen=True, slots=True)
class PrivilegeDef:
    statement: str
    action: str
    privileges: frozenset[str]
    object_type: str
    target: str
    roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FunctionDef:
    name: str
    arguments: str
    header: str
    body: str


@dataclass(frozen=True, slots=True)
class SchemaDef:
    tables: dict[str, TableDef]
    roles: dict[str, RoleDef]
    policies: tuple[PolicyDef, ...]
    privileges: tuple[PrivilegeDef, ...]
    functions: dict[str, FunctionDef]
    force_rls_tables: frozenset[str]


_IDENTIFIER = r"[a-z_][a-z0-9_]*"
_TABLE_CONSTRAINT_PREFIXES = {
    "check",
    "constraint",
    "exclude",
    "foreign",
    "primary",
    "unique",
}
_SQL_KEYWORDS = {
    "all",
    "and",
    "as",
    "asc",
    "by",
    "case",
    "current_user",
    "desc",
    "else",
    "end",
    "exists",
    "false",
    "for",
    "from",
    "in",
    "insert",
    "into",
    "is",
    "limit",
    "not",
    "null",
    "on",
    "or",
    "order",
    "returning",
    "select",
    "session_user",
    "then",
    "to",
    "true",
    "update",
    "using",
    "when",
    "where",
    "with",
}


def _strip_line_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _balanced_fragment(sql: str, opening_index: int) -> tuple[str, int]:
    depth = 0
    quote: str | None = None
    index = opening_index
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return sql[opening_index + 1 : index], index + 1
        index += 1
    raise ValueError("unbalanced SQL parentheses")


def _top_level_parts(value: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    for index, char in enumerate(value):
        if quote is not None:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return tuple(part for part in parts if part)


def _parse_tables(sql: str) -> dict[str, TableDef]:
    tables: dict[str, TableDef] = {}
    pattern = re.compile(rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_IDENTIFIER})\s*\(", re.I)
    for match in pattern.finditer(sql):
        body, _ = _balanced_fragment(sql, match.end() - 1)
        columns: set[str] = set()
        checks: list[str] = []
        for part in _top_level_parts(body):
            first = re.match(rf"({_IDENTIFIER})", part, re.I)
            if first is None:
                continue
            token = first.group(1).lower()
            if token not in _TABLE_CONSTRAINT_PREFIXES:
                columns.add(token)
            checks.extend(
                fragment.group(1).strip()
                for fragment in re.finditer(r"CHECK\s*\((.*?)\)", part, re.I | re.S)
            )
        name = match.group(1).lower()
        tables[name] = TableDef(name, frozenset(columns), tuple(checks))
    return tables


def _parse_roles(sql: str) -> dict[str, RoleDef]:
    roles: dict[str, RoleDef] = {}
    for match in re.finditer(rf"CREATE\s+ROLE\s+({_IDENTIFIER})\s+([^;]+);", sql, re.I):
        name = match.group(1).lower()
        attributes = frozenset(re.findall(_IDENTIFIER, match.group(2).lower()))
        roles[name] = RoleDef(name, attributes)
    return roles


def _parse_policies(sql: str) -> tuple[PolicyDef, ...]:
    policies: list[PolicyDef] = []
    pattern = re.compile(
        rf"CREATE\s+POLICY\s+({_IDENTIFIER})\s+ON\s+({_IDENTIFIER})(.*?);",
        re.I | re.S,
    )
    for match in pattern.finditer(sql):
        body = match.group(3)
        command_match = re.search(r"\bFOR\s+(ALL|SELECT|INSERT|UPDATE|DELETE)\b", body, re.I)
        roles_match = re.search(r"\bTO\s+([^\s(]+(?:\s*,\s*[^\s(]+)*)", body, re.I)
        roles = ()
        if roles_match is not None:
            roles = tuple(role.strip().lower() for role in roles_match.group(1).split(","))
        policies.append(
            PolicyDef(
                name=match.group(1).lower(),
                table=match.group(2).lower(),
                command=command_match.group(1).upper() if command_match else "ALL",
                roles=roles,
                expression=body.strip(),
            )
        )
    return tuple(policies)


def _parse_privileges(sql: str) -> tuple[PrivilegeDef, ...]:
    privileges: list[PrivilegeDef] = []
    pattern = re.compile(
        rf"\b(GRANT|REVOKE)\s+(.+?)\s+ON\s+"
        rf"(?:(TABLE|FUNCTION)\s+)?({_IDENTIFIER}(?:\s*\([^;]*?\))?)\s+"
        rf"(?:TO|FROM)\s+({_IDENTIFIER}(?:\s*,\s*{_IDENTIFIER})*)\s*;",
        re.I | re.S,
    )
    for match in pattern.finditer(sql):
        raw_privileges = match.group(2).upper()
        parsed = frozenset(
            item.strip().split("(", 1)[0] for item in _top_level_parts(raw_privileges)
        )
        target = re.match(_IDENTIFIER, match.group(4), re.I)
        assert target is not None
        privileges.append(
            PrivilegeDef(
                statement=match.group(0).strip(),
                action=match.group(1).upper(),
                privileges=parsed,
                object_type=(match.group(3) or "TABLE").upper(),
                target=target.group(0).lower(),
                roles=tuple(role.strip().lower() for role in match.group(5).split(",")),
            )
        )
    return tuple(privileges)


def _parse_functions(sql: str) -> dict[str, FunctionDef]:
    functions: dict[str, FunctionDef] = {}
    pattern = re.compile(
        rf"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+({_IDENTIFIER})\s*\((.*?)\)"
        r"(.*?)AS\s+(\$[a-z_]*\$)(.*?)\4\s*;",
        re.I | re.S,
    )
    for match in pattern.finditer(sql):
        name = match.group(1).lower()
        functions[name] = FunctionDef(
            name=name,
            arguments=match.group(2).strip(),
            header=match.group(3).strip(),
            body=match.group(5).strip(),
        )
    return functions


def parse_schema(sql: str) -> SchemaDef:
    """Parse the consent schema into testable DDL contracts."""
    clean = _strip_line_comments(sql)
    force_rls = frozenset(
        match.group(1).lower()
        for match in re.finditer(
            rf"ALTER\s+TABLE\s+({_IDENTIFIER})\s+FORCE\s+ROW\s+LEVEL\s+SECURITY\s*;",
            clean,
            re.I,
        )
    )
    return SchemaDef(
        tables=_parse_tables(clean),
        roles=_parse_roles(clean),
        policies=_parse_policies(clean),
        privileges=_parse_privileges(clean),
        functions=_parse_functions(clean),
        force_rls_tables=force_rls,
    )


def policy_column_errors(schema: SchemaDef) -> tuple[str, ...]:
    """Return policy references to missing target/mapping columns."""
    errors: list[str] = []
    for policy in schema.policies:
        target = schema.tables.get(policy.table)
        if target is None:
            errors.append(f"policy {policy.name}: unknown target table {policy.table}")
            continue
        aliases = {policy.table: policy.table}
        for match in re.finditer(
            rf"\b(?:FROM|JOIN)\s+({_IDENTIFIER})(?:\s+(?:AS\s+)?({_IDENTIFIER}))?",
            policy.expression,
            re.I,
        ):
            table_name = match.group(1).lower()
            alias = (match.group(2) or table_name).lower()
            aliases[alias] = table_name
        for qualifier, column in re.findall(
            rf"\b({_IDENTIFIER})\.({_IDENTIFIER})\b", policy.expression, re.I
        ):
            table_name = aliases.get(qualifier.lower())
            if table_name is None:
                continue
            table = schema.tables.get(table_name)
            if table is None or column.lower() not in table.columns:
                errors.append(f"policy {policy.name}: {qualifier}.{column} is not a defined column")

        scrubbed = re.sub(r"'(?:''|[^'])*'", "", policy.expression)
        scrubbed = re.sub(rf"\b{_IDENTIFIER}\s*\.\s*{_IDENTIFIER}\b", "", scrubbed)
        function_names = {
            match.group(1).lower()
            for match in re.finditer(rf"\b({_IDENTIFIER})\s*\(", scrubbed, re.I)
        }
        aliases_and_tables = set(aliases) | set(aliases.values())
        role_names = set(policy.roles)
        for token in re.findall(rf"\b({_IDENTIFIER})\b", scrubbed, re.I):
            identifier = token.lower()
            if (
                identifier in _SQL_KEYWORDS
                or identifier in function_names
                or identifier in aliases_and_tables
                or identifier in role_names
                or identifier in {"policy", "row"}
            ):
                continue
            if identifier not in target.columns:
                errors.append(
                    f"policy {policy.name}: {identifier} is not a column of {policy.table}"
                )
    return tuple(dict.fromkeys(errors))


def privilege_reference_errors(schema: SchemaDef) -> tuple[str, ...]:
    """Return GRANT/REVOKE references to missing tables/functions/roles."""
    errors: list[str] = []
    for privilege in schema.privileges:
        targets = schema.tables if privilege.object_type == "TABLE" else schema.functions
        if privilege.target not in targets:
            errors.append(
                f"{privilege.action} references unknown {privilege.object_type.lower()} "
                f"{privilege.target}"
            )
        for role in privilege.roles:
            if role not in schema.roles and role != "public":
                errors.append(f"{privilege.action} references unknown role {role}")
    return tuple(errors)


def grants_for(schema: SchemaDef, role: str) -> tuple[PrivilegeDef, ...]:
    return tuple(
        privilege
        for privilege in schema.privileges
        if privilege.action == "GRANT" and role in privilege.roles
    )


__all__ = [
    "FunctionDef",
    "PolicyDef",
    "PrivilegeDef",
    "RoleDef",
    "SchemaDef",
    "TableDef",
    "grants_for",
    "parse_schema",
    "policy_column_errors",
    "privilege_reference_errors",
]
