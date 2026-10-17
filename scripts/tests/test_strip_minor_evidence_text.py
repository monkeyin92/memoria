from __future__ import annotations

from scripts.strip_minor_evidence_text import (
    SUBJECT_CATEGORIES,
    build_parser,
    strip_minor_payload,
)


def test_strip_minor_payload_removes_all_transcript_keys_and_preserves_metadata() -> None:
    payload = {
        "subject_category": "minor",
        "memory_retention": "retained",
        "content_sha256": "already-computed",
        "text": "child text",
        "transcript": "child transcript",
        "user_text": "user words",
        "assistant_text": "assistant words",
        "event_kind": "turn",
    }

    assert strip_minor_payload(payload) == {
        "subject_category": "minor",
        "memory_retention": "retained",
        "content_sha256": "already-computed",
        "event_kind": "turn",
    }


def test_strip_minor_payload_leaves_adult_payload_unchanged() -> None:
    payload = {"subject_category": "adult", "text": "adult text", "event_kind": "turn"}

    assert strip_minor_payload(payload) == payload


def test_unknown_is_a_target_category() -> None:
    assert SUBJECT_CATEGORIES == ("minor", "unknown")
    assert strip_minor_payload({"subject_category": "unknown", "text": "text"}) == {
        "subject_category": "unknown"
    }


def test_parser_defaults_to_dry_run_and_reads_only_test_dsn(monkeypatch) -> None:
    monkeypatch.setenv("MEMORIA_TEST_POSTGRES_DSN", "postgresql://test/db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://other/db")

    args = build_parser().parse_args([])

    assert args.dsn == "postgresql://test/db"
    assert args.apply is False
