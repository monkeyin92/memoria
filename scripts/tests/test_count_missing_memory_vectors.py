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
        for index, (subject_key, about_subject_id, status) in enumerate(rows):
            claim_id = uuid.uuid4()
            claim_ids.append(claim_id)
            event_id = f"patrol-unlinked-event-{index}-{uuid.uuid4()}"
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
                    about_subject_id, predicate, value, confidence, status,
                    sensitive_domain, extractor_version, source_event_id,
                    valid_at, observed_at
                ) VALUES (%s, %s, 'fact', 'daily_life', %s, %s, 'age', 'eight',
                          0.9, %s, 'none', 'patrol-test-v1', %s, now(), now())
                """,
                (claim_id, account_id, subject_key, about_subject_id, status, event_id),
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
