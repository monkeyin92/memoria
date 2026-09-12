"""Canonical contract reuse and production-schema guards (PR-01/PR-17).

The memory scope package must consume the generated ADR-0033 contracts
directly (no hand-maintained parallel enums) and its PostgreSQL schema must
never fall back to ``USING (true)`` RLS.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRole,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    MemoryScope as CanonicalMemoryScope,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyEffect as CanonicalPolicyEffect,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyObligation as CanonicalPolicyObligation,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    RelationshipStatus as CanonicalRelationshipStatus,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    SpeakerState as CanonicalSpeakerState,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    SubjectCategory as CanonicalSubjectCategory,
)
from services.memory_scope import domain
from services.memory_scope.sqlite_store import DEV_TEST_ONLY as SQLITE_DEV_TEST_ONLY


def test_memory_scope_is_the_generated_enum() -> None:
    assert domain.MemoryScope is CanonicalMemoryScope


def test_obligations_are_the_generated_enum_with_uppercase_values() -> None:
    assert domain.PolicyObligation is CanonicalPolicyObligation
    for member in CanonicalPolicyObligation:
        assert member.value == member.value.upper()


def test_related_enums_are_the_generated_enums() -> None:
    assert domain.PolicyEffect is CanonicalPolicyEffect
    assert domain.SpeakerState is CanonicalSpeakerState
    assert domain.SubjectCategory is CanonicalSubjectCategory
    assert domain.BindingRole is BindingRole
    assert domain.RelationshipStatus is CanonicalRelationshipStatus


def test_no_lowercase_obligation_parallel_contract_in_domain() -> None:
    """The domain module must not define lowercase obligation string
    constants that shadow the canonical UPPER_SNAKE vocabulary."""
    source = Path(domain.__file__).read_text(encoding="utf-8")
    for lowercase in (
        '"persist_aggregate_only"',
        '"do_not_persist"',
        '"retention_ttl"',
        '"require_speaker_confirmation"',
        '"require_subject_approval"',
        '"write_policy_receipt"',
    ):
        assert lowercase not in source, (
            f"lowercase obligation parallel contract found in domain.py: {lowercase}"
        )


def test_postgres_schema_never_uses_open_rls() -> None:
    schema = (
        Path(__file__).resolve().parents[1]
        / "postgres_schema.sql"
    ).read_text(encoding="utf-8")
    assert "USING (true)" not in schema
    assert "WITH CHECK (true)" not in schema
    assert "current_setting('app.memory." in schema
    assert "FORCE ROW LEVEL SECURITY" in schema


def test_postgres_schema_uses_a_nologin_owner_for_sensitive_objects() -> None:
    schema = (
        Path(__file__).resolve().parents[1] / "postgres_schema.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE ROLE memoria_memory_owner" in schema
    assert "ALTER ROLE memoria_memory_owner" in schema
    assert "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS" in schema
    assert "SET ROLE memoria_memory_owner" in schema
    assert "TO memoria_memory_api, memoria_memory_worker, memoria_memory_owner" in schema
    assert schema.rstrip().endswith("RESET ROLE;")


def test_postgres_schema_declares_tables_before_foreign_keys() -> None:
    """P0-1: a fresh database must initialize in one pass - every FK target
    table is declared before the FK that references it."""
    schema = (
        Path(__file__).resolve().parents[1] / "postgres_schema.sql"
    ).read_text(encoding="utf-8")
    create_proposals = schema.index(
        "CREATE TABLE IF NOT EXISTS memory_shared_proposals"
    )
    fk_position = schema.index("REFERENCES memory_shared_proposals(proposal_id)")
    assert create_proposals < fk_position, (
        "memory_shared_proposals must be created before memory_records "
        "references it (fresh-db initialization)"
    )
    create_votes = schema.index("CREATE TABLE IF NOT EXISTS memory_shared_votes")
    assert create_votes > fk_position


def test_sqlite_adapter_is_marked_dev_test_only() -> None:
    assert SQLITE_DEV_TEST_ONLY is True


def test_postgres_schema_family_policy_requires_context_for_family_rows() -> None:
    """Fifth review: a family row must never be readable without a matching
    non-empty family context; the schema must not contain the lenient
    'context IS NULL' escape for family rows."""
    schema = (
        Path(__file__).resolve().parents[1] / "postgres_schema.sql"
    ).read_text(encoding="utf-8")
    # The lenient form `OR current_setting('app.memory.family_space_id', true) IS NULL`
    # inside the AND(...) of memory_records is gone: the only IS NULL escape
    # allowed pairs family_space_id IS NULL with context IS NULL.
    assert (
        "OR current_setting('app.memory.family_space_id', true) IS NULL"
        not in schema
    )
    # Proposals (family NOT NULL) always require a matching context.
    proposals_select = schema[
        schema.index("memory_proposals_select") :
        schema.index("memory_proposals_insert")
    ]
    compact = " ".join(proposals_select.split())
    assert (
        "family_space_id = current_setting(" in compact
        and "'app.memory.family_space_id', true )" in compact
    )
    # Grant-aware read branch exists and pins owner + scope.
    assert "app.memory.grant_owner_id" in schema
    assert "app.memory.grant_scope" in schema
    # At-most-once promotion index.
    assert "uq_memory_records_shared_proposal" in schema
    assert "WHERE shared_proposal_id IS NOT NULL" in schema
    # P1: JSONB columns are type-checked at the database level.
    assert "jsonb_typeof(co_subject_ids) = 'array'" in schema
    assert "jsonb_typeof(source_evidence_ids) = 'array'" in schema
    assert "jsonb_typeof(payload) = 'object'" in schema


def test_shared_proposal_binding_version_is_required() -> None:
    """Fifth review: missing/0/null/string/bool binding versions are never
    treated as v1."""
    from services.memory_scope.domain import SharedMemoryProposal

    def _proposal(**changes: object) -> SharedMemoryProposal:
        base: dict[str, object] = {
            "proposal_id": "p1",
            "family_space_id": "family-1",
            "proposer_subject_id": "person-a",
            "co_subject_ids": ("person-b",),
            "binding_version": 1,
            "session_id": "session-1",
            "epoch": 1,
            "binding_id": "binding-1",
            "binding_role": "primary_subject",
            "runtime_profile_id": "profile-1",
            "device_id": "device-1",
            "subject_revision": 0,
            "fence_context_hash": "f" * 64,
            "title": "t",
            "content": "c",
            "proposal_revision": 1,
            "capture_evidence_hash": "c" * 64,
            "consent_snapshot_revision": 1,
            "consent_snapshot_hash": "b" * 64,
            "membership_snapshot_id": "membership:family-1:1",
            "membership_snapshot_revision": 1,
            "membership_snapshot_hash": "a" * 64,
            "consent_snapshot_id": "consent-1",
            "status": "pending",
        }
        base.update(changes)
        return SharedMemoryProposal(**base)  # type: ignore[arg-type]

    assert _proposal().binding_version == 1
    with pytest.raises(ValueError, match="binding_version"):
        _proposal(binding_version=0)
    with pytest.raises(ValueError, match="binding_version"):
        _proposal(binding_version=-1)
    with pytest.raises(ValueError, match="binding_version"):
        _proposal(binding_version="v1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="binding_version"):
        _proposal(binding_version=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="binding_version"):
        _proposal(binding_version=None)  # type: ignore[arg-type]
    # Omitting the field fails construction (no silent v1).
    with pytest.raises(TypeError):
        SharedMemoryProposal(  # type: ignore[call-arg]
            proposal_id="p2",
            family_space_id="family-1",
            proposer_subject_id="person-a",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
        )


def test_jsonb_decode_helpers_accept_str_and_list() -> None:
    """asyncpg may hand back JSONB as str or decoded list; both must decode."""
    from services.memory_scope.postgres_store import _jsonb_dict, _jsonb_list

    assert _jsonb_list('["a", "b"]') == ["a", "b"]
    assert _jsonb_list(["a", "b"]) == ["a", "b"]
    assert _jsonb_dict('{"content": "x"}') == {"content": "x"}
    assert _jsonb_dict({"content": "x"}) == {"content": "x"}
    # P1: strict decoding - non-array / non-object / non-string elements
    # are rejected instead of silently becoming character sequences.
    with pytest.raises(ValueError, match="JSON array"):
        _jsonb_list('"abc"')
    with pytest.raises(ValueError, match="JSON array"):
        _jsonb_list('{"a": 1}')
    with pytest.raises(ValueError, match="JSON array"):
        _jsonb_list("[1, 2]")
    with pytest.raises(ValueError, match="JSON object"):
        _jsonb_dict('["a"]')
    with pytest.raises(ValueError, match="JSON object"):
        _jsonb_dict("42")


def test_memory_schema_role_separation_and_no_guc_authority() -> None:
    """P0-2/P1-7: memory schema grants on real roles only; API can only
    append outbox/audit, worker selects/updates the outbox; the old broad
    policies and single role are gone."""
    schema = (
        Path(__file__).resolve().parents[1] / "postgres_schema.sql"
    ).read_text(encoding="utf-8")
    assert "app.memory.service_role" not in schema
    assert "TO memoria_memory;" not in schema
    assert "memory_outbox_access" not in schema
    assert "memory_audit_access" not in schema
    assert "memoria_memory_worker_api" not in schema
    assert "memoria_memory_worker_worker" not in schema
    assert "pg_has_role(current_user, 'memoria_memory_worker'" in schema
    assert "GRANT SELECT, UPDATE ON memory_outbox" in schema
    assert "GRANT SELECT ON memory_audit_events" in schema


def test_memory_role_gates_reject_wrong_adapter_role() -> None:
    from services.memory_scope.postgres_store import PostgresMemoryStore

    api = PostgresMemoryStore("postgresql://memoria_memory_api@localhost/x")
    worker = PostgresMemoryStore("postgresql://memoria_memory_worker@localhost/x")
    api.with_role("api")
    worker.with_role("worker")
    with pytest.raises(RuntimeError, match="worker database role"):
        api._require_worker()  # noqa: SLF001
    with pytest.raises(RuntimeError, match="worker database role"):
        api._require_worker()  # noqa: SLF001
    with pytest.raises(RuntimeError, match="subject-API only"):
        worker._require_api()  # noqa: SLF001
    # Async worker methods fire the gate before SQL.
    import asyncio

    async def _probe() -> None:
        with pytest.raises(RuntimeError, match="worker database role"):
            await api.list_pending_outbox()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_probe())
    finally:
        # Explicit close: never leave an unclosed event loop behind that a
        # ``-W error`` full-suite run would surface as a ResourceWarning.
        loop.close()
