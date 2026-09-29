"""Shared frozen Digital Self and relationship snapshots for Legacy tests."""

from __future__ import annotations

from datetime import UTC, datetime

from services.digital_self.compiler import build_manifest
from services.digital_self.domain import (
    DigitalSelfVersion,
    MemoryClaimManifestEntry,
    RelationshipProfileManifestEntry,
)
from services.self_model.domain import RelationshipProfile

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)


def snapshots() -> tuple[DigitalSelfVersion, RelationshipProfile]:
    relationship_entry = RelationshipProfileManifestEntry(
        profile_id="11111111-1111-4111-8111-111111111111",
        version_number=3,
        person_id="person-1",
        relationship_id="22222222-2222-4222-8222-222222222222",
        salutation="小梅",
        tone="warm",
        advice_style="listen-first",
        sharing_scope="family",
        boundaries=("不替代专业意见",),
        support_source_event_ids=("relationship-source",),
        counterexample_source_event_ids=(),
    )
    memory_entry = MemoryClaimManifestEntry(
        claim_id="memory-1",
        category="life_story",
        subject_key="owner",
        predicate="lived_in",
        value="杭州",
        confidence=0.95,
        sensitive_domain="family",
        extractor_version="v1",
        source_event_id="memory-source",
        valid_at=NOW.isoformat(),
    )
    manifest, _, manifest_sha256 = build_manifest(
        (memory_entry, relationship_entry),
        compiler_version="digital-self-compiler-v3",
        policy_version="digital-self-policy-v3",
        persona_version_id=None,
        parent_version_id=None,
    )
    version = DigitalSelfVersion(
        version_id="33333333-3333-4333-8333-333333333333",
        account_id="owner-a",
        version_number=7,
        status="frozen",
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        created_at=NOW,
    )
    relationship = RelationshipProfile(
        profile_id="11111111-1111-4111-8111-111111111111",
        account_id="owner-a",
        version_number=3,
        person_id="person-1",
        relationship_id="22222222-2222-4222-8222-222222222222",
        salutation="小梅",
        tone="warm",
        advice_style="listen-first",
        sharing_scope="family",
        boundaries=("不替代专业意见",),
        status="approved",
        unresolved_conflict=False,
        sources=(),
        owner_reviewed_at=NOW,
        step_up_verified=True,
        created_at=NOW,
    )
    return version, relationship
