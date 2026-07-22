from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock

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


class _EmptyExtractor:
    version = "empty-test-extractor"

    async def extract(self, _text: str, _evidence: PersonaEvidence) -> tuple[()]:
        return ()


class _AsyncContext:
    def __init__(self, value: object = None) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeConnection:
    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.fetch = AsyncMock()
        self.fetchrow = AsyncMock()
        self.fetchval = AsyncMock()

    def transaction(self) -> _AsyncContext:
        return _AsyncContext()


class _FakePool:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(self._connection)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("speaker_class", "persona_eligible"),
    (
        pytest.param("owner", None, id="owner-missing"),
        pytest.param("owner", False, id="owner-false"),
        pytest.param("uncertain", None, id="uncertain-missing"),
        pytest.param("uncertain", False, id="uncertain-false"),
    ),
)
async def test_postgres_persona_learning_fails_closed_without_explicit_turn_eligibility(
    speaker_class: str,
    persona_eligible: bool | None,
) -> None:
    payload: dict[str, object] = {"text": "我觉得先把事实弄清楚。"}
    if persona_eligible is not None:
        payload["persona_eligible"] = persona_eligible
    if speaker_class == "uncertain":
        payload.update(
            {
                "speaker_reason_code": "shadow_owner_candidate",
                "speaker_profile_id": "persona-shadow-profile",
                "speaker_quality_score": 0.9,
                "speaker_model_version": "campplus-test",
                "speaker_template_version": 1,
            }
        )
    connection = _FakeConnection()
    connection.fetchrow.return_value = {
        "event_type": "speech.utterance_finalized",
        "speaker_class": speaker_class,
        "source": "test",
        "payload": payload,
        "occurred_at": datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
    }
    engine = PostgresPersonaEngine("postgresql://test/test", extractor=_EmptyExtractor())
    engine._pool = _FakePool(connection)

    result = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="ineligible-persona-turn",
            learning_allowed=True,
        )
    )

    assert result.accepted is False
    assert result.reason == "persona_ineligible_turn"
    connection.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_persona_account_lock_is_transaction_scoped_and_namespaced() -> None:
    connection = AsyncMock(spec=asyncpg.Connection)

    await PostgresPersonaEngine._lock_account(connection, "account-001")

    connection.fetchval.assert_awaited_once_with(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        "memoria-persona:account-001",
    )


@pytest.mark.asyncio
async def test_postgres_observation_rechecks_consent_in_final_write_transaction() -> None:
    connection = _FakeConnection()
    connection.fetchrow.return_value = {
        "event_type": "speech.utterance_finalized",
        "speaker_class": "owner",
        "source": "test",
        "payload": {"text": "没有可提取特征", "persona_eligible": True},
        "occurred_at": datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
    }
    connection.fetchval.side_effect = (None, None, None, None)
    engine = PostgresPersonaEngine("postgresql://test/test", extractor=_EmptyExtractor())
    engine._pool = _FakePool(connection)

    result = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="revoked-during-extraction",
            learning_allowed=True,
        )
    )

    assert result.accepted is False
    assert result.reason == "learning_not_authorized"


@pytest.mark.asyncio
async def test_postgres_consent_and_capsule_paths_share_the_account_lock() -> None:
    account_id = "persona-account"
    lock_call = (
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"memoria-persona:{account_id}",
    )

    grant_connection = _FakeConnection()
    grant_connection.fetchrow.return_value = {
        "revoked_at": None,
        "policy_version": "persona-learning-v1",
        "granted_at": datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
    }
    grant_engine = PostgresPersonaEngine("postgresql://test/test")
    grant_engine._pool = _FakePool(grant_connection)
    await grant_engine.grant_consent(
        account_id=account_id,
        policy_version="persona-learning-v1",
    )

    revoke_connection = _FakeConnection()
    revoke_connection.fetchrow.return_value = {
        "revoked_at": datetime(2026, 7, 21, 12, 1, tzinfo=UTC),
        "policy_version": "persona-learning-v1",
        "granted_at": datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
    }
    revoke_engine = PostgresPersonaEngine("postgresql://test/test")
    revoke_engine._pool = _FakePool(revoke_connection)
    await revoke_engine.revoke_consent(account_id=account_id)

    capsule_connection = _FakeConnection()
    capsule_connection.fetchrow.return_value = None
    capsule_engine = PostgresPersonaEngine("postgresql://test/test")
    capsule_engine._pool = _FakePool(capsule_connection)
    capsule = await capsule_engine.capsule(
        PersonaRequest(account_id=account_id, speaker_class="owner")
    )

    grant_connection.fetchval.assert_awaited_once_with(*lock_call)
    revoke_connection.fetchval.assert_awaited_once_with(*lock_call)
    capsule_connection.fetchval.assert_awaited_once_with(*lock_call)
    assert capsule.entries == ()


async def _cleanup(dsn: str, account_id: str) -> None:
    connection = await asyncpg.connect(dsn)
    await connection.execute(
        "DELETE FROM persona_learning_consents WHERE account_id = $1", account_id
    )
    await connection.execute("DELETE FROM persona_versions WHERE account_id = $1", account_id)
    await connection.execute("DELETE FROM persona_traits WHERE account_id = $1", account_id)
    await connection.execute("DELETE FROM speech_style_stats WHERE account_id = $1", account_id)
    await connection.execute(
        "DELETE FROM persona_observation_receipts WHERE account_id = $1", account_id
    )
    await connection.execute(
        "DELETE FROM archive_evidence_events WHERE account_id = $1", account_id
    )
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
                payload={"text": text, "persona_eligible": True},
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
            payload={
                "text": "做重大决定时，我习惯先列事实，再睡一晚。",
                "persona_eligible": True,
            },
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
    uncertain_style_capsule = await engine.capsule(
        PersonaRequest(
            account_id=account_id,
            speaker_class="uncertain",
            topic="表达看法",
            confirmed_style_only=True,
        )
    )
    revoked = await engine.revoke_consent(account_id=account_id)

    assert consent.revoked_at is None
    assert observed.published_version_id is not None
    assert "我觉得" in capsule.prompt_fragment
    assert confirmed.version_id
    assert "先列事实，再睡一晚" in decision_capsule.prompt_fragment
    assert {entry.category for entry in uncertain_style_capsule.entries} == {
        "verbal_tic",
        "sentence_length",
    }
    assert "先列事实，再睡一晚" not in uncertain_style_capsule.prompt_fragment
    assert all(not entry.context for entry in uncertain_style_capsule.entries)
    assert all(not entry.counterexample for entry in uncertain_style_capsule.entries)
    assert all(not entry.source_event_ids for entry in uncertain_style_capsule.entries)
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
