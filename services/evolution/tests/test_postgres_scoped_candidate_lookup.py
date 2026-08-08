from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest
from services.evolution.domain import CandidateArtifact
from services.evolution.postgres_store import PostgresEvolutionStore


def _candidate(
    candidate_id: str,
    *,
    account_id: str | None,
    task_family: str,
) -> CandidateArtifact:
    now = datetime.now(UTC)
    return CandidateArtifact(
        candidate_id=candidate_id,
        task_family=task_family,
        kind="prompt",
        scope="owner_private" if account_id is not None else "global_redacted",
        account_id=account_id,
        version=1,
        payload={
            "proposal": {
                "instruction": "use the requested forecast date",
                "match_terms": ["weather"],
            }
        },
        source_signal_ids=(f"{candidate_id}-signal-a", f"{candidate_id}-signal-b"),
        expected_behavior="answer the requested forecast date",
        regression_guards=("privacy_leakage_zero", "retention"),
        risk="medium",
        trusted_root_sha256="b" * 64,
        created_at=now,
        updated_at=now,
    )


async def _cleanup(dsn: str, candidate_ids: list[str]) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute("SELECT set_config('app.evolution_account_deletion', '1', false)")
        await connection.execute(
            "DELETE FROM evolution_candidates WHERE candidate_id = ANY($1::text[])",
            candidate_ids,
        )
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL evolution contract",
)
async def test_postgres_account_scoped_candidate_query_is_fail_closed() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    store = PostgresEvolutionStore(dsn)
    suffix = str(uuid.uuid4())
    family = f"scoped-weather-{suffix}"
    candidates = (
        _candidate(f"scoped-global-{suffix}", account_id=None, task_family=family),
        _candidate(f"scoped-owner-a-{suffix}", account_id="account-a", task_family=family),
        _candidate(f"scoped-owner-b-{suffix}", account_id="account-b", task_family=family),
    )
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    try:
        for candidate in candidates:
            store.create_candidate(candidate)
        visible = store.list_candidates_for_account("account-a")
        candidate_only = store.list_candidates_for_account(
            "account-a",
            statuses=("candidate",),
            task_family=family,
        )
        assert {candidate.candidate_id for candidate in visible} == {
            candidates[0].candidate_id,
            candidates[1].candidate_id,
        }
        assert {candidate.candidate_id for candidate in candidate_only} == {
            candidates[0].candidate_id,
            candidates[1].candidate_id,
        }
        assert store.list_candidates_for_account("account-a", statuses=("stable",)) == ()
    finally:
        await _cleanup(dsn, candidate_ids)
