"""Persona follows the person using the device: rows keyed by (account, subject)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent, EvidenceNotFoundError
from services.archive.life_archive import LifeArchive
from services.persona.domain import PersonaEvidence, PersonaRequest, PersonaReview
from services.persona.engine import PersonaEngine

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

# The pre-subject SQLite shape, copied from the engine before the migration.
_LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_traits (
    trait_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    description TEXT NOT NULL,
    context TEXT NOT NULL,
    counterexample TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disabled')),
    observation_count INTEGER NOT NULL DEFAULT 0 CHECK (observation_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    review_event_id TEXT,
    UNIQUE (account_id, category, normalized_key)
);

CREATE TABLE IF NOT EXISTS persona_evidence (
    trait_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    scene TEXT NOT NULL,
    weight REAL NOT NULL CHECK (weight >= 0 AND weight <= 1),
    occurred_at TEXT NOT NULL,
    PRIMARY KEY (trait_id, source_event_id),
    FOREIGN KEY (trait_id) REFERENCES persona_traits(trait_id) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS persona_observation_receipts (
    source_event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS speech_style_stats (
    account_id TEXT NOT NULL,
    scene TEXT NOT NULL,
    utterance_count INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    speech_duration_ms INTEGER NOT NULL DEFAULT 0,
    pause_ratio_sum REAL NOT NULL DEFAULT 0,
    pause_sample_count INTEGER NOT NULL DEFAULT 0,
    tic_counts_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, scene)
);

CREATE TABLE IF NOT EXISTS persona_learning_consents (
    account_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    grant_event_id TEXT NOT NULL,
    revoke_event_id TEXT
);

CREATE TABLE IF NOT EXISTS persona_versions (
    version_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    reason TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    parent_version_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, version_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_one_active_version
ON persona_versions(account_id) WHERE status = 'active';
"""


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
    archive: LifeArchive,
    engine: PersonaEngine,
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


def _setup(tmp_path: Path) -> tuple[LifeArchive, PersonaEngine, Path]:
    path = tmp_path / "memoria.sqlite3"
    archive = LifeArchive.sqlite(path)
    archive.initialize()
    engine = PersonaEngine.sqlite(path)
    engine.initialize()
    return archive, engine, path


def _count(path: Path, table: str, subject_id: str) -> int:
    with sqlite3.connect(path) as connection:
        return int(
            connection.execute(
                f"SELECT count(*) FROM {table} WHERE account_id = ? AND subject_id = ?",
                (ACCOUNT, subject_id),
            ).fetchone()[0]
        )


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
async def test_subjects_under_one_account_learn_independently(tmp_path: Path) -> None:
    archive, engine, path = _setup(tmp_path)
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
    assert _count(path, "speech_style_stats", ACCOUNT) == 1
    assert _count(path, "speech_style_stats", CHILD) == 1
    assert _count(path, "persona_versions", CHILD) >= 1


@pytest.mark.asyncio
async def test_other_subject_consent_is_the_callers_decision(tmp_path: Path) -> None:
    archive, engine, path = _setup(tmp_path)

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
    assert _count(path, "persona_traits", CHILD) == 0

    # No persona_learning_consents row exists for anyone, yet the child learns.
    child = await _learn(
        archive, engine, prefix="child", texts=CHILD_TEXTS, subject_id=CHILD, minute=10
    )
    assert child[-1] is not None
    assert _count(path, "persona_traits", CHILD) > 0

    # The account holder still needs its own consent row, re-checked at write.
    await archive.record(_event("holder-0", HOLDER_TEXTS[0], subject_id=ACCOUNT, minute=20))
    holder = await engine.observe(
        PersonaEvidence(account_id=ACCOUNT, source_event_id="holder-0", learning_allowed=True)
    )
    assert (holder.accepted, holder.reason) == (False, "learning_not_authorized")
    assert _count(path, "persona_traits", ACCOUNT) == 0

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
async def test_subject_capsule_keeps_the_account_speaker_gate(tmp_path: Path) -> None:
    archive, engine, _path = _setup(tmp_path)
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
async def test_holder_review_and_rollback_never_touch_another_subject(tmp_path: Path) -> None:
    archive, engine, _path = _setup(tmp_path)
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
async def test_forget_subject_removes_only_that_subject(tmp_path: Path) -> None:
    archive, engine, path = _setup(tmp_path)
    await engine.grant_consent(account_id=ACCOUNT, policy_version="persona-learning-v1")
    await _learn(archive, engine, prefix="holder", texts=HOLDER_TEXTS, subject_id=ACCOUNT, minute=0)
    await _learn(archive, engine, prefix="child", texts=CHILD_TEXTS, subject_id=CHILD, minute=10)
    holder_traits = await engine.traits(account_id=ACCOUNT)
    holder_versions = await engine.versions(account_id=ACCOUNT)

    with pytest.raises(ValueError):
        await engine.forget_subject(account_id=ACCOUNT, subject_id=ACCOUNT)
    with pytest.raises(ValueError):
        await engine.forget_subject(account_id=ACCOUNT, subject_id="  ")
    deleted = await engine.forget_subject(account_id=ACCOUNT, subject_id=CHILD)
    again = await engine.forget_subject(account_id=ACCOUNT, subject_id=CHILD)

    assert deleted > 0
    assert again == 0
    for table in ("persona_traits", "speech_style_stats", "persona_versions"):
        assert _count(path, table, CHILD) == 0
    with sqlite3.connect(path) as connection:
        orphaned = connection.execute(
            """
            SELECT count(*) FROM persona_evidence
            WHERE trait_id NOT IN (SELECT trait_id FROM persona_traits)
            """
        ).fetchone()[0]
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


