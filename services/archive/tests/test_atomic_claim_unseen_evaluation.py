"""Synthetic atomic claims through the real PG compiler and search projection.

The extractor is deterministic. These metrics do not measure Qwen or embeddings.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import ExtractedClaim, MemoryExtraction
from services.archive.memory_evaluation import (
    CatalogMemoryEvaluationAdapter,
    load_memory_evaluation_dataset,
    report_json,
    run_memory_evaluation,
)


class AtomicToolExtractor:
    version = "synthetic-atomic-tool-v1"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        text = str(event.payload["text"])
        tool = text.split(" use ", 1)[1].rstrip(".")
        return MemoryExtraction(
            claims=(
                ExtractedClaim(
                    subject_key="self",
                    predicate="uses_tool",
                    value=tool,
                    domain_category="work_experience",
                    confidence=0.9,
                ),
            ),
            extractor_version=self.version,
        )


@pytest.mark.parametrize(
    "dataset_name, count",
    [
        ("memory_eval_en_proper_unseen_v1.json", 4),
        ("memory_eval_atomic_fixed_r3.json", 8),
        ("memory_eval_atomic_unseen_r3.json", 8),
    ],
)
async def test_unseen_english_atomic_claims(postgres_database, owner_sql, dataset_name, count):
    owner_sql(f'GRANT CREATE ON DATABASE "{postgres_database.name}" TO memoria_app')
    dataset = load_memory_evaluation_dataset(
        Path(__file__).parents[1] / "evaluation" / dataset_name
    )
    adapter = CatalogMemoryEvaluationAdapter(
        postgres_database.role_dsn("memoria_app"),
        AtomicToolExtractor(),
        compiler_dsn=postgres_database.role_dsn("memoria_compiler"),
    )
    adapter.name = "memoria-postgres-synthetic-atomic-tool-no-embedding"
    report = await run_memory_evaluation(dataset, adapter)
    payload = json.loads(report_json(report))
    destination = os.environ.get("MEMORIA_ATOMIC_EVAL_REPORT")
    if destination:
        Path(destination + "." + dataset_name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )
    assert report.case_count == count
    assert report.metrics.cross_account_leakage == 0
    assert report.metrics.candidate_leakage == 0
    assert report.metrics.extraction_recall == 1
    assert report.metrics.recall_at_5 == 1
