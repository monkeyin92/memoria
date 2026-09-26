"""PostgreSQL Persona keyed by (account, subject), and its in-place migration."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import asyncpg
import pytest
from services.archive.domain import EvidenceEvent, EvidenceNotFoundError
from services.archive.postgres_archive import PostgresLifeArchive
from services.persona.domain import PersonaEvidence, PersonaRequest, PersonaReview
from services.persona.postgres_engine import PostgresPersonaEngine

pytestmark = pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL persona subject tests",
)

_PERSONA_DIR = Path(__file__).resolve().parents[1]
_ARCHIVE_SCHEMA = (_PERSONA_DIR.parent / "archive" / "postgres_archive_schema.sql").read_text(
    encoding="utf-8"
)
_PERSONA_SCHEMA = (_PERSONA_DIR / "postgres_schema.sql").read_text(encoding="utf-8")

HOLDER_TEXTS = (
    "我觉得先把事实弄清楚。",
    "我觉得应该先听完对方。",
    "我觉得答应的事要做到。",
)
CHILD_TEXTS = (
    "其实我今天想去公园玩。",
    "其实我更喜欢画画。",
    "其实我已经做完作业了。",
)

# The pre-subject PostgreSQL shape, copied from postgres_schema.sql before the
# migration (tables, the one-active index and RLS as production has them).
_LEGACY_SCHEMA = """
CREATE TABLE persona_traits (
    trait_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    description TEXT NOT NULL,
    context TEXT NOT NULL,
    counterexample TEXT NOT NULL DEFAULT '',
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disabled')
    ),
    observation_count INTEGER NOT NULL DEFAULT 0 CHECK (observation_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    review_event_id TEXT,
    UNIQUE (account_id, category, normalized_key)
);

CREATE TABLE persona_evidence (
    trait_id UUID NOT NULL REFERENCES persona_traits(trait_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    scene TEXT NOT NULL,
    weight DOUBLE PRECISION NOT NULL CHECK (weight BETWEEN 0 AND 1),
    occurred_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (trait_id, source_event_id)
);

CREATE TABLE persona_observation_receipts (
    source_event_id TEXT PRIMARY KEY
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE speech_style_stats (
    account_id TEXT NOT NULL,
    scene TEXT NOT NULL,
    utterance_count INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    speech_duration_ms BIGINT NOT NULL DEFAULT 0,
    pause_ratio_sum DOUBLE PRECISION NOT NULL DEFAULT 0,
    pause_sample_count INTEGER NOT NULL DEFAULT 0,
    tic_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, scene)
);

CREATE TABLE persona_learning_consents (
    account_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    grant_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id),
    revoke_event_id TEXT REFERENCES archive_evidence_events(event_id)
);

CREATE TABLE persona_versions (
    version_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    reason TEXT NOT NULL,
    snapshot JSONB NOT NULL,
    parent_version_id UUID REFERENCES persona_versions(version_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, version_number)
);

CREATE UNIQUE INDEX idx_pg_persona_one_active_version
ON persona_versions(account_id) WHERE status = 'active';

DO $legacy_rls$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'persona_traits', 'persona_evidence', 'persona_observation_receipts',
        'speech_style_stats', 'persona_learning_consents', 'persona_versions'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target);
        EXECUTE format(
            'CREATE POLICY %I ON %I '
            'USING (account_id = current_setting(''app.account_id'', true)) '
            'WITH CHECK (account_id = current_setting(''app.account_id'', true))',
            target || '_legacy_policy',
            target
        );
    END LOOP;
