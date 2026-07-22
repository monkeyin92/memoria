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
from services.persona.rules import PersonaCandidate


class _EmptyExtractor:
    version = "empty-test-extractor"

    async def extract(self, _text: str, _evidence: PersonaEvidence) -> tuple[()]:
        return ()


class _CapturingExtractor:
    version = "capturing-test-extractor"

    def __init__(self, candidate: PersonaCandidate | None = None) -> None:
        self.candidate = candidate
        self.evidence: list[PersonaEvidence] = []

    async def extract(
        self,
        _text: str,
        evidence: PersonaEvidence,
    ) -> tuple[PersonaCandidate, ...]:
        self.evidence.append(evidence)
        return (self.candidate,) if self.candidate is not None else ()


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
        "event_id": "revoked-during-extraction",
        "event_type": "speech.utterance_finalized",
        "speaker_class": "owner",
        "source": "test",
        "payload": {
            "text": "没有可提取特征",
            "persona_eligible": True,
            "owner_projection_eligible": True,
        },
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
@pytest.mark.parametrize("owner_projection_eligible", (None, False))
async def test_postgres_owner_learning_fails_closed_without_projection_eligibility(
    owner_projection_eligible: bool | None,
) -> None:
    payload: dict[str, object] = {
        "text": "我觉得先把事实弄清楚。",
        "persona_eligible": True,
    }
    if owner_projection_eligible is not None:
        payload["owner_projection_eligible"] = owner_projection_eligible
    connection = _FakeConnection()
    connection.fetchrow.return_value = {
        "event_id": "owner-projection-ineligible",
        "event_type": "speech.utterance_finalized",
        "speaker_class": "owner",
        "source": "test",
        "payload": payload,
        "occurred_at": datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
    }
    engine = PostgresPersonaEngine("postgresql://test/test", extractor=_EmptyExtractor())
    engine._pool = _FakePool(connection)

    result = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="owner-projection-ineligible",
            learning_allowed=True,
        )
    )

    assert result.accepted is False
    assert result.reason == "owner_projection_ineligible"
    connection.fetchval.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt_kind", "expected_factor", "updates_style_stats"),
    (
        ("spontaneous", 1.0, True),
        ("open", 0.7, False),
        ("leading", 0.35, False),
    ),
)
async def test_postgres_prompt_weight_scales_evidence_and_gates_style_stats(
    monkeypatch: pytest.MonkeyPatch,
    prompt_kind: str,
    expected_factor: float,
    updates_style_stats: bool,
) -> None:
    extractor = _CapturingExtractor()
    connection = _FakeConnection()
    connection.fetchrow.return_value = {
        "event_id": f"prompt-{prompt_kind}",
        "event_type": "speech.utterance_finalized",
        "speaker_class": "owner",
        "source": "test",
        "payload": {
            "text": "我觉得先把事实弄清楚。",
            "persona_eligible": True,
            "owner_projection_eligible": True,
            "interaction_mode": "companion",
            "prompt_kind": prompt_kind,
        },
        "occurred_at": datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
    }
    connection.fetchval.side_effect = (None, None, 1, None)
    engine = PostgresPersonaEngine("postgresql://test/test", extractor=extractor)
    engine._pool = _FakePool(connection)
    update_style_stats = AsyncMock()
    monkeypatch.setattr(engine, "_update_style_stats", update_style_stats)

    result = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id=f"prompt-{prompt_kind}",
            learning_allowed=True,
        )
    )

    assert result.accepted is True
    assert extractor.evidence[0].quality_score == pytest.approx(expected_factor)
    assert update_style_stats.await_count == int(updates_style_stats)


@pytest.mark.asyncio
async def test_postgres_nonexclusive_auto_promotion_uses_weighted_owner_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = PersonaCandidate(
        category="verbal_tic",
        normalized_key="我觉得",
        description="表达观点时常用“我觉得”自然起句",
        context="conversation",
    )
    extractor = _CapturingExtractor(candidate)
    connection = _FakeConnection()
    connection.fetchrow.side_effect = (
        {
            "event_id": "weighted-open-owner",
            "event_type": "speech.utterance_finalized",
            "speaker_class": "owner",
            "source": "test",
            "payload": {
                "text": "我觉得先把事实弄清楚。",
                "persona_eligible": True,
                "owner_projection_eligible": True,
                "interaction_mode": "companion",
                "prompt_kind": "open",
            },
            "occurred_at": datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
        },
        None,
        {
            "total_count": 3,
            "total_weight": 2.1,
            "owner_weight": 2.1,
            "uncertain_weight": 0,
            "uncertain_session_count": 0,
            "uncertain_profile_count": 0,
        },
        {"status": "candidate"},
    )
    connection.fetchval.side_effect = (None, None, 1, None, 1)
    engine = PostgresPersonaEngine("postgresql://test/test", extractor=extractor)
    engine._pool = _FakePool(connection)
    monkeypatch.setattr(engine, "_update_style_stats", AsyncMock())

    result = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="weighted-open-owner",
            learning_allowed=True,
        )
    )

    trait_update = next(
        call
        for call in connection.execute.await_args_list
        if "SET observation_count" in call.args[0]
    )
    assert result.published_version_id is None
    assert trait_update.args[1] == 3
    assert trait_update.args[2] == pytest.approx(0.45 + 0.13 * 2.1)
    assert trait_update.args[3] == "candidate"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observation_count", "expected_changed"),
    ((3, False), (5, True)),
)
async def test_postgres_exclusive_owner_bucket_uses_persisted_weights(
    observation_count: int,
    expected_changed: bool,
) -> None:
    trait_id = "00000000-0000-0000-0000-000000000001"
    connection = _FakeConnection()
    connection.fetch.return_value = [
        {
            "trait_id": trait_id,
            "status": "candidate",
            "review_event_id": None,
            "updated_at": "2026-07-21T12:00:00+00:00",
            "speaker_class": "owner",
            "session_id": f"owner-session-{index}",
            "payload": {},
            "weight": 0.7,
        }
        for index in range(observation_count)
    ]

    changed = await PostgresPersonaEngine._reconcile_exclusive_category(
        connection,
        account_id="persona-account",
        category="sentence_length",
    )

    query = connection.fetch.await_args.args[0]
    assert "pe.weight" in query
    assert changed is expected_changed
    assert connection.execute.await_count == int(expected_changed)


@pytest.mark.asyncio
async def test_postgres_exclusive_uncertain_bucket_uses_persisted_weights() -> None:
    connection = _FakeConnection()
    connection.fetch.return_value = [
        {
            "trait_id": "00000000-0000-0000-0000-000000000001",
            "status": "candidate",
            "review_event_id": None,
            "updated_at": "2026-07-21T12:00:00+00:00",
            "speaker_class": "uncertain",
            "session_id": f"shadow-session-{index // 2}",
            "payload": {
                "persona_eligible": True,
                "speaker_reason_code": "shadow_owner_candidate",
                "speaker_profile_id": "persona-shadow-profile",
                "speaker_quality_score": 0.9,
                "speaker_model_version": "campplus-test",
                "speaker_template_version": 1,
            },
            "weight": 0.35,
        }
        for index in range(6)
    ]

    changed = await PostgresPersonaEngine._reconcile_exclusive_category(
        connection,
        account_id="persona-account",
        category="sentence_length",
        uncertain_profile_id="persona-shadow-profile",
    )

    assert changed is False
    connection.execute.assert_not_awaited()


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
                payload={
                    "text": text,
                    "persona_eligible": True,
                    "owner_projection_eligible": True,
                },
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
                "owner_projection_eligible": True,
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
