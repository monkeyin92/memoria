"""The vector-gap patrol prints counts only and exits 0/1/3."""

from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from scripts import count_missing_memory_vectors as patrol
from services.archive.memory_vector_backfill import MissingVectorCount


class _Embedder:
    model = "patrol-unlinked-test"
    dimensions = 2

    async def embed(self, _text: str) -> tuple[float, ...]:
        return (0.0, 0.0)


def _settings(dsn: str) -> Any:
    return SimpleNamespace(
        archive_database_url=SimpleNamespace(get_secret_value=lambda: dsn),
    )


def _stub_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(patrol, "ControlSettings", lambda: object())


def test_no_gap_prints_counts_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_settings(monkeypatch)

    async def count(_settings: Any, _accounts: Any) -> MissingVectorCount:
        return MissingVectorCount(total=0, corrected=0)

    monkeypatch.setattr(patrol, "_count", count)

    assert patrol.main(["--account", "account-under-test"]) == 0
    output = capsys.readouterr().out.strip()
    assert json.loads(output) == {
        "corrected": 0,
        "missing": 0,
        "unlinked_child_claims": 0,
    }
    assert "account-under-test" not in output
    assert "claim_id" not in output


def test_gap_prints_counts_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_settings(monkeypatch)

    async def count(_settings: Any, _accounts: Any) -> MissingVectorCount:
        return MissingVectorCount(total=3, corrected=2)

    monkeypatch.setattr(patrol, "_count", count)

    assert patrol.main(["--account", "account-under-test"]) == 1
    output = capsys.readouterr().out.strip()
    assert json.loads(output) == {
        "corrected": 2,
        "missing": 3,
        "unlinked_child_claims": 0,
    }
    assert "account-under-test" not in output


def test_own_error_exits_three_without_ids(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_settings(monkeypatch)

    async def count(_settings: Any, _accounts: Any) -> MissingVectorCount:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(patrol, "_count", count)

    assert patrol.main(["--account", "account-under-test"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RuntimeError" in captured.err
    assert "account-under-test" not in captured.err
    assert "database unavailable" not in captured.err


def _insert_claim(
    connection: psycopg.Connection[Any],
    *,
    account_id: str,
    subject_key: str,
    status: str,
    claim_id: uuid.UUID,
    linked_subject_id: str | None = None,
    person_relation: str | None = None,
) -> uuid.UUID | None:
    event_id = f"patrol-unlinked-event-{claim_id}"
    connection.execute(
        """
        INSERT INTO archive_evidence_events (
            event_id, account_id, event_type, schema_version, occurred_at,
            speaker_class, source, payload, content_sha256
        ) VALUES (%s, %s, 'speech.utterance_finalized', 1, now(), 'owner',
                  'patrol-test', '{}'::jsonb, %s)
        """,
        (event_id, account_id, hashlib.sha256(event_id.encode()).hexdigest()),
    )
    connection.execute(
        """
        INSERT INTO memory_claims (
            claim_id, account_id, category, domain_category, subject_key,
            predicate, value, confidence, status, sensitive_domain,
            extractor_version, source_event_id, valid_at, observed_at
        ) VALUES (%s, %s, 'fact', 'daily_life', %s, 'age', 'eight',
                  0.9, %s, 'none', 'patrol-test-v1', %s, now(), now())
        """,
        (claim_id, account_id, subject_key, status, event_id),
    )
    if linked_subject_id is not None:
        connection.execute(
            "INSERT INTO memory_claim_subjects (account_id, claim_id, subject_id)"
            " VALUES (%s, %s, %s)",
            (account_id, claim_id, linked_subject_id),
        )
    if person_relation is None:
        return None
    person_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO person_entities (
            person_id, account_id, canonical_key, display_name,
            relationship_to_owner, status, source_event_id, created_at
        ) VALUES (%s, %s, %s, 'person', %s, 'confirmed', %s, now())
        """,
        (person_id, account_id, subject_key, person_relation, event_id),
    )
    return person_id


def test_unlinked_child_claims_are_counted_without_ids(
    postgres_database: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    account_id = f"patrol-unlinked-{uuid.uuid4()}"
    child_id = f"patrol-child-{uuid.uuid4()}"
    rows = (
        ("Son:child", None, "confirmed"),
        ("son:child", child_id, "confirmed"),
        ("daughter:child", None, "retracted"),
        ("self", None, "confirmed"),
    )
    claim_ids: list[uuid.UUID] = []
    with psycopg.connect(postgres_database.owner_dsn(), autocommit=True) as connection:
        for subject_key, linked_subject_id, status in rows:
            claim_id = uuid.uuid4()
            claim_ids.append(claim_id)
            _insert_claim(
                connection,
                account_id=account_id,
                subject_key=subject_key,
                status=status,
                claim_id=claim_id,
                linked_subject_id=linked_subject_id,
            )

    dsn = postgres_database.role_dsn("memoria_app")
    monkeypatch.setattr(patrol, "ControlSettings", lambda: _settings(dsn))
    monkeypatch.setattr(patrol, "build_memory_embedder", lambda _settings: _Embedder())

    assert patrol.main(["--account", account_id]) == 0
    output = capsys.readouterr().out.strip()
    assert json.loads(output) == {
        "corrected": 0,
        "missing": 0,
        "unlinked_child_claims": 1,
    }
    raw_values = (account_id, child_id, *(str(claim_id) for claim_id in claim_ids))
    assert all(value not in output for value in raw_values)
    assert not any(key in output.lower() for key in ("son:", "daughter:", "self"))


def test_unlinked_child_claims_count_person_relation_keys(
    postgres_database: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    account_id = f"patrol-person-{uuid.uuid4()}"
    linked_child_id = f"patrol-person-child-{uuid.uuid4()}"
    counted_key = f"child:daughter-{uuid.uuid4()}"
    linked_key = f"child:linked-{uuid.uuid4()}"
    friend_key = f"child:friend-{uuid.uuid4()}"
    rows = (
        (counted_key, "Daughter", None),
        (linked_key, "daughter", linked_child_id),
        (friend_key, "friend", None),
    )
    claim_ids: list[uuid.UUID] = []
    person_ids: list[uuid.UUID] = []
    with psycopg.connect(postgres_database.owner_dsn(), autocommit=True) as connection:
        for subject_key, relation, linked_subject_id in rows:
            claim_id = uuid.uuid4()
            claim_ids.append(claim_id)
            person_id = _insert_claim(
                connection,
                account_id=account_id,
                subject_key=subject_key,
                status="confirmed",
                claim_id=claim_id,
                linked_subject_id=linked_subject_id,
                person_relation=relation,
            )
            if person_id is not None:
                person_ids.append(person_id)

    dsn = postgres_database.role_dsn("memoria_app")
    monkeypatch.setattr(patrol, "ControlSettings", lambda: _settings(dsn))
    monkeypatch.setattr(patrol, "build_memory_embedder", lambda _settings: _Embedder())

    assert patrol.main(["--account", account_id]) == 0
    output = capsys.readouterr().out.strip()
    assert json.loads(output) == {
        "corrected": 0,
        "missing": 0,
        "unlinked_child_claims": 1,
    }
    raw_values = (
        account_id,
        linked_child_id,
        counted_key,
        linked_key,
        friend_key,
        *(str(claim_id) for claim_id in claim_ids),
        *(str(person_id) for person_id in person_ids),
    )
    assert all(value not in output for value in raw_values)
    assert not any(key in output.lower() for key in ("son:", "daughter:", "child:", "friend"))