END;
$legacy_rls$;
"""

_SUBJECT_TABLES = ("persona_traits", "speech_style_stats", "persona_versions")


def _legacy_trait_id(account_id: str, category: str, normalized_key: str) -> uuid.UUID:
    key = f"{account_id}:{category}:{normalized_key}"
    return uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:persona-trait:{key}")


def _event(
    account_id: str,
    event_id: str,
    text: str,
    *,
    subject_id: str | None,
    minute: int,
) -> EvidenceEvent:
    return EvidenceEvent(
        event_id=event_id,
        account_id=account_id,
        session_id="subject-session",
        turn_id=minute + 1,
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 9, 26, 9, minute, tzinfo=UTC),
        speaker_class="owner",
        source="test",
        subject_id=subject_id,
        payload={
            "text": text,
            "persona_eligible": True,
            "owner_projection_eligible": True,
            "interaction_mode": "companion",
            "prompt_kind": "spontaneous",
        },
    )


async def _cleanup(dsn: str, account_id: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        for table in (
            "persona_learning_consents",
            "persona_versions",
            "persona_traits",
            "speech_style_stats",
            "persona_observation_receipts",
            "archive_processing_outbox",
            "archive_evidence_events",
        ):
            await connection.execute(f"DELETE FROM {table} WHERE account_id = $1", account_id)
    finally:
        await connection.close()


@pytest.fixture
async def stores() -> AsyncIterator[tuple[str, PostgresLifeArchive, PostgresPersonaEngine]]:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = f"pg-subject-account-{uuid.uuid4().hex[:8]}"
    archive = PostgresLifeArchive(dsn)
    engine = PostgresPersonaEngine(dsn)
    await archive.initialize()
    await engine.initialize()
    try:
        yield account_id, archive, engine
    finally:
        await _cleanup(dsn, account_id)
        await engine.close()
        await archive.close()


async def _learn(
    archive: PostgresLifeArchive,
    engine: PostgresPersonaEngine,
    account_id: str,
    *,
    prefix: str,
    texts: tuple[str, ...],
    subject_id: str | None,
    minute: int,
    learning_allowed: bool = True,
) -> list[str | None]:
    published: list[str | None] = []
    for index, text in enumerate(texts):
        event_id = f"{account_id}-{prefix}-{index}"
        await archive.record(
            _event(account_id, event_id, text, subject_id=subject_id, minute=minute + index)
        )
        result = await engine.observe(
            PersonaEvidence(
                account_id=account_id,
                source_event_id=event_id,
                learning_allowed=learning_allowed,
            )
        )
        published.append(result.published_version_id)
    return published


async def _count(account_id: str, table: str, subject_id: str) -> int:
    connection = await asyncpg.connect(os.environ["MEMORIA_TEST_POSTGRES_DSN"])
    try:
        return int(
            await connection.fetchval(
                f"SELECT count(*) FROM {table} WHERE account_id = $1 AND subject_id = $2",
                account_id,
                subject_id,
            )
        )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_postgres_subjects_learn_independently_and_forget_cleanly(
    stores: tuple[str, PostgresLifeArchive, PostgresPersonaEngine],
) -> None:
    account_id, archive, engine = stores
    child_id = f"{account_id}-child"

    # The child learns without any persona_learning_consents row; False is honoured.
    denied = await _learn(
        archive,
        engine,
        account_id,
        prefix="child-denied",
        texts=CHILD_TEXTS[:1],
        subject_id=child_id,
        minute=0,
        learning_allowed=False,
    )
    child = await _learn(
        archive, engine, account_id, prefix="child", texts=CHILD_TEXTS, subject_id=child_id,
        minute=5,
    )
    await archive.record(
        _event(account_id, f"{account_id}-holder-early", HOLDER_TEXTS[0], subject_id=account_id,
               minute=9)
    )
    holder_without_consent = await engine.observe(
        PersonaEvidence(
            account_id=account_id,
            source_event_id=f"{account_id}-holder-early",
            learning_allowed=True,
        )
    )
    assert denied == [None]
    assert child[-1] is not None
    assert holder_without_consent.reason == "learning_not_authorized"

    await engine.grant_consent(account_id=account_id, policy_version="persona-learning-v1")
    holder = await _learn(
        archive, engine, account_id, prefix="holder", texts=HOLDER_TEXTS, subject_id=account_id,
        minute=10,
    )
    assert holder[-1] is not None

    holder_capsule = await engine.capsule(
        PersonaRequest(account_id=account_id, speaker_class="owner", topic="表达看法")
    )
    child_capsule = await engine.capsule(
        PersonaRequest(
            account_id=account_id, speaker_class="owner", topic="表达看法", subject_id=child_id
        )
    )
    style_only = await engine.capsule(
        PersonaRequest(
            account_id=account_id,
            speaker_class="uncertain",
            confirmed_style_only=True,
            subject_id=child_id,
        )
    )
    gated = [
        await engine.capsule(
            PersonaRequest(account_id=account_id, speaker_class=speaker, subject_id=child_id)
        )
        for speaker in ("guest", "uncertain")
    ]
    disabled = await engine.capsule(
        PersonaRequest(
            account_id=account_id, speaker_class="owner", enabled=False, subject_id=child_id
        )
    )
    traits = await engine.traits(account_id=account_id)
    versions = await engine.versions(account_id=account_id)

    child_trait_ids = {entry.trait_id for entry in child_capsule.entries}
    assert "我觉得" in holder_capsule.prompt_fragment
    assert "其实" not in holder_capsule.prompt_fragment
    assert "其实" in child_capsule.prompt_fragment
    assert "我觉得" not in child_capsule.prompt_fragment
    assert child_capsule.version_id == child[-1]
    assert style_only.entries and all(not e.source_event_ids for e in style_only.entries)
    assert all(capsule.entries == () for capsule in (*gated, disabled))
    assert str(_legacy_trait_id(account_id, "verbal_tic", "我觉得")) in {
        trait.trait_id for trait in traits
    }
    assert child_trait_ids.isdisjoint({trait.trait_id for trait in traits})
    assert all("其实" not in trait.description for trait in traits)
    assert child[-1] not in {version.version_id for version in versions}
    assert await _count(account_id, "speech_style_stats", child_id) == 1

    # Holder-only operations cannot reach the child's rows.
    with pytest.raises(EvidenceNotFoundError):
        await engine.review(
            PersonaReview(
                account_id=account_id, trait_id=next(iter(child_trait_ids)), action="disable"
            )
        )
    child_version = child[-1]
    assert child_version is not None
    with pytest.raises(EvidenceNotFoundError):
        await engine.rollback(account_id=account_id, version_id=child_version)
    await engine.rollback(account_id=account_id, version_id=versions[-1].version_id)
    after_rollback = await engine.capsule(
        PersonaRequest(account_id=account_id, speaker_class="owner", subject_id=child_id)
    )
    assert after_rollback.prompt_fragment == child_capsule.prompt_fragment

    # Revoking the holder's consent does not hide the child's persona.
    await engine.revoke_consent(account_id=account_id)
    assert (
        await engine.capsule(
            PersonaRequest(account_id=account_id, speaker_class="owner", subject_id=child_id)
        )
    ).entries

    holder_traits = await engine.traits(account_id=account_id)
    with pytest.raises(ValueError):
        await engine.forget_subject(account_id=account_id, subject_id=account_id)
    before = await engine.remaining_subject_rows(account_id=account_id, subject_id=child_id)
    deleted = await engine.forget_subject(account_id=account_id, subject_id=child_id)
    again = await engine.forget_subject(account_id=account_id, subject_id=child_id)
    assert deleted > 0 and again == 0
    assert before["persona_traits"] > 0
    assert await engine.remaining_subject_rows(account_id=account_id, subject_id=child_id) == {
        "persona_traits": 0, "speech_style_stats": 0, "persona_versions": 0,
    }
    for table in _SUBJECT_TABLES:
        assert await _count(account_id, table, child_id) == 0
    assert await engine.traits(account_id=account_id) == holder_traits
    assert (
        await engine.capsule(
            PersonaRequest(account_id=account_id, speaker_class="owner", subject_id=child_id)
        )
    ).entries == ()


def _migration_block() -> str:
    match = re.search(
        r"DO \$persona_subject_migration\$.*?\$persona_subject_migration\$;",
        _PERSONA_SCHEMA,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(0)


@pytest.mark.asyncio
async def test_postgres_legacy_schema_migrates_in_place_as_the_forced_rls_owner() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    suffix = uuid.uuid4().hex[:10]
    schema = f"persona_subject_mig_{suffix}"
    owner = f"persona_subject_owner_{suffix}"
    account_id = "legacy-account"
    trait_id = _legacy_trait_id(account_id, "verbal_tic", "我觉得")
    version_id = uuid.UUID("30000000-0000-0000-0000-000000000001")
    admin = await asyncpg.connect(dsn)
    await admin.execute(f"CREATE ROLE {owner} NOLOGIN")
    await admin.execute(f"CREATE SCHEMA {schema} AUTHORIZATION {owner}")
    connection = await asyncpg.connect(dsn, server_settings={"search_path": schema})
    engine: PostgresPersonaEngine | None = None
    try:
        # A non-superuser owner, like memoria_app: FORCE RLS applies to it.
        await connection.execute(f"SET ROLE {owner}")
        await connection.execute(_ARCHIVE_SCHEMA)
        await connection.execute(_LEGACY_SCHEMA)
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.account_id', $1, true)", account_id
            )
            await connection.execute(
                """
                INSERT INTO archive_evidence_events (
                    event_id, account_id, event_type, schema_version, occurred_at,
                    speaker_class, source, payload, content_sha256
                ) VALUES ('legacy-event', $1, 'speech.utterance_finalized', 1, now(),
                          'owner', 'test', $2::jsonb, $3)
                """,
                account_id,
                json.dumps(
                    {
                        "text": HOLDER_TEXTS[0],
                        "persona_eligible": True,
                        "owner_projection_eligible": True,
                    },
                    ensure_ascii=False,
                ),
                "a" * 64,
            )
            await connection.execute(
                """
                INSERT INTO persona_traits (
                    trait_id, account_id, category, normalized_key, description, context,
                    confidence, status, observation_count
                ) VALUES ($1, $2, 'verbal_tic', '我觉得', '表达观点时常用“我觉得”自然起句',
                          'conversation', 0.9, 'confirmed', 3)
                """,
                trait_id,
                account_id,
            )
            await connection.execute(
                """
                INSERT INTO persona_evidence (
                    trait_id, account_id, source_event_id, scene, weight, occurred_at
                ) VALUES ($1, $2, 'legacy-event', 'conversation', 1.0, now())
                """,
                trait_id,
                account_id,
            )
            await connection.execute(
                "INSERT INTO speech_style_stats (account_id, scene) VALUES ($1, 'conversation')",
                account_id,
            )
            await connection.execute(
                """
                INSERT INTO persona_learning_consents (
                    account_id, policy_version, granted_at, grant_event_id
                ) VALUES ($1, 'persona-learning-v1', now(), 'legacy-event')
                """,
                account_id,
            )
            await connection.execute(
                """
                INSERT INTO persona_versions (
                    version_id, account_id, version_number, status, reason, snapshot
                ) VALUES ($1, $2, 1, 'active', 'legacy', $3::jsonb)
                """,
                version_id,
                account_id,
                json.dumps(
                    [
                        {
                            "trait_id": str(trait_id),
                            "category": "verbal_tic",
                            "description": "表达观点时常用“我觉得”自然起句",
                            "context": "conversation",
                            "counterexample": "",
                            "confidence": 0.9,
                            "source_event_ids": ["legacy-event"],
                        }
                    ],
                    ensure_ascii=False,
                ),
            )

        await connection.execute(_PERSONA_SCHEMA)

        # A second startup's migration block takes no table lock at all.
        async with connection.transaction():
            await connection.execute(_migration_block())
            relation_locks = await connection.fetch(
                """
                SELECT c.relname, l.mode
                FROM pg_locks AS l
                JOIN pg_class AS c ON c.oid = l.relation
                WHERE l.pid = pg_backend_pid()
                  AND c.relnamespace = to_regnamespace($1)
                  AND l.mode <> 'AccessShareLock'
                """,
                schema,
            )
        assert relation_locks == []
        await connection.execute(_PERSONA_SCHEMA)

        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.account_id', $1, true)", account_id
            )
            subjects = {
                table: await connection.fetch(
                    f"SELECT account_id, subject_id FROM {table}"  # noqa: S608
                )
                for table in _SUBJECT_TABLES
            }
            migrated_trait = await connection.fetchval("SELECT trait_id FROM persona_traits")
            migrated_version = await connection.fetchval("SELECT version_id FROM persona_versions")
            evidence = await connection.fetchval("SELECT count(*) FROM persona_evidence")
        constraints = {
            str(row["conname"])
            for row in await connection.fetch(
                """
                SELECT conname FROM pg_constraint
                WHERE connamespace = to_regnamespace($1)
                """,
                schema,
            )
        }
        indexes = {
            str(row["indexname"])
            for row in await connection.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = $1", schema
            )
        }
        not_null = await connection.fetch(
            """
            SELECT c.relname, a.attnotnull
            FROM pg_attribute AS a JOIN pg_class AS c ON c.oid = a.attrelid
            WHERE c.relnamespace = to_regnamespace($1) AND a.attname = 'subject_id'
              AND c.relname = ANY($2::text[])
            """,
            schema,
            list(_SUBJECT_TABLES),
        )
        forced = await connection.fetch(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
            WHERE relnamespace = to_regnamespace($1) AND relname = ANY($2::text[])
            """,
            schema,
            list(_SUBJECT_TABLES),
        )
        policies = {
            str(row["qual"])
            for row in await connection.fetch(
                # Only the persona tables: archive policies in the same schema
                # depend on which global roles earlier tests created.
                "SELECT qual FROM pg_policies WHERE schemaname = $1 "
                "AND (tablename LIKE 'persona%' OR tablename = 'speech_style_stats')",
                schema,
            )
        }

        assert all(
            [tuple(row) for row in rows] == [(account_id, account_id)]
            for rows in subjects.values()
        )
        assert migrated_trait == trait_id
        assert migrated_version == version_id
        assert evidence == 1
        assert {
            "persona_traits_account_subject_key",
            "speech_style_stats_pkey",
            "persona_versions_account_subject_version_key",
        } <= constraints
        assert "persona_traits_account_id_category_normalized_key_key" not in constraints
        assert "persona_versions_account_id_version_number_key" not in constraints
        assert "idx_pg_persona_one_active_subject_version" in indexes
        assert "idx_pg_persona_one_active_version" not in indexes
        assert {row["relname"] for row in not_null} == set(_SUBJECT_TABLES)
        assert all(row["attnotnull"] for row in not_null)
        assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in forced)
        assert all(
            "account_id" in policy and "subject_id" not in policy for policy in policies
        ), policies

        # The engine runs on the migrated schema and extends the same trait id.
        await connection.execute("RESET ROLE")
        engine = PostgresPersonaEngine(f"{dsn}?{urlencode({'search_path': schema})}")
        await engine.initialize()
        capsule = await engine.capsule(
            PersonaRequest(account_id=account_id, speaker_class="owner")
        )
        assert capsule.version_id == str(version_id)
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.account_id', $1, true)", account_id
            )
            await connection.execute(
                """
                INSERT INTO archive_evidence_events (
                    event_id, account_id, event_type, schema_version, occurred_at,
                    speaker_class, source, payload, content_sha256, subject_id
                ) VALUES ('after-migration', $1, 'speech.utterance_finalized', 1, now(),
                          'owner', 'test', $2::jsonb, $3, $1)
                """,
                account_id,
                json.dumps(
                    {
                        "text": HOLDER_TEXTS[1],
                        "persona_eligible": True,
                        "owner_projection_eligible": True,
                    },
                    ensure_ascii=False,
                ),
                "b" * 64,
            )
        observed = await engine.observe(
            PersonaEvidence(
                account_id=account_id, source_event_id="after-migration", learning_allowed=True
            )
        )
        assert str(trait_id) in observed.candidate_trait_ids
    finally:
        if engine is not None:
            await engine.close()
        await connection.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.execute(f"DROP OWNED BY {owner}")
        await admin.execute(f"DROP ROLE IF EXISTS {owner}")
        await admin.close()
