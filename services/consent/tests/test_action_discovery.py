"""Action-time consent discovery: schema contract and Policy-port tests.

Covers the ``consent_discover_action_fence`` SQL seam (executor-only role,
identity/subject authority, canonical pairs, head locking, minimal canonical
returns) and the ``production_wiring`` adapter that turns the discovery
payload into Policy evidence ports without a live DSN.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import asyncpg
import pytest
from services.consent.evidence import (
    BindingEvidence,
    ConsentEvidence,
    ConsentParams,
    ConsentSnapshot,
)
from services.consent.tests.schema_static import (
    SchemaDef,
    grants_for,
    parse_schema,
    privilege_reference_errors,
)
from services.policy.action_authorizer import (
    ActionAuthorizationError,
    ActionExecutionRequest,
)
from services.policy.production_wiring import (
    ConsentDiscovery,
    ConsentEvidenceAdapter,
    ConsentSnapshotEvidenceAdapter,
    PostgresCurrentConsentAuthorityAdapter,
)
from services.policy.tests.fakes import make_context

NOW = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def schema() -> SchemaDef:
    sql = (Path(__file__).resolve().parents[1] / "postgres_schema.sql").read_text()
    return parse_schema(sql)


@pytest.fixture(scope="module")
def schema_sql() -> str:
    return (Path(__file__).resolve().parents[1] / "postgres_schema.sql").read_text()


def _evidence(
    *,
    consent_id: str = "consent-1",
    version: int = 1,
    status: str = "active",
    subject_id: str = "person-child",
    resource_owner_id: str = "person-child",
    actor_id: str = "person-guardian",
    device_id: str | None = "device-1",
    capability: str = "memory_capture",
    purpose: str = "memory_capture",
) -> ConsentEvidence:
    return ConsentEvidence(
        consent_id=consent_id,
        version=version,
        snapshot_id="historical-snap-1",
        status=cast(Any, status),
        subject_id=subject_id,
        resource_owner_id=resource_owner_id,
        actor_id=actor_id,
        actor_kind="guardian",
        device_id=device_id,
        binding_id="binding-1",
        binding_version=1,
        capability=cast(Any, capability),
        purpose=cast(Any, purpose),
        policy_version="multi-subject-v2",
        evidence_id="evidence-1",
        offer_id="offer-1",
        idempotency_key="idem-1",
        params=ConsentParams(),
        valid_from=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(hours=1),
        supersedes_consent_id=None,
        superseded_by_consent_id=None,
    )


def _binding(*, status: str = "active") -> BindingEvidence:
    return BindingEvidence(
        binding_id="binding-1",
        version=1,
        device_id="device-1",
        status=cast(Any, status),
        declared_mode="family_shared",
        valid_from=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(hours=1),
    )


def _snapshot(
    *grants: ConsentEvidence,
    binding: BindingEvidence | None = None,
) -> ConsentSnapshot:
    return ConsentSnapshot(
        snapshot_id="current-snap-7",
        version=7,
        subject_id="person-child",
        binding_id="binding-1",
        binding_version=1,
        policy_version="multi-subject-v2",
        created_at=NOW - timedelta(minutes=30),
        grants=grants,
        relationships=(),
        binding=binding or _binding(),
    )


class _FakeConnection:
    """Minimal asyncpg-shaped connection for adapter tests."""

    def __init__(
        self,
        *,
        row: dict[str, object] | None,
        in_transaction: bool = True,
        error: Exception | None = None,
        transaction_id: int = 42,
        advisory_lock_held: bool = True,
    ) -> None:
        self._row = row
        self._in_transaction = in_transaction
        self._error = error
        self._transaction_id = transaction_id
        self._advisory_lock_held = advisory_lock_held
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def is_in_transaction(self) -> bool:
        return self._in_transaction

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        self.calls.append((query, args))
        if self._error is not None:
            raise self._error
        return self._row

    async def fetchval(self, query: str, *args: object) -> object:
        self.calls.append((query, args))
        if "pg_advisory_xact_lock" in query:
            return None
        if "pg_catalog.pg_locks" in query:
            return self._advisory_lock_held
        return self._transaction_id


# ---------------------------------------------------------------------------
# SQL schema contract
# ---------------------------------------------------------------------------


def test_discovery_function_exists_with_exact_executor_signature(
    schema: SchemaDef,
) -> None:
    assert "consent_discover_action_fence" in schema.functions
    function = schema.functions["consent_discover_action_fence"]
    assert re.match(
        r"p_actor_id\s+text\s*,\s*p_subject_id\s+text\s*,"
        r"\s*p_resource_owner_id\s+text\s*,\s*p_device_id\s+text\s*,"
        r"\s*p_binding_id\s+text\s*,\s*p_binding_version\s+integer\s*,"
        r"\s*p_capability\s+text\s*,\s*p_purpose\s+text\s*,"
        r"\s*p_now\s+timestamptz",
        function.arguments,
        re.I,
    )
    header = function.header.lower()
    assert "security definer" in header
    assert "row_security = on" in header
    assert "search_path = pg_catalog, public" in header
    assert "consent_json" in function.header.lower()
    assert "snapshot_json" in function.header.lower()


def test_discovery_function_pins_session_role_and_locks_heads(
    schema: SchemaDef,
) -> None:
    body = schema.functions["consent_discover_action_fence"].body.lower()
    assert "session_user <> 'memoria_action_executor'" in body
    assert "identity_binding_visible(p_actor_id, p_binding_id)" in body
    assert "identity_binding_visible(p_subject_id, p_binding_id)" in body
    assert (
        "identity_relationship_active(\n                p_actor_id, p_subject_id,"
        in body
    )
    assert "p_resource_owner_id is distinct from p_subject_id" in body
    assert "authenticated action context mismatch" in body
    assert "authenticated binding mismatch" in body
    assert "canonical capability/purpose mismatch" in body
    assert "when 'voice_clone_use' then 'voice_clone'" in body
    assert "when 'crisis_notification' then 'crisis_response'" in body
    assert "head.actor_id in (p_actor_id, p_subject_id)" in body
    assert "evidence_json ->> 'resource_owner_id'" in body
    assert "evidence_json ->> 'device_id'" in body
    assert body.count("for update of head") == 2
    assert "consent_evidence_head" in body
    assert "consent_snapshot_head" in body
    assert "matched_consents || evidence_row.evidence_json" in body


def test_discovery_grant_is_executor_only_and_public_revoked(
    schema: SchemaDef,
) -> None:
    executor_grants = grants_for(schema, "memoria_action_executor")
    discovery_grants = tuple(
        grant
        for grant in executor_grants
        if grant.object_type == "FUNCTION"
        and grant.target == "consent_discover_action_fence"
    )
    assert len(discovery_grants) == 1
    assert discovery_grants[0].privileges == frozenset({"EXECUTE"})
    public_revokes = tuple(
        grant
        for grant in schema.privileges
        if grant.action == "REVOKE"
        and grant.target == "consent_discover_action_fence"
        and "public" in grant.roles
    )
    assert len(public_revokes) == 1
    assert not any(
        grant.target == "consent_discover_action_fence"
        and grant.action == "GRANT"
        and role in {"memoria_consent", "memoria_policy_projector", "memoria_consent_maintenance"}
        for grant in schema.privileges
        for role in grant.roles
    )


def test_discovery_role_is_bootstrapped_like_session_runtime(schema: SchemaDef) -> None:
    role = schema.roles["memoria_action_executor"]
    assert "login" in role.attributes
    assert "nosuperuser" in role.attributes
    assert "nobypassrls" in role.attributes


def test_identity_discovery_grants_are_conditional_and_parser_safe(
    schema: SchemaDef, schema_sql: str
) -> None:
    # A literal GRANT on the Identity-owned function would be a static
    # reference error, so the grant must use format() inside a DO block.
    assert privilege_reference_errors(schema) == ()
    assert "identity_binding_visible(text, text)" in schema_sql
    assert (
        "identity_relationship_active(text, text, text, timestamptz)"
        in schema_sql
    )
    assert "to_regprocedure(" in schema_sql
    assert "'memoria_consent_owner'" in schema_sql
    assert "format(" in schema_sql


# ---------------------------------------------------------------------------
# Policy port adapters
# ---------------------------------------------------------------------------


def test_evidence_adapter_projects_exact_port_fields() -> None:
    evidence = _evidence()
    adapter = ConsentEvidenceAdapter(evidence)
    assert adapter.consent_id == evidence.consent_id
    assert adapter.version == evidence.version
    assert adapter.snapshot_id == evidence.snapshot_id
    assert adapter.status == evidence.status
    assert adapter.subject_id == evidence.subject_id
    assert adapter.resource_owner_id == evidence.resource_owner_id
    assert adapter.actor_id == evidence.actor_id
    assert adapter.actor_kind == evidence.actor_kind
    assert adapter.device_id == evidence.device_id
    assert adapter.binding_id == evidence.binding_id
    assert adapter.binding_version == evidence.binding_version
    assert adapter.capability == evidence.capability
    assert adapter.purpose == evidence.purpose
    assert adapter.policy_version == evidence.policy_version
    assert adapter.evidence_id == evidence.evidence_id
    assert adapter.offer_id == evidence.offer_id
    assert adapter.idempotency_key == evidence.idempotency_key
    assert adapter.params is evidence.params
    assert adapter.valid_from == evidence.valid_from
    assert adapter.valid_until == evidence.valid_until
    assert adapter.canonical_hash == evidence.canonical_hash
    assert adapter.is_effective_at(NOW) is True
    assert adapter.is_effective_at(NOW + timedelta(days=1)) is False
    assert adapter.matches("person-child", "binding-1", 1, "memory_capture") is True
    assert adapter.matches("person-child", "binding-1", 1, "memory_recall") is False


def test_snapshot_adapter_maps_revision_grants_and_currency() -> None:
    grant_a = _evidence(consent_id="consent-a", version=2)
    grant_b = _evidence(consent_id="consent-b", version=1)
    adapter = ConsentSnapshotEvidenceAdapter(_snapshot(grant_a, grant_b))
    assert adapter.snapshot_id == "current-snap-7"
    assert adapter.revision == 7
    assert adapter.canonical_hash == _snapshot(grant_a, grant_b).canonical_hash
    assert adapter.status == "current"
    assert adapter.subject_id == "person-child"
    assert adapter.binding_id == "binding-1"
    assert adapter.binding_version == 1
    assert adapter.grant_refs == tuple(
        sorted(
            (
                (grant_b.consent_id, grant_b.version, grant_b.canonical_hash),
                (grant_a.consent_id, grant_a.version, grant_a.canonical_hash),
            )
        )
    )
    assert adapter.valid_from == _binding().valid_from
    assert adapter.valid_until == _binding().valid_until
    assert adapter.is_current_at(NOW) is True
    assert adapter.is_current_at(NOW + timedelta(days=1)) is False


def test_snapshot_adapter_contains_requires_exact_evidence_identity() -> None:
    grant = _evidence(consent_id="consent-a", version=2)
    adapter = ConsentSnapshotEvidenceAdapter(_snapshot(grant))
    assert adapter.contains(ConsentEvidenceAdapter(grant)) is True
    assert adapter.contains(ConsentEvidenceAdapter(_evidence(consent_id="other"))) is False
    tampered = _evidence(consent_id="consent-a", version=2)
    object.__setattr__(tampered, "canonical_hash", "f" * 64)
    assert adapter.contains(ConsentEvidenceAdapter(tampered)) is False
    foreign = _evidence(consent_id="consent-a", version=2, subject_id="other-person")
    assert adapter.contains(ConsentEvidenceAdapter(foreign)) is False


def test_snapshot_adapter_status_reflects_revoked_binding() -> None:
    revoked_binding = _binding(status="revoked")
    adapter = ConsentSnapshotEvidenceAdapter(
        _snapshot(_evidence(), binding=revoked_binding)
    )
    assert adapter.status == "revoked"
    assert adapter.is_current_at(NOW) is False


def test_consent_discovery_accepts_empty_state() -> None:
    discovery = ConsentDiscovery(
        consent_evidence=(),
        consent_snapshot_evidence=(),
    )
    assert discovery.consent_evidence == ()
    assert discovery.consent_snapshot_evidence == ()


# ---------------------------------------------------------------------------
# discover_current adapter
# ---------------------------------------------------------------------------


def _discovery_row(
    *,
    evidence: tuple[ConsentEvidence, ...] = (),
    snapshot: ConsentSnapshot | None = None,
) -> dict[str, object]:
    return {
        "consent_json": json.dumps(
            [item.to_canonical_dict() for item in evidence],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "snapshot_json": (
            json.dumps(snapshot.to_canonical_dict(), sort_keys=True, separators=(",", ":"))
            if snapshot is not None
            else None
        ),
    }


@pytest.mark.asyncio
async def test_discover_current_decodes_canonical_evidence_and_snapshot() -> None:
    grant = _evidence()
    snapshot = _snapshot(grant)
    connection = _FakeConnection(row=_discovery_row(evidence=(grant,), snapshot=snapshot))
    adapter = PostgresCurrentConsentAuthorityAdapter()

    discovery = await adapter.discover_current(
        cast(asyncpg.Connection, cast(Any, connection)),
        actor_id="person-guardian",
        subject_id="person-child",
        resource_owner_id="person-child",
        device_id="device-1",
        binding_id="binding-1",
        binding_version=1,
        capability="memory_capture",
        purpose="memory_capture",
        now=NOW,
    )

    assert len(discovery.consent_evidence) == 1
    assert len(discovery.consent_snapshot_evidence) == 1
    evidence_port = discovery.consent_evidence[0]
    snapshot_port = discovery.consent_snapshot_evidence[0]
    assert evidence_port.consent_id == "consent-1"
    assert evidence_port.capability == "memory_capture"
    assert snapshot_port.snapshot_id == "current-snap-7"
    assert snapshot_port.revision == 7
    assert snapshot_port.contains(evidence_port) is True
    query, args = connection.calls[0]
    assert "consent_discover_action_fence" in query
    assert args == (
        "person-guardian",
        "person-child",
        "person-child",
        "device-1",
        "binding-1",
        1,
        "memory_capture",
        "memory_capture",
        NOW,
    )


@pytest.mark.asyncio
async def test_discovery_proof_rejects_released_transaction_locks() -> None:
    grant = _evidence()
    snapshot = _snapshot(grant)
    connection = _FakeConnection(
        row=_discovery_row(evidence=(grant,), snapshot=snapshot)
    )
    adapter = PostgresCurrentConsentAuthorityAdapter()
    discovery = await adapter.discover_current(
        cast(asyncpg.Connection, cast(Any, connection)),
        actor_id="person-guardian",
        subject_id="person-child",
        resource_owner_id="person-child",
        device_id="device-1",
        binding_id="binding-1",
        binding_version=1,
        capability="memory_capture",
        purpose="memory_capture",
        now=NOW,
    )
    connection._advisory_lock_held = False
    context = make_context(
        actor_id="person-guardian",
        subject_id="person-child",
        resource_owner_id="person-child",
        device_id="device-1",
        binding_id="binding-1",
        binding_version=1,
        capability="memory_capture",
        purpose="memory_capture",
        consent_evidence=discovery.consent_evidence,
        consent_snapshot_evidence=discovery.consent_snapshot_evidence,
        evaluated_at=NOW,
    )

    with pytest.raises(ActionAuthorizationError, match="locks are no longer held"):
        await adapter._reuse_discovery_lock(
            cast(asyncpg.Connection, cast(Any, connection)),
            ActionExecutionRequest(
                receipt_id="receipt-1",
                context=context,
                now=NOW,
                consent_authority_proof=discovery.authority_proof,
            ),
        )


@pytest.mark.asyncio
async def test_discover_current_returns_snapshot_when_no_consent() -> None:
    snapshot = _snapshot()
    connection = _FakeConnection(row=_discovery_row(snapshot=snapshot))
    adapter = PostgresCurrentConsentAuthorityAdapter()

    discovery = await adapter.discover_current(
        cast(asyncpg.Connection, cast(Any, connection)),
        actor_id="person-guardian",
        subject_id="person-child",
        resource_owner_id="person-child",
        device_id="device-1",
        binding_id="binding-1",
        binding_version=1,
        capability="memory_capture",
        purpose="memory_capture",
        now=NOW,
    )

    assert discovery.consent_evidence == ()
    assert len(discovery.consent_snapshot_evidence) == 1
    assert discovery.consent_snapshot_evidence[0].snapshot_id == "current-snap-7"


@pytest.mark.asyncio
async def test_discover_current_returns_empty_snapshot_evidence_when_missing() -> None:
    grant = _evidence()
    connection = _FakeConnection(row=_discovery_row(evidence=(grant,)))
    adapter = PostgresCurrentConsentAuthorityAdapter()

    discovery = await adapter.discover_current(
        cast(asyncpg.Connection, cast(Any, connection)),
        actor_id="person-guardian",
        subject_id="person-child",
        resource_owner_id="person-child",
        device_id="device-1",
        binding_id="binding-1",
        binding_version=1,
        capability="memory_capture",
        purpose="memory_capture",
        now=NOW,
    )

    assert len(discovery.consent_evidence) == 1
    assert discovery.consent_snapshot_evidence == ()


@pytest.mark.asyncio
async def test_discover_current_rejects_malformed_canonical_payloads() -> None:
    adapter = PostgresCurrentConsentAuthorityAdapter()
    malformed_rows = (
        {"consent_json": '[{"unknown_key": true}]', "snapshot_json": None},
        {"consent_json": "[1, 2]", "snapshot_json": None},
        {"consent_json": "not-json", "snapshot_json": None},
        {"consent_json": "[]", "snapshot_json": '{"snapshot_id": "x"}'},
    )
    for row in malformed_rows:
        connection = _FakeConnection(row=row)
        with pytest.raises(ValueError):
            await adapter.discover_current(
                cast(asyncpg.Connection, cast(Any, connection)),
                actor_id="person-guardian",
                subject_id="person-child",
                resource_owner_id="person-child",
                device_id="device-1",
                binding_id="binding-1",
                binding_version=1,
                capability="memory_capture",
                purpose="memory_capture",
                now=NOW,
            )


@pytest.mark.asyncio
async def test_discover_current_rejects_tampered_evidence_hash() -> None:
    grant = _evidence()
    payload = grant.to_canonical_dict()
    payload["canonical_hash"] = "f" * 64
    connection = _FakeConnection(
        row={
            "consent_json": json.dumps([payload], sort_keys=True, separators=(",", ":")),
            "snapshot_json": None,
        }
    )
    with pytest.raises(ValueError, match="canonical_hash"):
        await PostgresCurrentConsentAuthorityAdapter().discover_current(
            cast(asyncpg.Connection, cast(Any, connection)),
            actor_id="person-guardian",
            subject_id="person-child",
            resource_owner_id="person-child",
            device_id="device-1",
            binding_id="binding-1",
            binding_version=1,
            capability="memory_capture",
            purpose="memory_capture",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_discover_current_requires_open_caller_transaction() -> None:
    connection = _FakeConnection(row=None, in_transaction=False)
    with pytest.raises(ActionAuthorizationError, match="open caller transaction"):
        await PostgresCurrentConsentAuthorityAdapter().discover_current(
            cast(asyncpg.Connection, cast(Any, connection)),
            actor_id="person-guardian",
            subject_id="person-child",
            resource_owner_id="person-child",
            device_id="device-1",
            binding_id="binding-1",
            binding_version=1,
            capability="memory_capture",
            purpose="memory_capture",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_discover_current_fails_closed_on_postgres_error() -> None:
    connection = _FakeConnection(
        row=None,
        error=asyncpg.exceptions.InsufficientPrivilegeError("denied"),
    )
    with pytest.raises(ActionAuthorizationError, match="denied"):
        await PostgresCurrentConsentAuthorityAdapter().discover_current(
            cast(asyncpg.Connection, cast(Any, connection)),
            actor_id="person-guardian",
            subject_id="person-child",
            resource_owner_id="person-child",
            device_id="device-1",
            binding_id="binding-1",
            binding_version=1,
            capability="memory_capture",
            purpose="memory_capture",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_discover_current_rejects_naive_now() -> None:
    connection = _FakeConnection(row=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        await PostgresCurrentConsentAuthorityAdapter().discover_current(
            cast(asyncpg.Connection, cast(Any, connection)),
            actor_id="person-guardian",
            subject_id="person-child",
            resource_owner_id="person-child",
            device_id="device-1",
            binding_id="binding-1",
            binding_version=1,
            capability="memory_capture",
            purpose="memory_capture",
            now=datetime(2026, 8, 11, 9, 0),
        )