def _legacy_database(path: Path) -> dict[str, str]:
    LifeArchive.sqlite(path).initialize()
    now = datetime(2026, 9, 1, tzinfo=UTC).isoformat()
    trait_id = _legacy_trait_id(ACCOUNT, "verbal_tic", "我觉得")
    version_id = "30000000-0000-0000-0000-000000000001"
    snapshot = [
        {
            "trait_id": trait_id,
            "category": "verbal_tic",
            "description": "表达观点时常用“我觉得”自然起句",
            "context": "conversation",
            "counterexample": "",
            "confidence": 0.9,
            "source_event_ids": ["legacy-event"],
        }
    ]
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_LEGACY_SCHEMA)
        payload = json.dumps(
            {
                "text": HOLDER_TEXTS[0],
                "persona_eligible": True,
                "owner_projection_eligible": True,
            },
            ensure_ascii=False,
        )
        connection.execute(
            """
            INSERT INTO evidence_events (
                event_id, account_id, event_type, schema_version, occurred_at,
                recorded_at, speaker_class, source, payload_json, content_sha256
            ) VALUES ('legacy-event', ?, 'speech.utterance_finalized', 1, ?, ?,
                      'owner', 'test', ?, ?)
            """,
            (ACCOUNT, now, now, payload, "a" * 64),
        )
        connection.execute(
            """
            INSERT INTO persona_traits (
                trait_id, account_id, category, normalized_key, description, context,
                confidence, status, observation_count, created_at, updated_at
            ) VALUES (?, ?, 'verbal_tic', '我觉得', ?, 'conversation', 0.9,
                      'confirmed', 3, ?, ?)
            """,
            (trait_id, ACCOUNT, snapshot[0]["description"], now, now),
        )
        connection.execute(
            """
            INSERT INTO persona_evidence (
                trait_id, account_id, source_event_id, scene, weight, occurred_at
            ) VALUES (?, ?, 'legacy-event', 'conversation', 1.0, ?)
            """,
            (trait_id, ACCOUNT, now),
        )
        connection.execute(
            """
            INSERT INTO persona_observation_receipts (source_event_id, account_id, observed_at)
            VALUES ('legacy-event', ?, ?)
            """,
            (ACCOUNT, now),
        )
        connection.execute(
            """
            INSERT INTO speech_style_stats (
                account_id, scene, utterance_count, char_count, tic_counts_json, updated_at
            ) VALUES (?, 'conversation', 1, 11, '{"我觉得": 1}', ?)
            """,
            (ACCOUNT, now),
        )
        connection.execute(
            """
            INSERT INTO persona_learning_consents (
                account_id, policy_version, granted_at, grant_event_id
            ) VALUES (?, 'persona-learning-v1', ?, 'legacy-event')
            """,
            (ACCOUNT, now),
        )
        connection.execute(
            """
            INSERT INTO persona_versions (
                version_id, account_id, version_number, status, reason,
                snapshot_json, created_at
            ) VALUES (?, ?, 1, 'active', 'legacy', ?, ?)
            """,
            (version_id, ACCOUNT, json.dumps(snapshot, ensure_ascii=False), now),
        )
    return {"trait_id": trait_id, "version_id": version_id}


