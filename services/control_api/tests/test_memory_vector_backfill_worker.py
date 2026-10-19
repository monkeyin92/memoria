"""The periodic repair starts only with vectors and logs counts only."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from services.archive.memory_vector_backfill import (
    BackfillReport,
    MemoryVectorBackfillWorker,
    build_memory_vector_backfill_worker,
)
from services.control_api.app.config_fields.archive import ArchiveFields
from services.control_api.app.wiring import Wiring


def test_backfill_config_defaults() -> None:
    fields = ArchiveFields()

    assert fields.memory_vector_backfill_interval_s == 3600.0
    assert fields.memory_vector_backfill_batch == 200


def test_backfill_config_rejects_a_non_positive_interval() -> None:
    with pytest.raises(ValidationError):
        ArchiveFields(MEMORIA_MEMORY_VECTOR_BACKFILL_INTERVAL_S=0)


@pytest.mark.asyncio
async def test_worker_round_logs_counts_without_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen_limits: list[int] = []

    async def backfill(limit: int) -> BackfillReport:
        seen_limits.append(limit)
        return BackfillReport(attempted=2, embedded=1, failed=1, skipped=0, remaining=3)

    worker = MemoryVectorBackfillWorker(backfill, interval_s=10.0, batch_size=7)
    with caplog.at_level(logging.INFO, logger="services.archive.memory_vector_backfill"):
        report = await worker.run_once()

    assert seen_limits == [7]
    assert report == BackfillReport(attempted=2, embedded=1, failed=1, skipped=0, remaining=3)
    assert (
        "memory vector backfill attempted=2 embedded=1 failed=1 skipped=0 remaining=3"
        in caplog.text
    )


def test_worker_is_not_built_when_vectors_are_off() -> None:
    async def backfill(_limit: int) -> BackfillReport:
        return BackfillReport()

    assert (
        build_memory_vector_backfill_worker(
            backfill,
            vector_enabled=False,
            interval_s=3600.0,
            batch_size=200,
        )
        is None
    )
    worker = build_memory_vector_backfill_worker(
        backfill,
        vector_enabled=True,
        interval_s=3600.0,
        batch_size=200,
    )
    assert isinstance(worker, MemoryVectorBackfillWorker)


class _FakeCatalog:
    def __init__(self, *, enabled: bool) -> None:
        self.vector_enabled = enabled
        self.calls: list[tuple[tuple[str, ...], int]] = []

    async def backfill_missing_claim_vectors(
        self, *, account_ids: Any, limit: int
    ) -> BackfillReport:
        self.calls.append((tuple(account_ids), limit))
        return BackfillReport(attempted=0, embedded=0, failed=0, skipped=0, remaining=0)


@pytest.mark.asyncio
async def test_wiring_starts_the_worker_only_when_vectors_are_enabled() -> None:
    app = FastAPI()
    settings = cast(
        Any,
        SimpleNamespace(
            memory_vector_backfill_interval_s=10.0,
            memory_vector_backfill_batch=5,
        ),
    )
    wiring = Wiring(app=app, settings=settings, live=True)
    enabled_catalog = _FakeCatalog(enabled=True)

    worker = wiring.start_memory_vector_backfill(
        app,
        settings,
        cast(Any, enabled_catalog),
        lambda: ("account-a",),
    )

    assert worker is not None
    assert app.state.memory_vector_backfill_worker is worker
    await wiring.resources.aclose()

    off_app = FastAPI()
    off_wiring = Wiring(app=off_app, settings=settings, live=True)
    assert (
        off_wiring.start_memory_vector_backfill(
            off_app,
            settings,
            cast(Any, _FakeCatalog(enabled=False)),
            lambda: ("account-a",),
        )
        is None
    )
    assert off_app.state.memory_vector_backfill_worker is None
