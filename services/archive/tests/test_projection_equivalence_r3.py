"""Exact redundant projections through the production compiler and PostgreSQL."""

import json
import os
from pathlib import Path

from services.archive.memory_domain import (
    ExtractedClaim,
    ExtractedKnowledge,
    ExtractedTimeline,
    MemoryExtraction,
    MemorySearchQuery,
)
from services.archive.tests.test_memory_catalog import _record


class ProjectionExtractor:
    version = "synthetic-projection-equivalence-r3"

    async def extract(self, event):
        text = event.payload["text"]
        return MemoryExtraction(
            claims=tuple(
                ExtractedClaim(
                    subject_key="self",
                    predicate=p,
                    value=text,
                    domain_category="work_experience",
                    confidence=0.9,
                )
                for p in ("uses_tool", "recommends_tool")
            ),
            timeline=(
                ExtractedTimeline(
                    title=text, domain_category="work_experience", event_start=event.occurred_at
                ),
            ),
            knowledge=(
                ExtractedKnowledge(question=text, answer=text, domain_category="work_experience"),
            ),
            extractor_version=self.version,
        )


async def test_equivalent_projections_leave_distinct_claims_and_sources(archive, make_catalog):
    for i, text in enumerate(("Qdrant", "Qdrant PostgreSQL", "Qdrant Obsidian")):
        await _record(archive, event_id=f"equiv-{i}", text=text, minute=i, explicit_memory=True)
    catalog = await make_catalog(extractor=ProjectionExtractor())
    report = await catalog.compile_pending()
    assert report.compiled_events == 3
    result = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="Qdrant",
            include_candidates=True,
            limit=20,
            distinct_facts=True,
        )
    )
    raw = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="Qdrant",
            include_candidates=True,
            limit=20,
        )
    )
    assert len(raw.items) == 10
    if destination := os.environ.get("MEMORIA_R3_DEDUP_REPORT"):
        Path(destination).write_text(
            json.dumps(
                {
                    "extractor": ProjectionExtractor.version,
                    "embedding": "none",
                    "raw_count": len(raw.items),
                    "distinct_count": len(result.items),
                    "sources": 3,
                    "raw_unique_source_ratio": 3 / len(raw.items),
                    "distinct_unique_source_ratio": 3 / len(result.items),
                },
                indent=2,
            )
        )
    assert len(result.items) == 7
    assert sum(item.kind == "claim" for item in result.items) == 6
    assert len(next(item for item in result.items if item.kind != "claim").source_event_ids) == 3
    assert len({item.item_id for item in result.items}) == 7
    assert len({item.source_event_id for item in result.items}) == 3
    assert not (
        await catalog.search(
            MemorySearchQuery(account_id="other", speaker_class="owner", text="Qdrant")
        )
    ).items
