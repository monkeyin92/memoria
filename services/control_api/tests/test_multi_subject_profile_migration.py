import sqlite3

from services.control_api.app.database import MemoryStore


def test_new_profile_defaults_to_unknown_unverified_subject(tmp_path) -> None:
    store = MemoryStore(str(tmp_path / "memoria.sqlite3"))

    profile = store.get_profile(user_id="account-new", now="2026-08-09T10:00:00Z")

    assert profile["subject_category"] == "unknown"
    assert profile["birth_year_band"] == "unknown"
    assert profile["age_evidence_status"] == "unverified"


def test_legacy_default_adult_profiles_are_quarantined_during_migration(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE profiles (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '朋友',
                subject_category TEXT NOT NULL DEFAULT 'adult'
                    CHECK (subject_category IN ('adult', 'minor')),
                birth_year_band TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (birth_year_band IN (
                        'unknown', 'under_14', '14_to_17', '18_or_over'
                    )),
                subject_revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO profiles (
                user_id, subject_category, birth_year_band,
                subject_revision, created_at, updated_at
            ) VALUES
                ('legacy-default-adult', 'adult', 'unknown', 0, 't0', 't0'),
                ('legacy-minor', 'minor', '14_to_17', 2, 't0', 't0');
            """
        )

    store = MemoryStore(str(path))
    store.initialize()

    adult = store.get_subject_profile(user_id="legacy-default-adult")
    minor = store.get_subject_profile(user_id="legacy-minor")
    assert adult is not None and minor is not None
    assert (
        adult["subject_category"],
        adult["birth_year_band"],
        adult["age_evidence_status"],
    ) == ("unknown", "unknown", "unverified")
    assert adult["subject_revision"] == 1
    assert (
        minor["subject_category"],
        minor["birth_year_band"],
        minor["age_evidence_status"],
    ) == ("minor", "14_17", "unverified")
