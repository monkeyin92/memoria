"""Persona follows the person using the device: rows keyed by (account, subject)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg
import pytest
from services.archive.domain import EvidenceEvent, EvidenceNotFoundError
from services.archive.postgres_archive import PostgresLifeArchive
from services.persona.domain import PersonaEvidence, PersonaRequest, PersonaReview
from services.persona.postgres_engine import PostgresPersonaEngine

if TYPE_CHECKING:
    from testing.postgres_harness import TestDatabase

EngineFactory = Callable[..., Awaitable[PostgresPersonaEngine]]

ACCOUNT = "family-account"
CHILD = "child-subject"
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


def _legacy_trait_id(account_id: str, category: str, normalized_key: str) -> str:
    key = f"{account_id}:{category}:{normalized_key}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:persona-trait:{key}"))


def _event(
    event_id: str,
    text: str,
    *,
    subject_id: str | None,
    minute: int,
    account_id: str = ACCOUNT,
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


async def _learn(
    archive: PostgresLifeArchive,
    engine: PostgresPersonaEngine,
    *,
    prefix: str,
    texts: tuple[str, ...],
    subject_id: str | None,
    minute: int,
    learning_allowed: bool = True,
) -> list[str | None]:
    published: list[str | None] = []
    for index, text in enumerate(texts):
        event_id = f"{prefix}-{index}"
        await archive.record(_event(event_id, text, subject_id=subject_id, minute=minute + index))
        result = await engine.observe(
            PersonaEvidence(
                account_id=ACCOUNT,
                source_event_id=event_id,
                learning_allowed=learning_allowed,
            )
        )
        published.append(result.published_version_id)
    return published


async def _count(database: TestDatabase, table: str, subject_id: str) -> int:
    connection = await asyncpg.connect(database.owner_dsn())
    try:
        return int(
            await connection.fetchval(
                f"SELECT count(*) FROM {table} WHERE account_id = $1 AND subject_id = $2",
                ACCOUNT,
                subject_id,
            )
        )
    finally:
        await connection.close()


def test_persona_request_bounds_subject_id() -> None:
    assert PersonaRequest(account_id=ACCOUNT, speaker_class="owner").effective_subject_id == (
        ACCOUNT
    )
    assert (
        PersonaRequest(
            account_id=ACCOUNT, speaker_class="owner", subject_id=CHILD
        ).effective_subject_id
        == CHILD
    )
    for invalid in ("", "   ", "x" * 129):
        with pytest.raises(ValueError):
            PersonaRequest(account_id=ACCOUNT, speaker_class="owner", subject_id=invalid)


@pytest.mark.asyncio
async def test_subjects_under_one_account_learn_independently(
    archive: PostgresLifeArchive,
    make_engine: EngineFactory,
    postgres_database: TestDatabase,
) -> None:
    engine = await make_engine()
    await engine.grant_consent(account_id=ACCOUNT, policy_version="persona-learning-v1")

    holder = await _learn(
        archive, engine, prefix="holder", texts=HOLDER_TEXTS, subject_id=ACCOUNT, minute=0
    )
    child = await _learn(
        archive, engine, prefix="child", texts=CHILD_TEXTS, subject_id=CHILD, minute=10
    )

    holder_capsule = await engine.capsule(
        PersonaRequest(account_id=ACCOUNT, speaker_class="owner", topic="表达看法")
    )
    child_capsule = await engine.capsule(
        PersonaRequest(
            account_id=ACCOUNT, speaker_class="owner", topic="表达看法", subject_id=CHILD
        )
    )
    explicit_holder = await engine.capsule(
        PersonaRequest(account_id=ACCOUNT, speaker_class="owner", subject_id=ACCOUNT)
    )
    traits = await engine.traits(account_id=ACCOUNT)
    versions = await engine.versions(account_id=ACCOUNT)

    assert holder[-1] is not None and child[-1] is not None
    assert "我觉得" in holder_capsule.prompt_fragment
    assert "其实" not in holder_capsule.prompt_fragment
    assert "其实" in child_capsule.prompt_fragment
    assert "我觉得" not in child_capsule.prompt_fragment
    assert child_capsule.version_id == child[-1]
    assert explicit_holder.version_id == holder_capsule.version_id
    # Both subjects start their own version history at 1.
    assert holder_capsule.version_number is not None
    assert child_capsule.version_number is not None
    assert all("其实" not in trait.description for trait in traits)
    assert any("我觉得" in trait.description for trait in traits)
    assert _legacy_trait_id(ACCOUNT, "verbal_tic", "我觉得") in {t.trait_id for t in traits}
    child_trait_ids = {entry.trait_id for entry in child_capsule.entries}
    assert child_trait_ids.isdisjoint({trait.trait_id for trait in traits})
    assert _legacy_trait_id(ACCOUNT, "verbal_tic", "其实") not in child_trait_ids
    assert child[-1] not in {version.version_id for version in versions}
    assert all(not set(v.trait_ids) & child_trait_ids for v in versions)
    assert await _count(postgres_database, "speech_style_stats", ACCOUNT) == 1
    assert await _count(postgres_database, "speech_style_stats", CHILD) == 1
    assert await _count(postgres_database, "persona_versions", CHILD) >= 1


@pytest.mark.asyncio
async def test_other_subject_consent_is_the_callers_decision(
    archive: PostgresLifeArchive,
    make_engine: EngineFactory,
    postgres_database: TestDatabase,
) -> None:
    engine = await make_engine()

    denied = await _learn(
        archive,
        engine,
        prefix="child-denied",
        texts=CHILD_TEXTS[:1],
        subject_id=CHILD,
        minute=0,
        learning_allowed=False,
    )
    assert denied == [None]
    assert await _count(postgres_database, "persona_traits", CHILD) == 0

    # No persona_learning_consents row exists for anyone, yet the child learns.
    child = await _learn(
        archive, engine, prefix="child", texts=CHILD_TEXTS, subject_id=CHILD, minute=10
    )
    assert child[-1] is not None
    assert await _count(postgres_database, "persona_traits", CHILD) > 0

    # The account holder still needs its own consent row, re-checked at write.
    await archive.record(_event("holder-0", HOLDER_TEXTS[0], subject_id=ACCOUNT, minute=20))
    holder = await engine.observe(
        PersonaEvidence(account_id=ACCOUNT, source_event_id="holder-0", learning_allowed=True)
    )
    assert (holder.accepted, holder.reason) == (False, "learning_not_authorized")
    assert await _count(postgres_database, "persona_traits", ACCOUNT) == 0

    # A legacy event without a subject is the account holder's.
    await archive.record(_event("legacy-0", HOLDER_TEXTS[1], subject_id=None, minute=21))
    legacy = await engine.observe(
        PersonaEvidence(account_id=ACCOUNT, source_event_id="legacy-0", learning_allowed=True)
    )
    assert legacy.reason == "learning_not_authorized"

    # Revoking the holder's consent neither blocks nor hides the child.
    await engine.grant_consent(account_id=ACCOUNT, policy_version="persona-learning-v1")
    await engine.revoke_consent(account_id=ACCOUNT)
    await archive.record(_event("child-late", CHILD_TEXTS[0], subject_id=CHILD, minute=30))
    late = await engine.observe(
        PersonaEvidence(account_id=ACCOUNT, source_event_id="child-late", learning_allowed=True)
    )
    capsule = await engine.capsule(
        PersonaRequest(account_id=ACCOUNT, speaker_class="owner", subject_id=CHILD)
    )
    assert late.accepted is True
    assert "其实" in capsule.prompt_fragment


@pytest.mark.asyncio
async def test_subject_capsule_keeps_the_account_speaker_gate(
    archive: PostgresLifeArchive,
    make_engine: EngineFactory,
    postgres_database: TestDatabase,
) -> None:
    engine = await make_engine()
    await _learn(archive, engine, prefix="child", texts=CHILD_TEXTS, subject_id=CHILD, minute=0)

    def request(**overrides: object) -> PersonaRequest:
        fields: dict[str, object] = {
            "account_id": ACCOUNT,
            "speaker_class": "owner",
            "topic": "表达看法",
            "subject_id": CHILD,
        }
        fields.update(overrides)
        return PersonaRequest(**fields)  # type: ignore[arg-type]

    owner = await engine.capsule(request())
    guest = await engine.capsule(request(speaker_class="guest"))
    uncertain = await engine.capsule(request(speaker_class="uncertain"))
    disabled = await engine.capsule(request(enabled=False))
    style_only = await engine.capsule(
        request(speaker_class="uncertain", confirmed_style_only=True)
    )
    stranger = await engine.capsule(request(subject_id="someone-else"))

    assert owner.entries
    assert guest.entries == () and uncertain.entries == () and disabled.entries == ()
    assert stranger.entries == ()
    assert style_only.entries
    assert "其实" in style_only.prompt_fragment
    assert all(not entry.source_event_ids for entry in style_only.entries)
    assert all(not entry.context for entry in style_only.entries)


@pytest.mark.asyncio
async def test_holder_review_and_rollback_never_touch_another_subject(
    archive: PostgresLifeArchive,
    make_engine: EngineFactory,
    postgres_database: TestDatabase,
) -> None:
    engine = await make_engine()
    await engine.grant_consent(account_id=ACCOUNT, policy_version="persona-learning-v1")
    await _learn(archive, engine, prefix="holder", texts=HOLDER_TEXTS, subject_id=ACCOUNT, minute=0)
    child = await _learn(
        archive, engine, prefix="child", texts=CHILD_TEXTS, subject_id=CHILD, minute=10
    )
    child_capsule = await engine.capsule(
        PersonaRequest(account_id=ACCOUNT, speaker_class="owner", subject_id=CHILD)
    )
    child_trait = child_capsule.entries[0].trait_id
    child_version = child[-1]
    assert child_version is not None

    with pytest.raises(EvidenceNotFoundError):
        await engine.review(PersonaReview(account_id=ACCOUNT, trait_id=child_trait, action="disable"))
    with pytest.raises(EvidenceNotFoundError):
        await engine.rollback(account_id=ACCOUNT, version_id=child_version)

    holder_versions = await engine.versions(account_id=ACCOUNT)
    await engine.rollback(account_id=ACCOUNT, version_id=holder_versions[-1].version_id)
    holder_trait = next(
        trait for trait in await engine.traits(account_id=ACCOUNT) if "我觉得" in trait.description
    )
    await engine.review(
        PersonaReview(account_id=ACCOUNT, trait_id=holder_trait.trait_id, action="disable")
    )

    after = await engine.capsule(
        PersonaRequest(account_id=ACCOUNT, speaker_class="owner", subject_id=CHILD)
    )
    assert after.version_id == child_capsule.version_id
    assert after.prompt_fragment == child_capsule.prompt_fragment


@pytest.mark.asyncio
async def test_forget_subject_removes_only_that_subject(
    archive: PostgresLifeArchive,
    make_engine: EngineFactory,
    postgres_database: TestDatabase,
) -> None:
    engine = await make_engine()
    await engine.grant_consent(account_id=ACCOUNT, policy_version="persona-learning-v1")
    await _learn(archive, engine, prefix="holder", texts=HOLDER_TEXTS, subject_id=ACCOUNT, minute=0)
    await _learn(archive, engine, prefix="child", texts=CHILD_TEXTS, subject_id=CHILD, minute=10)
    holder_traits = await engine.traits(account_id=ACCOUNT)
    holder_versions = await engine.versions(account_id=ACCOUNT)

    with pytest.raises(ValueError):
        await engine.forget_subject(account_id=ACCOUNT, subject_id=ACCOUNT)
    with pytest.raises(ValueError):
        await engine.forget_subject(account_id=ACCOUNT, subject_id="  ")
    before = await engine.remaining_subject_rows(account_id=ACCOUNT, subject_id=CHILD)
    deleted = await engine.forget_subject(account_id=ACCOUNT, subject_id=CHILD)
    again = await engine.forget_subject(account_id=ACCOUNT, subject_id=CHILD)
    after = await engine.remaining_subject_rows(account_id=ACCOUNT, subject_id=CHILD)

    assert before["persona_traits"] > 0
    assert after == {"persona_traits": 0, "speech_style_stats": 0, "persona_versions": 0}
    with pytest.raises(ValueError):
        await engine.remaining_subject_rows(account_id=ACCOUNT, subject_id=ACCOUNT)

    assert deleted > 0
    assert again == 0
    for table in ("persona_traits", "speech_style_stats", "persona_versions"):
        assert await _count(postgres_database, table, CHILD) == 0
    connection = await asyncpg.connect(postgres_database.owner_dsn())
    try:
        orphaned = await connection.fetchval(
            """
            SELECT count(*) FROM persona_evidence
            WHERE trait_id NOT IN (SELECT trait_id FROM persona_traits)
            """
        )
    finally:
        await connection.close()
    assert orphaned == 0
    assert (
        await engine.capsule(
            PersonaRequest(account_id=ACCOUNT, speaker_class="owner", subject_id=CHILD)
        )
    ).entries == ()
    assert await engine.traits(account_id=ACCOUNT) == holder_traits
    assert await engine.versions(account_id=ACCOUNT) == holder_versions
    assert (
        await engine.capsule(PersonaRequest(account_id=ACCOUNT, speaker_class="owner"))
    ).entries


@pytest.mark.asyncio
async def test_rows_written_without_a_subject_belong_to_the_account_holder(
    postgres_database: TestDatabase,
) -> None:
    connection = await asyncpg.connect(postgres_database.role_dsn("memoria_app"))
    try:
        async with connection.transaction():
            await connection.execute("SELECT set_config('app.account_id', $1, true)", ACCOUNT)
            await connection.execute(
                """
                INSERT INTO persona_traits (
                    trait_id, account_id, category, normalized_key, description, context,
                    confidence
                ) VALUES ($1, $2, 'verbal_tic', 'k', 'd', 'conversation', 0.5)
                """,
                uuid.uuid4(),
                ACCOUNT,
            )
            await connection.execute(
                "INSERT INTO speech_style_stats (account_id, scene) VALUES ($1, 's')", ACCOUNT
            )
            await connection.execute(
                """
                INSERT INTO persona_versions (
                    version_id, account_id, version_number, status, reason, snapshot
                ) VALUES ($1, $2, 1, 'active', 'fixture', '[]'::jsonb)
                """,
                uuid.uuid4(),
                ACCOUNT,
            )
            rows = [
                [
                    tuple(row)
                    for row in await connection.fetch(
                        f"SELECT subject_id, {column} FROM {table}"  # noqa: S608
                    )
                ]
                for table, column in (
                    ("persona_traits", "status"),
                    ("speech_style_stats", "utterance_count"),
                    ("persona_versions", "status"),
                )
            ]
    finally:
        await connection.close()
    assert rows == [[(ACCOUNT, "candidate")], [(ACCOUNT, 0)], [(ACCOUNT, "active")]]
