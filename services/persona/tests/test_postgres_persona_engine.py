from __future__ import annotations

import os
from datetime import UTC, datetime

import asyncpg
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.postgres_archive import PostgresLifeArchive
from services.persona.domain import (
    PersonaCounterexampleRequiredError,
    PersonaEvidence,
    PersonaRequest,
    PersonaReview,
)
from services.persona.postgres_engine import PostgresPersonaEngine


async def _cleanup(dsn: str, account_id: str) -> None:
    connection = await asyncpg.connect(dsn)
    await connection.execute("DELETE FROM persona_learning_consents WHERE account_id = $1", account_id)
    await connection.execute("DELETE FROM persona_versions WHERE account_id = $1", account_id)
    await connection.execute("DELETE FROM persona_traits WHERE account_id = $1", account_id)
    await connection.execute("DELETE FROM speech_style_stats WHERE account_id = $1", account_id)
    await connection.execute(
        "DELETE FROM persona_observation_receipts WHERE account_id = $1", account_id
    )
    await connection.execute("DELETE FROM archive_evidence_events WHERE account_id = $1", account_id)
    await connection.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL persona contract test",
)
async def test_postgres_persona_matches_versioned_public_contract_and_forces_rls() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = "postgres-persona-account"
    archive = PostgresLifeArchive(dsn)
    engine = PostgresPersonaEngine(dsn)
    await archive.initialize()
    await engine.initialize()
    await _cleanup(dsn, account_id)

    consent = await engine.grant_consent(
        account_id=account_id,
        policy_version="persona-learning-v1",
    )
    assert await engine.learning_allowed(account_id=account_id)
    for index, text in enumerate(
        (
            "我觉得先把事实弄清楚。",
            "我觉得应该先听完对方。",
            "我觉得答应的事要做到。",
        )
    ):
        event_id = f"postgres-persona-style-{index}"
        await archive.record(
            EvidenceEvent(
                event_id=event_id,
                account_id=account_id,
                event_type="speech.utterance_finalized",
                occurred_at=datetime(2026, 7, 19, 14, index, tzinfo=UTC),
                speaker_class="owner",
                source="contract-test",
                payload={"text": text},
            )
        )
        observed = await engine.observe(
            PersonaEvidence(
                account_id=account_id,
                source_event_id=event_id,
                learning_allowed=True,
            )
        )
    capsule = await engine.capsule(
        PersonaRequest(
            account_id=account_id,
            speaker_class="owner",
            topic="表达看法",
        )
    )

    decision_event = "postgres-persona-decision"
    await archive.record(
        EvidenceEvent(
            event_id=decision_event,
            account_id=account_id,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 7, 19, 14, 4, tzinfo=UTC),
            speaker_class="owner",
            source="contract-test",
            payload={"text": "做重大决定时，我习惯先列事实，再睡一晚。"},
        )
    )
    decision = await engine.observe(
        PersonaEvidence(
            account_id=account_id,
            source_event_id=decision_event,
            learning_allowed=True,
        )
    )
    with pytest.raises(PersonaCounterexampleRequiredError):
        await engine.review(
            PersonaReview(
                account_id=account_id,
                trait_id=decision.candidate_trait_ids[0],
                action="confirm",
            )
        )
    confirmed = await engine.review(
        PersonaReview(
            account_id=account_id,
            trait_id=decision.candidate_trait_ids[0],
            action="confirm",
            counterexample="紧急安全风险出现时会立即行动。",
        )
    )
    decision_capsule = await engine.capsule(
        PersonaRequest(
            account_id=account_id,
            speaker_class="owner",
            topic="重大决定",
        )
    )
    revoked = await engine.revoke_consent(account_id=account_id)

    assert consent.revoked_at is None
    assert observed.published_version_id is not None
    assert "我觉得" in capsule.prompt_fragment
    assert confirmed.version_id
    assert "先列事实，再睡一晚" in decision_capsule.prompt_fragment
    assert revoked.revoked_at is not None
    assert not await engine.learning_allowed(account_id=account_id)

    connection = await asyncpg.connect(dsn)
    rls = await connection.fetch(
        """
        SELECT relname, relrowsecurity, relforcerowsecurity
        FROM pg_class
        WHERE relname = ANY($1::text[])
        """,
        [
            "persona_traits",
            "persona_evidence",
            "speech_style_stats",
            "persona_learning_consents",
            "persona_versions",
        ],
    )
    await connection.close()
    assert len(rls) == 5
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rls)

    await _cleanup(dsn, account_id)
    await engine.close()
    await archive.close()
