"""PostgreSQL contract tests for the evidence-backed self model."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.self_model.domain import (
    InvalidSelfModelTransitionError,
    SelfModelIdempotencyConflictError,
    SelfModelNotFoundError,
    SourceInput,
    UntrustedSelfModelSourceError,
)
from services.self_model.postgres_registry import PostgresSelfModelRegistry


def _dsn(dsn: str, *, user: str, password: str, database: str) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"{quote(user)}:{quote(password)}@{host}", f"/{database}", parsed.query, ""))


def test_postgres_schema_forces_rls_immutable_profiles_and_audits() -> None:
    schema = Path(__file__).parents[1].joinpath("postgres_schema.sql").read_text(encoding="utf-8")

    for table in (
        "self_model_cognitive_claims",
        "self_model_decision_cases",
        "self_model_relationship_profiles",
        "self_model_audit_events",
    ):
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in schema
    assert "self_model_relationship_profile_guard" in schema
    assert "self_model_audit_immutable_guard" in schema
    assert "UNIQUE (account_id, person_id, version_number)" not in schema
    assert "DROP CONSTRAINT IF EXISTS" in schema
    assert "self_model_relationship_profi_account_id_person_id_version__key" in schema


@pytest.mark.asyncio
async def test_postgres_registry_validates_decision_and_source_commands_before_io() -> None:
    registry = PostgresSelfModelRegistry("postgresql://localhost/memoria")

    with pytest.raises(ValueError, match="options must not be empty"):
        await registry.create_decision_case(
            account_id="account",
            idempotency_key="empty-options",
            kind="real",
            context="test",
            options=(),
            constraints=(),
            chosen_option="none",
        )
    with pytest.raises(ValueError, match="chosen_option must be one of options"):
        await registry.create_decision_case(
            account_id="account",
            idempotency_key="bad-choice",
            kind="real",
            context="test",
            options=("A",),
            constraints=(),
            chosen_option="B",
        )
    with pytest.raises(ValueError, match="only supporting evidence can be adopted"):
        await registry.add_source(
            account_id="account",
            item_kind="cognitive_claim",
            item_id=str(uuid.uuid4()),
            source_event_id="event",
            relation="counterexample",
            adopted=True,
            negative=False,
            expected_version=1,
            idempotency_key="bad-adoption",
        )
    with pytest.raises(ValueError, match="duplicate source"):
        await registry.create_cognitive_claim(
            account_id="account",
            idempotency_key="duplicate-create-source",
            claim_type="belief",
            statement="duplicate",
            sources=(
                SourceInput(source_event_id="event"),
                SourceInput(source_event_id="event"),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL self model contract",
)
async def test_postgres_registry_enforces_rls_effective_policy_immutability_and_deletion() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    suffix = uuid.uuid4().hex[:10]
    database, role = f"memoria_self_model_{suffix}", f"memoria_self_model_{suffix}"
    password = f"self-model-{suffix}"
    account_a, account_b = f"self-model-a-{suffix}", f"self-model-b-{suffix}"
    source_id = f"self-model-source-{suffix}"
    counterexample_id = f"self-model-counterexample-{suffix}"
    invalid_events = (
        (f"uncertain-{suffix}", "speech.utterance_finalized", "uncertain",
         '{"owner_projection_eligible":true,"interaction_mode":"companion"}'),
        (f"guest-{suffix}", "speech.utterance_finalized", "guest",
         '{"owner_projection_eligible":true,"interaction_mode":"companion"}'),
        (f"assistant-{suffix}", "speech.utterance_finalized", "assistant",
         '{"owner_projection_eligible":true,"interaction_mode":"companion"}'),
        (f"simulated-{suffix}", "speech.utterance_finalized", "owner",
         '{"owner_projection_eligible":true,"interaction_mode":"companion","simulated_output":true}'),
        (f"ineligible-{suffix}", "speech.utterance_finalized", "owner",
         '{"owner_projection_eligible":false,"interaction_mode":"companion"}'),
        (f"preview-{suffix}", "speech.utterance_finalized", "owner",
         '{"owner_projection_eligible":true,"interaction_mode":"self_preview"}'),
        (f"wrong-type-{suffix}", "assistant.response_finalized", "owner",
         '{"owner_projection_eligible":true,"interaction_mode":"companion"}'),
    )
    admin = await asyncpg.connect(admin_dsn)
    registry: PostgresSelfModelRegistry | None = None
    try:
        await admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS")
        await admin.execute(f'CREATE DATABASE "{database}" OWNER "{role}"')
        app_dsn = _dsn(admin_dsn, user=role, password=password, database=database)
        registry = PostgresSelfModelRegistry(app_dsn)
        await registry.initialize()
        migration_connection = await asyncpg.connect(app_dsn)
        try:
            await migration_connection.execute(
                """
                ALTER TABLE self_model_relationship_profiles
                ADD CONSTRAINT
                    self_model_relationship_profi_account_id_person_id_version__key
                UNIQUE (account_id, person_id, version_number)
                """
            )
        finally:
            await migration_connection.close()
        await registry.close()
        registry = PostgresSelfModelRegistry(app_dsn)
        await registry.initialize()
        connection = await asyncpg.connect(app_dsn)
        try:
            assert await connection.fetchval(
                """
                SELECT count(*)
                FROM pg_constraint
                WHERE conrelid = 'self_model_relationship_profiles'::regclass
                  AND conname =
                      'self_model_relationship_profi_account_id_person_id_version__key'
                """
            ) == 0
            person_id, relationship_id = uuid.uuid4(), uuid.uuid4()
            second_relationship_id = uuid.uuid4()
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.account_id', $1, true)", account_a)
                for event_id in (source_id, counterexample_id):
                    await connection.execute(
                        """
                        INSERT INTO archive_evidence_events (
                            event_id, account_id, event_type, schema_version, occurred_at,
                            speaker_class, source, payload, content_sha256
                        ) VALUES ($1, $2, 'speech.utterance_finalized', 1, $3,
                                  'owner', 'self-model-test',
                                  '{"text":"owner source","owner_projection_eligible":true,"interaction_mode":"companion"}'::jsonb,
                                  $4)
                        """,
                        event_id,
                        account_a,
                        datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                        "a" * 64,
                    )
                for event_id, event_type, speaker_class, payload in invalid_events:
                    await connection.execute(
                        """
                        INSERT INTO archive_evidence_events (
                            event_id, account_id, event_type, schema_version, occurred_at,
                            speaker_class, source, payload, content_sha256
                        ) VALUES ($1, $2, $3, 1, $4, $5, 'self-model-test',
                                  $6::jsonb, $7)
                        """,
                        event_id,
                        account_a,
                        event_type,
                        datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                        speaker_class,
                        payload,
                        "b" * 64,
                    )
                await connection.execute(
                    """
                    INSERT INTO person_entities (
                        person_id, account_id, canonical_key, display_name,
                        relationship_to_owner, source_event_id, created_at
                    ) VALUES ($1, $2, 'friend:lin', '小林', 'friend', $3, $4)
                    """,
                    person_id,
                    account_a,
                    source_id,
                    datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                )
                await connection.execute(
                    """
                    INSERT INTO relationships (
                        relationship_id, account_id, person_id, relationship_type,
                        source_event_id, valid_at
                    ) VALUES ($1, $2, $3, 'colleague', $4, $5)
                    """,
                    second_relationship_id,
                    account_a,
                    person_id,
                    source_id,
                    datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                )
                await connection.execute(
                    """
                    INSERT INTO relationships (
                        relationship_id, account_id, person_id, relationship_type,
                        source_event_id, valid_at
                    ) VALUES ($1, $2, $3, 'friend', $4, $5)
                    """,
                    relationship_id,
                    account_a,
                    person_id,
                    source_id,
                    datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
                )
            assert await connection.fetchval("SELECT count(*) FROM self_model_cognitive_claims") == 0

            before_failed_create = await registry.export_account(account_a)
            with pytest.raises(UntrustedSelfModelSourceError):
                await registry.create_decision_case(
                    account_id=account_a,
                    idempotency_key="atomic-create-rollback",
                    kind="real",
                    context="第二个来源不合格",
                    options=("A",),
                    constraints=(),
                    chosen_option="A",
                    sources=(
                        SourceInput(source_event_id=source_id),
                        SourceInput(source_event_id=invalid_events[1][0]),
                    ),
                )
            after_failed_create = await registry.export_account(account_a)
            for table in (
                "self_model_decision_cases",
                "self_model_decision_case_sources",
                "self_model_audit_events",
                "self_model_command_receipts",
            ):
                assert after_failed_create[table] == before_failed_create[table]

            trust_claim = await registry.create_cognitive_claim(
                account_id=account_a,
                idempotency_key="trust-claim",
                claim_type="belief",
                statement="只接受可信 owner 来源",
                confidence=0.8,
            )
            for index, (event_id, *_rest) in enumerate(invalid_events):
                with pytest.raises(UntrustedSelfModelSourceError):
                    await registry.add_source(
                        account_id=account_a,
                        item_kind="cognitive_claim",
                        item_id=trust_claim.claim_id,
                        source_event_id=event_id,
                        relation="support",
                        adopted=True,
                        negative=False,
                        expected_version=trust_claim.version,
                        idempotency_key=f"invalid-source-{index}",
                    )

            claim = await registry.create_cognitive_claim(
                account_id=account_a,
                idempotency_key="claim-1",
                claim_type="value",
                statement="先核实事实，再做结论",
                confidence=0.9,
                sources=(SourceInput(source_event_id=source_id),),
            )
            assert claim.version == 2
            duplicate_claim = await registry.create_cognitive_claim(
                account_id=account_a,
                idempotency_key="claim-1",
                claim_type="value",
                statement="先核实事实，再做结论",
                confidence=0.9,
                sources=(SourceInput(source_event_id=source_id),),
            )
            assert duplicate_claim == claim
            with pytest.raises(SelfModelIdempotencyConflictError):
                await registry.create_cognitive_claim(
                    account_id=account_a,
                    idempotency_key="claim-1",
                    claim_type="value",
                    statement="先核实事实，再做结论",
                    confidence=0.9,
                    sources=(),
                )
            with pytest.raises(
                InvalidSelfModelTransitionError, match="step-up"
            ):
                await registry.review_cognitive_claim(
                    account_id=account_a,
                    claim_id=claim.claim_id,
                    status="confirmed",
                    expected_version=claim.version,
                    step_up_verified=False,
                    idempotency_key="claim-review-no-step-up",
                )
            with pytest.raises(
                InvalidSelfModelTransitionError, match="counterexample"
            ):
                await registry.review_cognitive_claim(
                    account_id=account_a,
                    claim_id=claim.claim_id,
                    status="confirmed",
                    expected_version=claim.version,
                    step_up_verified=True,
                    idempotency_key="claim-review-no-counterexample",
                )
            claim = await registry.add_source(
                account_id=account_a,
                item_kind="cognitive_claim",
                item_id=claim.claim_id,
                source_event_id=counterexample_id,
                relation="counterexample",
                adopted=False,
                negative=False,
                expected_version=claim.version,
                idempotency_key="claim-source-2",
            )
            reviewed = await registry.review_cognitive_claim(
                account_id=account_a,
                claim_id=claim.claim_id,
                status="confirmed",
                expected_version=claim.version,
                step_up_verified=True,
                idempotency_key="claim-review-1",
            )
            assert [item.claim_id for item in await registry.list_cognitive_claims(
                account_id=account_a, effective_only=True
            )] == [reviewed.claim_id]

            decision = await registry.create_decision_case(
                account_id=account_a,
                idempotency_key="decision-1",
                kind="hypothetical",
                context="假设搬家",
                options=("搬家", "不搬"),
                constraints=("预算",),
                chosen_option="搬家",
                sources=(SourceInput(source_event_id=source_id),),
            )
            assert decision.version == 2
            reviewed_decision = await registry.review_decision_case(
                account_id=account_a,
                case_id=decision.case_id,
                status="confirmed",
                expected_version=decision.version,
                step_up_verified=False,
                idempotency_key="decision-review-1",
            )
            with pytest.raises(InvalidSelfModelTransitionError, match="cannot transition"):
                await registry.review_decision_case(
                    account_id=account_a,
                    case_id=decision.case_id,
                    status="confirmed",
                    expected_version=reviewed_decision.version,
                    step_up_verified=False,
                    idempotency_key="decision-review-repeat-status",
                )
            assert await registry.list_decision_cases(account_id=account_a, effective_only=True) == ()

            profile = await registry.create_relationship_profile(
                account_id=account_a,
                idempotency_key="relationship-1",
                person_id=str(person_id),
                relationship_id=str(relationship_id),
                salutation="小林",
                tone="温和",
                advice_style="先倾听再建议",
                boundaries=("不分享私密经历",),
                sources=(SourceInput(source_event_id=source_id),),
            )
            assert profile.version_number == 1
            second_profile = await registry.create_relationship_profile(
                account_id=account_a,
                idempotency_key="relationship-2",
                person_id=str(person_id),
                relationship_id=str(second_relationship_id),
                salutation="林同事",
                tone="直接",
                advice_style="先给结论",
                boundaries=(),
                sources=(SourceInput(source_event_id=source_id),),
            )
            assert second_profile.person_id == profile.person_id
            assert second_profile.profile_id != profile.profile_id
            assert second_profile.relationship_id != profile.relationship_id
            profile = await registry.review_relationship_profile(
                account_id=account_a,
                profile_id=profile.profile_id,
                version_number=profile.version_number,
                status="approved",
                expected_status="candidate",
                step_up_verified=True,
                idempotency_key="profile-review-1",
            )
            assert [item.profile_id for item in await registry.list_relationship_profiles(
                account_id=account_a, effective_only=True
            )] == [profile.profile_id]
            with pytest.raises(asyncpg.PostgresError, match="immutable"):
                async with connection.transaction():
                    await connection.execute(
                        "SELECT set_config('app.account_id', $1, true)", account_a
                    )
                    await connection.execute(
                        "UPDATE self_model_relationship_profiles SET tone = '直接' WHERE profile_id = $1",
                        uuid.UUID(profile.profile_id),
                    )
            revised = await registry.revise_relationship_profile(
                account_id=account_a,
                profile_id=profile.profile_id,
                expected_version=profile.version_number,
                salutation="小林",
                tone="更直接",
                advice_style="先给结论",
                boundaries=("不分享私密经历",),
                idempotency_key="profile-revise-1",
                sharing_scope="private",
            )
            assert revised.version_number == 2
            assert (
                await registry.get_relationship_profile(
                    account_id=account_a,
                    profile_id=profile.profile_id,
                    version_number=1,
                )
            ).status == "superseded"
            with pytest.raises(SelfModelNotFoundError):
                await registry.get_cognitive_claim(account_id=account_b, claim_id=claim.claim_id)
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.account_id', $1, true)", account_a)
                assert await connection.fetchval(
                    "SELECT count(*) FROM self_model_audit_events"
                ) >= 7
                assert await connection.fetchval(
                    "SELECT count(*) FROM self_model_cognitive_claims WHERE account_id = $1",
                    account_b,
                ) == 0
            rls = await connection.fetchrow(
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_class WHERE relname = 'self_model_relationship_profiles'
                """
            )
            assert rls is not None and rls["relrowsecurity"] and rls["relforcerowsecurity"]
        finally:
            await connection.close()

        exported = await registry.export_account(account_a)
        assert len(exported["self_model_cognitive_claims"]) == 2
        assert len(exported["self_model_relationship_profiles"]) == 3
        deleted = await registry.delete_account(account_a)
        assert deleted["self_model_cognitive_claims"] == 2
        assert deleted["self_model_relationship_profiles"] == 3
        assert all(not rows for rows in (await registry.export_account(account_a)).values())
    finally:
        if registry is not None:
            await registry.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1", database
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()
