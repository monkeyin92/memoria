from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from services.memory_scope.shared_actions import FamilySharedActionInput


def test_propose_input_contains_business_fields_only() -> None:
    action = FamilySharedActionInput.propose(
        title="家庭记忆",
        content="一起旅行",
        source_evidence_ids=("evidence-1",),
        co_subject_ids=("person-b",),
    )
    assert action.name == "propose"
    assert action.proposal_id is None
    assert not hasattr(action, "policy_receipt_id")
    assert not hasattr(action, "consent_snapshot_id")
    assert not hasattr(action, "family_space_id")
    assert not hasattr(action, "fence")


@pytest.mark.parametrize("name", ["confirm", "object", "withdraw"])
def test_commands_contain_only_proposal_id(
    name: Literal["confirm", "object", "withdraw"],
) -> None:
    action = FamilySharedActionInput.command(name, proposal_id="proposal-1")
    assert action.name == name
    assert action.proposal_id == "proposal-1"
    assert action.title is None
    assert action.source_evidence_ids == ()


def test_action_input_rejects_mixed_business_shapes() -> None:
    with pytest.raises(ValueError, match="accepts proposal_id only"):
        FamilySharedActionInput(
            name="confirm",
            proposal_id="proposal-1",
            title="forged business data",
        )


def test_action_input_rejects_missing_proposal_business_data() -> None:
    with pytest.raises(ValueError, match="requires title and content"):
        FamilySharedActionInput(name="propose")


def test_postgres_shared_action_schema_exposes_only_narrow_action_ports() -> None:
    schema = Path(__file__).parents[1].joinpath("postgres_schema.sql").read_text(
        encoding="utf-8"
    )

    for function_name in (
        "memory_shared_lock_membership",
        "memory_shared_lock_capture_evidence",
        "memory_shared_lock_proposal",
        "memory_shared_lock_approvals",
        "memory_shared_action_propose",
        "memory_shared_action_confirm",
        "memory_shared_action_object",
        "memory_shared_action_withdraw",
        "memory_shared_action_promote",
    ):
        assert f"FUNCTION {function_name}" in schema

    assert "GRANT SELECT, INSERT, UPDATE ON memory_shared_proposals" in schema
    assert "TO memoria_action_executor" not in schema.split(
        "-- Narrow SECURITY DEFINER sensitive-commit function", 1
    )[0]
    assert "GRANT EXECUTE ON FUNCTION memory_shared_action_propose" in schema
    assert "GRANT EXECUTE ON FUNCTION memory_shared_action_promote" in schema


def test_postgres_schema_exposes_pr12_capture_ports_with_narrow_privilege() -> None:
    schema = Path(__file__).parents[1].joinpath("postgres_schema.sql").read_text(
        encoding="utf-8"
    )

    assert (
        "CREATE OR REPLACE FUNCTION memory_capture_lock_action_resource(\n"
        "    p_capability TEXT,\n"
        "    p_action_resource_id TEXT,\n"
        "    p_subject_id TEXT,\n"
        "    p_capture_evidence_ids TEXT[],\n"
        "    p_content_sha256 TEXT,\n"
        "    p_action_revision INTEGER\n"
        ") RETURNS BOOLEAN"
    ) in schema
    assert "CREATE OR REPLACE FUNCTION memory_capture_commit(p_payload JSONB)" in schema
    assert "p_action_resource_id !~ '^capture:[a-f0-9]{64}$'" in schema
    assert "action_policy_lock_receipt(p_payload ->> 'policy_receipt_id')" in schema
    assert "'session_ephemeral', 'family_shared'" not in schema[
        schema.index("CREATE OR REPLACE FUNCTION memory_capture_commit") :
        schema.index("$memory_capture_commit$;")
    ]
    assert "item ->> 'code' = 'PERSIST_AGGREGATE_ONLY'" in schema[
        schema.index("CREATE OR REPLACE FUNCTION memory_capture_commit") :
        schema.index("$memory_capture_commit$;")
    ]
    assert "GRANT EXECUTE ON FUNCTION memory_capture_commit(JSONB)" in schema
    assert (
        "GRANT EXECUTE ON FUNCTION memory_capture_lock_action_resource(\n"
        "    TEXT, TEXT, TEXT, TEXT[], TEXT, INTEGER\n"
        ") TO memoria_action_executor"
    ) in schema
    capture_section = schema[
        schema.index("CREATE OR REPLACE FUNCTION memory_capture_lock_action_resource") :
        schema.index("-- ---------------------------------------------------------------------------\n-- Family-shared action-executor ports")
    ]
    assert "GRANT EXECUTE ON FUNCTION memory_capture_commit" in capture_section
    assert "GRANT EXECUTE ON FUNCTION memory_capture_lock_action_resource" in capture_section
    assert "TO memoria_memory_api" not in capture_section
    assert "TO memoria_memory_worker" not in capture_section
    shared_evidence_lock = schema[
        schema.index("CREATE OR REPLACE FUNCTION memory_shared_lock_capture_evidence") :
        schema.index("CREATE OR REPLACE FUNCTION memory_shared_lock_proposal")
    ]
    assert "v_evidence_id TEXT;" in shared_evidence_lock
    assert "FOREACH v_evidence_id IN ARRAY p_evidence_ids" in shared_evidence_lock
    assert "memory_capture_evidence.evidence_id = v_evidence_id" in shared_evidence_lock
    assert "    evidence_id TEXT;" not in shared_evidence_lock


def test_postgres_schema_projects_archive_candidates_only_through_session_authority() -> None:
    schema = Path(__file__).parents[1].joinpath("postgres_schema.sql").read_text(
        encoding="utf-8"
    )
    start = schema.index(
        "CREATE OR REPLACE FUNCTION memory_project_capture_evidence"
    )
    end = schema.index("$memory_project_capture_evidence$;", start)
    section = schema[start:end]

    assert "SECURITY DEFINER" in section
    assert "session_runtime_assert_action_context" in section
    assert "session_runtime_action_current_profile" in section
    assert "profile ->> 'speaker_state' IS DISTINCT FROM 'confirmed'" in section
    assert "expected_scope IS DISTINCT FROM p_payload ->> 'memory_scope'" in section
    assert "INSERT INTO memory_capture_evidence" in section
    assert (
        "GRANT EXECUTE ON FUNCTION memory_project_capture_evidence(JSONB)\n"
        "    TO memoria_action_executor;"
    ) in schema
    assert "TO memoria_memory_api" not in section
    assert "TO memoria_memory_worker" not in section


def test_shared_action_port_is_not_ready_without_a_live_postgres_authority() -> None:
    from services.memory_scope.shared_actions import (
        PostgresFamilySharedActionExecutor,
    )

    executor = PostgresFamilySharedActionExecutor(
        "postgresql://memoria_action_executor:test@localhost/memoria"
    )

    assert executor.available is False
