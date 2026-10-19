"""The vector-gap patrol prints counts only and exits 0/1/3."""

from __future__ import annotations

import json
from typing import Any

import pytest
from scripts import count_missing_memory_vectors as patrol
from services.archive.memory_vector_backfill import MissingVectorCount


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
    assert json.loads(output) == {"corrected": 0, "missing": 0}
    assert "account-under-test" not in output
    assert "claim" not in output


def test_gap_prints_counts_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_settings(monkeypatch)

    async def count(_settings: Any, _accounts: Any) -> MissingVectorCount:
        return MissingVectorCount(total=3, corrected=2)

    monkeypatch.setattr(patrol, "_count", count)

    assert patrol.main(["--account", "account-under-test"]) == 1
    output = capsys.readouterr().out.strip()
    assert json.loads(output) == {"corrected": 2, "missing": 3}
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
