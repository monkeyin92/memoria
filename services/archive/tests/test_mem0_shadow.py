from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from services.archive.mem0_shadow import (
    Mem0ShadowEvaluationAdapter,
    Mem0ShadowUnavailableError,
    ShadowEvaluationStore,
)
from services.archive.memory_evaluation import (
    load_memory_evaluation_dataset,
    run_memory_evaluation,
)

DATASET = (
    Path(__file__).parents[1] / "evaluation" / "memory_eval_zh_v1.json"
)


class FakeMem0:
    def __init__(self) -> None:
        self.by_user: dict[str, list[dict[str, object]]] = {}

    def add(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        user_id: str,
        metadata: Mapping[str, object],
    ) -> object:
        text = messages[0]["content"]
        item = {
            "id": f"{user_id}:{len(self.by_user.get(user_id, []))}",
            "memory": text,
            "metadata": dict(metadata),
        }
        self.by_user.setdefault(user_id, []).append(item)
        return {"results": [item], "usage": {"input_tokens": 4, "output_tokens": 2}}

    def search(self, query: str, *, user_id: str, limit: int) -> object:
        del query
        return {"results": self.by_user.get(user_id, [])[:limit]}


class FailingMem0(FakeMem0):
    def add(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        user_id: str,
        metadata: Mapping[str, object],
    ) -> object:
        del messages, user_id, metadata
        raise TimeoutError("shadow provider timeout")


@pytest.mark.asyncio
async def test_mem0_shadow_uses_hashed_isolated_namespaces_and_never_crosses_accounts() -> None:
    dataset = load_memory_evaluation_dataset(DATASET)
    case = next(
        value
        for value in dataset.cases
        if value.scenario == "cross_account_isolation"
    )
    client = FakeMem0()
    adapter = Mem0ShadowEvaluationAdapter(
        config={"vector_store": {"provider": "fake"}},
        experiment_id="shadow-ab-001",
        client_factory=lambda _: client,
    )

    observation = await adapter.observe(case)

    assert len(client.by_user) == 2
    assert all(user.startswith("memoria-shadow:shadow-ab-001:") for user in client.by_user)
    assert all("eval-owner" not in user for user in client.by_user)
    result = observation.query_results[0]
    assert all(item.account_id == "eval-owner-a" for item in result.items)
    assert all("拉萨" not in item.body for item in result.items)


@pytest.mark.asyncio
async def test_mem0_shadow_isolates_cases_that_share_the_same_synthetic_account() -> None:
    dataset = load_memory_evaluation_dataset(DATASET)
    cases = tuple(
        case
        for case in dataset.cases
        if case.evidence and case.evidence[0].account_id == "eval-owner-a"
    )[:2]
    client = FakeMem0()
    adapter = Mem0ShadowEvaluationAdapter(
        config={"vector_store": {"provider": "fake"}},
        experiment_id="shadow-case-isolation",
        client_factory=lambda _: client,
    )

    for case in cases:
        await adapter.observe(case)

    assert len(cases) == 2
    assert len(client.by_user) == 2


@pytest.mark.asyncio
async def test_mem0_failure_is_reported_without_raising_into_the_primary_evaluation() -> None:
    dataset = load_memory_evaluation_dataset(DATASET)
    adapter = Mem0ShadowEvaluationAdapter(
        config={},
        experiment_id="shadow-failure",
        client_factory=lambda _: FailingMem0(),
    )

    report = await run_memory_evaluation(dataset, adapter)

    assert len(report.failed_cases) == len(dataset.cases)
    assert report.metrics.extraction_recall == 0
    assert report.metrics.cross_account_leakage == 0


def test_mem0_dependency_is_lazy_and_reports_an_actionable_diagnostic() -> None:
    def unavailable(_: Mapping[str, object]) -> FakeMem0:
        raise ModuleNotFoundError("mem0")

    with pytest.raises(Mem0ShadowUnavailableError, match="mem0ai"):
        Mem0ShadowEvaluationAdapter(
            config={},
            experiment_id="missing",
            client_factory=unavailable,
        )


@pytest.mark.asyncio
async def test_shadow_report_store_is_separate_from_authoritative_memory_tables(
    tmp_path: Path,
) -> None:
    dataset = load_memory_evaluation_dataset(DATASET)
    report = await run_memory_evaluation(
        dataset,
        Mem0ShadowEvaluationAdapter(
            config={},
            experiment_id="shadow-store",
            client_factory=lambda _: FakeMem0(),
        ),
    )
    path = tmp_path / "shadow.sqlite3"

    run_id = ShadowEvaluationStore(path).record(
        experiment_id="shadow-store",
        report=report,
    )

    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        stored = connection.execute(
            "SELECT provider, dataset_version FROM shadow_evaluation_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert tables == {"shadow_evaluation_runs"}
    assert stored == ("mem0-shadow", "memory-eval-zh-v1")
