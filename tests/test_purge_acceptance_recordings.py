"""Acceptance recordings older than 30 days go, and the receipt names each hash."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from scripts.purge_acceptance_recordings import due, main, purge

NOW = 1_800_000_000.0
OLD = NOW - 31 * 86400
FRESH = NOW - 86400


def _file(path: Path, content: bytes, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


def test_only_recordings_older_than_the_window_are_due(tmp_path: Path) -> None:
    root = tmp_path / "docs" / "acceptance"
    old = root / "run-old" / "take.wav"
    fresh = root / "run-new" / "take.wav"
    note = root / "run-old" / "notes.md"
    _file(old, b"old-audio", OLD)
    _file(fresh, b"fresh-audio", FRESH)
    _file(note, b"not audio", OLD)
    assert due(root, now=NOW, retention_seconds=30 * 86400) == [old]


def test_a_dry_run_deletes_nothing(tmp_path: Path, capsys) -> None:
    root = tmp_path / "share" / "真机测试"
    audio = root / "take.ogg"
    _file(audio, b"ogg", OLD)
    code = main(["--root", str(root), "--now", str(NOW)])
    assert code == 0
    assert audio.read_bytes() == b"ogg"
    assert "purge_mode=dry-run due=1" in capsys.readouterr().out


def test_apply_deletes_the_file_and_records_its_hash(tmp_path: Path, capsys) -> None:
    root = tmp_path / "docs" / "acceptance"
    content = b"field-recording"
    audio = root / "run" / "take.wav"
    _file(audio, content, OLD)
    receipt = tmp_path / "receipts" / "purge.json"
    code = main(
        [
            "--root",
            str(root),
            "--now",
            str(NOW),
            "--apply",
            "--receipt",
            str(receipt),
        ]
    )
    assert code == 0
    assert not audio.exists()
    written = json.loads(receipt.read_text(encoding="utf-8"))
    assert written == {
        "deleted": [
            {
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "path": str(audio),
            }
        ]
    }
    assert content not in receipt.read_bytes()
    assert "purge_mode=apply deleted=1" in capsys.readouterr().out


def test_apply_without_a_receipt_refuses(tmp_path: Path) -> None:
    root = tmp_path / "docs" / "acceptance"
    audio = root / "take.mp3"
    _file(audio, b"mp3", OLD)
    code = main(["--root", str(root), "--now", str(NOW), "--apply"])
    assert code == 2
    assert audio.read_bytes() == b"mp3"


def test_purge_writes_one_row_per_file(tmp_path: Path) -> None:
    first = tmp_path / "a.wav"
    second = tmp_path / "nested" / "b.pcm"
    _file(first, b"one", OLD)
    _file(second, b"two", OLD)
    receipt = tmp_path / "out.json"
    rows = purge([first, second], receipt)
    assert [row["bytes"] for row in rows] == [3, 3]
    assert not first.exists()
    assert not second.exists()
