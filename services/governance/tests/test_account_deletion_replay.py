from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from services.control_api.app.database import MemoryStore
from services.governance.account_data import AccountDataGovernance
from services.governance.subject_deletion import SubjectDeletionLedger, SubjectScope


@pytest.mark.sqlite_only
def test_finalize_account_deletion_retains_user_id(tmp_path: Path) -> None:
    db_path = tmp_path / "memoria.sqlite3"
    store = MemoryStore(str(db_path))
    try:
        store.register_account(
            user_id="user_test_01",
            username="testuser",
            username_normalized="testuser",
            password_hash="fake-hash",
            now="2026-10-26T12:00:00Z",
        )
        deletion = store.begin_account_deletion(
            user_id="user_test_01", started_at="2026-10-26T12:00:00Z"
        )
        assert deletion["status"] == "deleting"
        assert store.pending_account_deletions() == ("user_test_01",)
        assert store.completed_account_deletions() == ()

        finalized = store.finalize_account_deletion(
            user_id="user_test_01",
            request_id=deletion["request_id"],
            completed_at="2026-10-26T12:01:00Z",
            deleted_counts={"messages": 0},
        )
        assert finalized["daily_chat_usage"] == 0
        assert finalized["messages"] == 0
        assert store.pending_account_deletions() == ()
        assert store.completed_account_deletions() == ("user_test_01",)

        # Direct database verification that user_id was not cleared to NULL
        with store._connection() as conn:
            row = conn.execute(
                "SELECT user_id, status FROM account_deletions WHERE user_id_hash = ?",
                (store._user_id_hash("user_test_01"),),
            ).fetchone()
            assert row is not None
            assert row["user_id"] == "user_test_01"
            assert row["status"] == "completed"
    finally:
        store.close()


@pytest.mark.sqlite_only
def test_export_and_merge_includes_account_deletions(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    store = MemoryStore(str(source_db))
    try:
        store.register_account(
            user_id="user_test_02",
            username="user2",
            username_normalized="user2",
            password_hash="fake-hash",
            now="2026-10-26T12:00:00Z",
        )
        deletion = store.begin_account_deletion(
            user_id="user_test_02", started_at="2026-10-26T12:00:00Z"
        )
        store.finalize_account_deletion(
            user_id="user_test_02",
            request_id=deletion["request_id"],
            completed_at="2026-10-26T12:01:00Z",
            deleted_counts={},
        )
        ledger = SubjectDeletionLedger(source_db)
        ledger.initialize()
        ledger.begin(
            SubjectScope(account_id="acc_01", subject_id="sub_01"),
            redact_identity=True,
            now="2026-10-26T12:00:00Z",
        )
        ledger.update(
            SubjectScope(account_id="acc_01", subject_id="sub_01"),
            step="completed",
            progress={},
            lineage={},
            now="2026-10-26T12:01:00Z",
        )

        snapshot = tmp_path / "snapshot.json"
        exported_count = ledger.export_completed(snapshot)
        assert exported_count == 2

        data = json.loads(snapshot.read_text(encoding="utf-8"))
        assert len(data["rows"]) == 1
        assert len(data["account_rows"]) == 1
        assert data["account_rows"][0]["user_id"] == "user_test_02"

        # Restore into a fresh target DB
        target_db = tmp_path / "target.sqlite3"
        target_store = MemoryStore(str(target_db))
        target_store.initialize()
        try:
            target_ledger = SubjectDeletionLedger(target_db)
            target_ledger.initialize()
            merged = target_ledger.merge_completed(snapshot)
            assert merged["merged"] == 1
            assert merged["account_merged"] == 1
            assert target_store.completed_account_deletions() == ("user_test_02",)
        finally:
            target_store.close()
    finally:
        store.close()


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_account_governance_replay_completed_deletions(tmp_path: Path) -> None:
    db_path = tmp_path / "memoria.sqlite3"
    store = MemoryStore(str(db_path))
    try:
        store.register_account(
            user_id="user_test_03",
            username="user3",
            username_normalized="user3",
            password_hash="fake-hash",
            now="2026-10-26T12:00:00Z",
        )
        deletion = store.begin_account_deletion(
            user_id="user_test_03", started_at="2026-10-26T12:00:00Z"
        )
        store.finalize_account_deletion(
            user_id="user_test_03",
            request_id=deletion["request_id"],
            completed_at="2026-10-26T12:01:00Z",
            deleted_counts={},
        )
        assert store.completed_account_deletions() == ("user_test_03",)

        voice_profiles = MagicMock()
        voice_profiles.profiles = AsyncMock(return_value=[])
        archive_repo = MagicMock()
        archive_repo.object_references = AsyncMock(return_value=[])
        archive_repo.delete_account = AsyncMock(return_value={})
        archive_repo.remaining_account_rows = AsyncMock(return_value={})
        speaker_repo = MagicMock()
        speaker_repo.delete_account = AsyncMock(return_value={})
        speaker_repo.remaining_account_rows = AsyncMock(return_value={})
        evolution_repo = MagicMock()
        evolution_repo.mark_account_deleting = AsyncMock()
        evolution_repo.delete_account = AsyncMock(return_value={})
        evolution_repo.remaining_account_rows = AsyncMock(return_value={})

        governance = AccountDataGovernance(
            memory_store=store,
            archive_repository=archive_repo,
            speaker_repository=speaker_repo,
            evolution_repository=evolution_repo,
            guardian_repository=None,
            corpus_retention_service=None,
            voice_profiles=voice_profiles,
            legacy_registry=None,
            archive_object_store=None,
            session_terminator=None,
            operation_blocker=None,
        )

        report = await governance.replay_completed_deletions()
        assert report == {"account_replayed": 1, "account_incomplete": 0}
        assert store.completed_account_deletions() == ("user_test_03",)
    finally:
        store.close()