def _schema_and_rows(path: Path) -> tuple[list[tuple[str, str]], dict[str, list[tuple[object, ...]]]]:
    with sqlite3.connect(path) as connection:
        schema = connection.execute(
            """
            SELECT name, sql FROM sqlite_master
            WHERE sql IS NOT NULL AND (name LIKE 'persona%' OR name LIKE 'speech%'
                                       OR name LIKE 'idx_persona%')
            ORDER BY name
            """
        ).fetchall()
        rows = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "persona_traits",
                "persona_evidence",
                "persona_observation_receipts",
                "speech_style_stats",
                "persona_versions",
            )
        }
    return schema, rows


@pytest.mark.asyncio
async def test_legacy_sqlite_file_migrates_in_place_once(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    ids = _legacy_database(path)

    engine = PersonaEngine.sqlite(path)
    engine.initialize()
    schema, rows = _schema_and_rows(path)
    PersonaEngine.sqlite(path).initialize()
    schema_again, rows_again = _schema_and_rows(path)

    with sqlite3.connect(path) as connection:
        subjects = {
            table: connection.execute(
                f"SELECT DISTINCT account_id, subject_id FROM {table}"
            ).fetchall()
            for table in ("persona_traits", "speech_style_stats", "persona_versions")
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'persona_versions'"
            )
        }
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        leftovers = connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%__subject_rebuild'"
        ).fetchall()

    assert all(pairs == [(ACCOUNT, ACCOUNT)] for pairs in subjects.values())
    assert [row[0] for row in rows["persona_traits"]] == [ids["trait_id"]]
    assert [row[0] for row in rows["persona_versions"]] == [ids["version_id"]]
    assert len(rows["persona_evidence"]) == 1
    assert len(rows["persona_observation_receipts"]) == 1
    assert len(rows["speech_style_stats"]) == 1
    assert "idx_persona_one_active_subject_version" in indexes
    assert "idx_persona_one_active_version" not in indexes
    assert foreign_keys == [] and leftovers == []
    assert (schema_again, rows_again) == (schema, rows)

    traits = await engine.traits(account_id=ACCOUNT)
    capsule = await engine.capsule(PersonaRequest(account_id=ACCOUNT, speaker_class="owner"))
    assert [trait.trait_id for trait in traits] == [ids["trait_id"]]
    assert traits[0].source_event_ids == ("legacy-event",)
    assert capsule.version_id == ids["version_id"]

    # New learning keeps extending the migrated trait instead of forking it.
    archive = LifeArchive.sqlite(path)
    await archive.record(_event("after-migration", HOLDER_TEXTS[1], subject_id=ACCOUNT, minute=5))
    result = await engine.observe(
        PersonaEvidence(account_id=ACCOUNT, source_event_id="after-migration", learning_allowed=True)
    )
    assert ids["trait_id"] in result.candidate_trait_ids
    with sqlite3.connect(path) as connection:
        next_version = connection.execute(
            "SELECT MAX(version_number) FROM persona_versions WHERE account_id = ?", (ACCOUNT,)
        ).fetchone()[0]
    assert next_version >= 1


def test_rows_written_without_a_subject_belong_to_the_account_holder(tmp_path: Path) -> None:
    _archive, _engine, path = _setup(tmp_path)
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO persona_traits (
                trait_id, account_id, category, normalized_key, description, context,
                confidence, created_at, updated_at
            ) VALUES ('fixture-trait', ?, 'verbal_tic', 'k', 'd', 'conversation', 0.5, ?, ?)
            """,
            (ACCOUNT, now, now),
        )
        connection.execute(
            "INSERT INTO speech_style_stats (account_id, scene, updated_at) VALUES (?, 's', ?)",
            (ACCOUNT, now),
        )
        connection.execute(
            """
            INSERT INTO persona_versions (
                version_id, account_id, version_number, status, reason, snapshot_json, created_at
            ) VALUES ('fixture-version', ?, 1, 'active', 'fixture', '[]', ?)
            """,
            (ACCOUNT, now),
        )
        rows = [
            connection.execute(f"SELECT subject_id, status FROM {table}").fetchall()
            if table != "speech_style_stats"
            else connection.execute(f"SELECT subject_id, utterance_count FROM {table}").fetchall()
            for table in ("persona_traits", "speech_style_stats", "persona_versions")
        ]
    assert rows == [[(ACCOUNT, "candidate")], [(ACCOUNT, 0)], [(ACCOUNT, "active")]]
