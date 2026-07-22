"""Canonical manifest encoding shared by the SQLite and PostgreSQL adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from services.digital_self.domain import (
    CognitiveClaimManifestEntry,
    DecisionCaseManifestEntry,
    DigitalSelfManifest,
    DigitalSelfSourceSummary,
    EmptyDigitalSelfSourceError,
    ManifestEntry,
    ManifestIntegrityError,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    RelationshipProfileManifestEntry,
    SourceSnapshotConflictError,
)
from services.self_model.domain import (
    CognitiveClaim,
    CognitiveClaimType,
    DecisionCase,
    DecisionKind,
    RelationshipProfile,
    SelfModelSource,
)

V1_MANIFEST_SCHEMA_VERSION = "digital-self-manifest-v1"
MANIFEST_SCHEMA_VERSION = "digital-self-manifest-v2"
DEFAULT_COMPILER_VERSION = "digital-self-compiler-v2"
DEFAULT_POLICY_VERSION = "digital-self-policy-v2"
_SUPPORTED_SCHEMA_VERSIONS = frozenset(
    {V1_MANIFEST_SCHEMA_VERSION, MANIFEST_SCHEMA_VERSION}
)


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestIntegrityError("manifest is not canonical JSON") from exc


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _portable_time(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _number(value: object) -> float:
    if not isinstance(value, (int, float, str)):
        raise TypeError("manifest numeric value is invalid")
    return float(value)


def memory_entry(row: Mapping[str, object]) -> MemoryClaimManifestEntry:
    return MemoryClaimManifestEntry(
        claim_id=str(row["claim_id"]),
        category=str(row["category"]),
        subject_key=str(row["subject_key"]),
        predicate=str(row["predicate"]),
        value=str(row["value"]),
        confidence=_number(row["confidence"]),
        sensitive_domain=str(row["sensitive_domain"]),
        extractor_version=str(row["extractor_version"]),
        source_event_id=str(row["source_event_id"]),
        valid_at=_portable_time(row["valid_at"]),
    )


def persona_entry(
    item: Mapping[str, object],
    *,
    persona_version_id: str,
) -> PersonaTraitManifestEntry:
    raw_source_ids = item.get("source_event_ids")
    if not isinstance(raw_source_ids, list):
        raise SourceSnapshotConflictError("persona snapshot source_event_ids are invalid")
    return PersonaTraitManifestEntry(
        trait_id=str(item["trait_id"]),
        persona_version_id=persona_version_id,
        category=str(item["category"]),
        description=str(item["description"]),
        context=str(item.get("context") or ""),
        counterexample=str(item.get("counterexample") or ""),
        confidence=_number(item["confidence"]),
        source_event_ids=tuple(sorted(str(source_id) for source_id in raw_source_ids)),
    )


def _source_ids(
    sources: Sequence[SelfModelSource],
    relation: str,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            source.source_event_id
            for source in sources
            if source.relation == relation
        )
    )


def cognitive_entry(item: CognitiveClaim) -> CognitiveClaimManifestEntry:
    return CognitiveClaimManifestEntry(
        claim_id=item.claim_id,
        claim_type=item.claim_type,
        statement=item.statement,
        context=item.context,
        confidence=item.confidence,
        sharing_scope=item.sharing_scope,
        support_source_event_ids=_source_ids(item.sources, "support"),
        counterexample_source_event_ids=_source_ids(item.sources, "counterexample"),
    )


def decision_entry(item: DecisionCase) -> DecisionCaseManifestEntry:
    return DecisionCaseManifestEntry(
        case_id=item.case_id,
        kind=item.kind,
        context=item.context,
        options=item.options,
        constraints=item.constraints,
        chosen_option=item.chosen_option,
        rejected_options=item.rejected_options,
        outcome=item.outcome,
        reflection=item.reflection,
        still_endorsed=item.still_endorsed,
        sharing_scope=item.sharing_scope,
        support_source_event_ids=_source_ids(item.sources, "support"),
        counterexample_source_event_ids=_source_ids(item.sources, "counterexample"),
    )


def relationship_entry(
    item: RelationshipProfile,
) -> RelationshipProfileManifestEntry:
    return RelationshipProfileManifestEntry(
        profile_id=item.profile_id,
        version_number=item.version_number,
        person_id=item.person_id,
        relationship_id=item.relationship_id,
        salutation=item.salutation,
        tone=item.tone,
        advice_style=item.advice_style,
        sharing_scope=item.sharing_scope,
        boundaries=item.boundaries,
        support_source_event_ids=_source_ids(item.sources, "support"),
        counterexample_source_event_ids=_source_ids(item.sources, "counterexample"),
    )


def entry_dict(entry: ManifestEntry) -> dict[str, object]:
    if isinstance(entry, MemoryClaimManifestEntry):
        return {
            "type": entry.entry_type,
            "claim_id": entry.claim_id,
            "category": entry.category,
            "subject_key": entry.subject_key,
            "predicate": entry.predicate,
            "value": entry.value,
            "confidence": entry.confidence,
            "sensitive_domain": entry.sensitive_domain,
            "extractor_version": entry.extractor_version,
            "source_event_id": entry.source_event_id,
            "valid_at": entry.valid_at,
        }
    if isinstance(entry, PersonaTraitManifestEntry):
        return {
            "type": entry.entry_type,
            "trait_id": entry.trait_id,
            "persona_version_id": entry.persona_version_id,
            "category": entry.category,
            "description": entry.description,
            "context": entry.context,
            "counterexample": entry.counterexample,
            "confidence": entry.confidence,
            "source_event_ids": list(entry.source_event_ids),
        }
    if isinstance(entry, CognitiveClaimManifestEntry):
        return {
            "type": entry.entry_type,
            "claim_id": entry.claim_id,
            "claim_type": entry.claim_type,
            "statement": entry.statement,
            "context": entry.context,
            "confidence": entry.confidence,
            "sharing_scope": entry.sharing_scope,
            "support_source_event_ids": list(entry.support_source_event_ids),
            "counterexample_source_event_ids": list(
                entry.counterexample_source_event_ids
            ),
        }
    if isinstance(entry, DecisionCaseManifestEntry):
        return {
            "type": entry.entry_type,
            "case_id": entry.case_id,
            "kind": entry.kind,
            "context": entry.context,
            "options": list(entry.options),
            "constraints": list(entry.constraints),
            "chosen_option": entry.chosen_option,
            "rejected_options": list(entry.rejected_options),
            "outcome": entry.outcome,
            "reflection": entry.reflection,
            "still_endorsed": entry.still_endorsed,
            "sharing_scope": entry.sharing_scope,
            "support_source_event_ids": list(entry.support_source_event_ids),
            "counterexample_source_event_ids": list(
                entry.counterexample_source_event_ids
            ),
        }
    return {
        "type": entry.entry_type,
        "profile_id": entry.profile_id,
        "version_number": entry.version_number,
        "person_id": entry.person_id,
        "relationship_id": entry.relationship_id,
        "salutation": entry.salutation,
        "tone": entry.tone,
        "advice_style": entry.advice_style,
        "sharing_scope": entry.sharing_scope,
        "boundaries": list(entry.boundaries),
        "support_source_event_ids": list(entry.support_source_event_ids),
        "counterexample_source_event_ids": list(
            entry.counterexample_source_event_ids
        ),
    }


def entry_sort_key(entry: ManifestEntry) -> tuple[str, str]:
    if isinstance(entry, MemoryClaimManifestEntry):
        return (entry.entry_type, entry.claim_id)
    if isinstance(entry, PersonaTraitManifestEntry):
        return (entry.entry_type, entry.trait_id)
    if isinstance(entry, CognitiveClaimManifestEntry):
        return (entry.entry_type, entry.claim_id)
    if isinstance(entry, DecisionCaseManifestEntry):
        return (entry.entry_type, entry.case_id)
    return (entry.entry_type, f"{entry.profile_id}:{entry.version_number:020d}")


def _source_summary(
    entries: tuple[ManifestEntry, ...],
    *,
    persona_version_id: str | None,
) -> DigitalSelfSourceSummary:
    memory_count = sum(isinstance(entry, MemoryClaimManifestEntry) for entry in entries)
    persona_count = sum(isinstance(entry, PersonaTraitManifestEntry) for entry in entries)
    cognitive_count = sum(
        isinstance(entry, CognitiveClaimManifestEntry) for entry in entries
    )
    decision_count = sum(
        isinstance(entry, DecisionCaseManifestEntry) for entry in entries
    )
    relationship_count = sum(
        isinstance(entry, RelationshipProfileManifestEntry) for entry in entries
    )
    source_bytes = canonical_json_bytes(
        {
            "entries": [entry_dict(entry) for entry in entries],
            "persona_version_id": persona_version_id,
        }
    )
    return DigitalSelfSourceSummary(
        memory_claim_count=memory_count,
        persona_trait_count=persona_count,
        persona_version_id=persona_version_id,
        source_summary_sha256=sha256_hex(source_bytes),
        cognitive_claim_count=cognitive_count,
        decision_case_count=decision_count,
        relationship_profile_count=relationship_count,
    )


def build_manifest(
    entries: Sequence[ManifestEntry],
    *,
    compiler_version: str,
    policy_version: str,
    persona_version_id: str | None,
    parent_version_id: str | None,
    rollback_target_version_id: str | None = None,
    expected_source_summary_sha256: str | None = None,
) -> tuple[DigitalSelfManifest, bytes, str]:
    ordered = tuple(sorted(entries, key=entry_sort_key))
    if not ordered:
        raise EmptyDigitalSelfSourceError("no confirmed owner sources are available")
    summary = _source_summary(ordered, persona_version_id=persona_version_id)
    if (
        expected_source_summary_sha256 is not None
        and expected_source_summary_sha256 != summary.source_summary_sha256
    ):
        raise SourceSnapshotConflictError("source summary changed before build")
    manifest = DigitalSelfManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        compiler_version=compiler_version,
        policy_version=policy_version,
        parent_version_id=parent_version_id,
        rollback_target_version_id=rollback_target_version_id,
        entries=ordered,
        source_summary=summary,
    )
    manifest_bytes = canonical_manifest_bytes(manifest)
    return manifest, manifest_bytes, sha256_hex(manifest_bytes)


def manifest_dict(manifest: DigitalSelfManifest) -> dict[str, object]:
    source_summary: dict[str, object] = {
        "memory_claim_count": manifest.source_summary.memory_claim_count,
        "persona_trait_count": manifest.source_summary.persona_trait_count,
        "persona_version_id": manifest.source_summary.persona_version_id,
        "source_summary_sha256": manifest.source_summary.source_summary_sha256,
    }
    if manifest.schema_version == MANIFEST_SCHEMA_VERSION:
        source_summary.update(
            {
                "cognitive_claim_count": (
                    manifest.source_summary.cognitive_claim_count
                ),
                "decision_case_count": manifest.source_summary.decision_case_count,
                "relationship_profile_count": (
                    manifest.source_summary.relationship_profile_count
                ),
            }
        )
    return {
        "schema_version": manifest.schema_version,
        "compiler_version": manifest.compiler_version,
        "policy_version": manifest.policy_version,
        "parent_version_id": manifest.parent_version_id,
        "rollback_target_version_id": manifest.rollback_target_version_id,
        "entries": [entry_dict(entry) for entry in manifest.entries],
        "source_summary": source_summary,
    }


def canonical_manifest_bytes(manifest: DigitalSelfManifest) -> bytes:
    return canonical_json_bytes(manifest_dict(manifest))


def _entry_from_dict(value: Mapping[str, object]) -> ManifestEntry:
    entry_type = value.get("type")
    try:
        if entry_type == "memory_claim":
            return MemoryClaimManifestEntry(
                claim_id=str(value["claim_id"]),
                category=str(value["category"]),
                subject_key=str(value["subject_key"]),
                predicate=str(value["predicate"]),
                value=str(value["value"]),
                confidence=_number(value["confidence"]),
                sensitive_domain=str(value["sensitive_domain"]),
                extractor_version=str(value["extractor_version"]),
                source_event_id=str(value["source_event_id"]),
                valid_at=str(value["valid_at"]),
            )
        if entry_type == "persona_trait":
            source_ids = value["source_event_ids"]
            if not isinstance(source_ids, list):
                raise TypeError
            return PersonaTraitManifestEntry(
                trait_id=str(value["trait_id"]),
                persona_version_id=str(value["persona_version_id"]),
                category=str(value["category"]),
                description=str(value["description"]),
                context=str(value["context"]),
                counterexample=str(value["counterexample"]),
                confidence=_number(value["confidence"]),
                source_event_ids=tuple(str(item) for item in source_ids),
            )
        if entry_type == "cognitive_claim":
            return CognitiveClaimManifestEntry(
                claim_id=str(value["claim_id"]),
                claim_type=cast(CognitiveClaimType, str(value["claim_type"])),
                statement=str(value["statement"]),
                context=str(value["context"]),
                confidence=_number(value["confidence"]),
                sharing_scope=str(value["sharing_scope"]),
                support_source_event_ids=_string_tuple(
                    value["support_source_event_ids"]
                ),
                counterexample_source_event_ids=_string_tuple(
                    value["counterexample_source_event_ids"]
                ),
            )
        if entry_type == "decision_case":
            return DecisionCaseManifestEntry(
                case_id=str(value["case_id"]),
                kind=cast(DecisionKind, str(value["kind"])),
                context=str(value["context"]),
                options=_string_tuple(value["options"]),
                constraints=_string_tuple(value["constraints"]),
                chosen_option=str(value["chosen_option"]),
                rejected_options=_string_tuple(value["rejected_options"]),
                outcome=str(value["outcome"]),
                reflection=str(value["reflection"]),
                still_endorsed=_boolean(value["still_endorsed"]),
                sharing_scope=str(value["sharing_scope"]),
                support_source_event_ids=_string_tuple(
                    value["support_source_event_ids"]
                ),
                counterexample_source_event_ids=_string_tuple(
                    value["counterexample_source_event_ids"]
                ),
            )
        if entry_type == "relationship_profile":
            return RelationshipProfileManifestEntry(
                profile_id=str(value["profile_id"]),
                version_number=_integer(value["version_number"]),
                person_id=str(value["person_id"]),
                relationship_id=str(value["relationship_id"]),
                salutation=str(value["salutation"]),
                tone=str(value["tone"]),
                advice_style=str(value["advice_style"]),
                sharing_scope=str(value["sharing_scope"]),
                boundaries=_string_tuple(value["boundaries"]),
                support_source_event_ids=_string_tuple(
                    value["support_source_event_ids"]
                ),
                counterexample_source_event_ids=_string_tuple(
                    value["counterexample_source_event_ids"]
                ),
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestIntegrityError("manifest entry is invalid") from exc
    raise ManifestIntegrityError("manifest entry type is invalid")


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError
    return tuple(str(item) for item in value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError
    return value


def _integer(value: object) -> int:
    if not isinstance(value, (int, str)):
        raise TypeError
    return int(value)


def decode_manifest(
    manifest_bytes: bytes,
    *,
    expected_manifest_sha256: str,
    expected_source_summary_sha256: str,
    expected_parent_version_id: str | None,
    expected_rollback_target_version_id: str | None,
) -> DigitalSelfManifest:
    if sha256_hex(manifest_bytes) != expected_manifest_sha256:
        raise ManifestIntegrityError("manifest digest does not match stored bytes")
    try:
        raw = json.loads(manifest_bytes)
        if not isinstance(raw, dict):
            raise TypeError
        raw_entries = raw["entries"]
        raw_summary = raw["source_summary"]
        if not isinstance(raw_entries, list) or not isinstance(raw_summary, dict):
            raise TypeError
        entries = tuple(
            _entry_from_dict(cast(Mapping[str, object], item))
            for item in raw_entries
            if isinstance(item, dict)
        )
        if len(entries) != len(raw_entries):
            raise TypeError
        schema_version = str(raw["schema_version"])
        summary = DigitalSelfSourceSummary(
            memory_claim_count=int(raw_summary["memory_claim_count"]),
            persona_trait_count=int(raw_summary["persona_trait_count"]),
            persona_version_id=(
                str(raw_summary["persona_version_id"])
                if raw_summary.get("persona_version_id") is not None
                else None
            ),
            source_summary_sha256=str(raw_summary["source_summary_sha256"]),
            cognitive_claim_count=(
                int(raw_summary["cognitive_claim_count"])
                if schema_version == MANIFEST_SCHEMA_VERSION
                else 0
            ),
            decision_case_count=(
                int(raw_summary["decision_case_count"])
                if schema_version == MANIFEST_SCHEMA_VERSION
                else 0
            ),
            relationship_profile_count=(
                int(raw_summary["relationship_profile_count"])
                if schema_version == MANIFEST_SCHEMA_VERSION
                else 0
            ),
        )
        manifest = DigitalSelfManifest(
            schema_version=schema_version,
            compiler_version=str(raw["compiler_version"]),
            policy_version=str(raw["policy_version"]),
            parent_version_id=(
                str(raw["parent_version_id"]) if raw.get("parent_version_id") is not None else None
            ),
            rollback_target_version_id=(
                str(raw["rollback_target_version_id"])
                if raw.get("rollback_target_version_id") is not None
                else None
            ),
            entries=entries,
            source_summary=summary,
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestIntegrityError("manifest bytes are invalid") from exc

    if canonical_manifest_bytes(manifest) != manifest_bytes:
        raise ManifestIntegrityError("manifest bytes are not canonical")
    if manifest.schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestIntegrityError("manifest schema version is unsupported")
    if manifest.schema_version == V1_MANIFEST_SCHEMA_VERSION and any(
        isinstance(
            entry,
            (
                CognitiveClaimManifestEntry,
                DecisionCaseManifestEntry,
                RelationshipProfileManifestEntry,
            ),
        )
        for entry in entries
    ):
        raise ManifestIntegrityError("v1 manifest contains v2 entries")
    if tuple(sorted(entries, key=entry_sort_key)) != entries or not entries:
        raise ManifestIntegrityError("manifest entries are empty or not sorted")
    recomputed = _source_summary(entries, persona_version_id=summary.persona_version_id)
    if recomputed != summary or summary.source_summary_sha256 != expected_source_summary_sha256:
        raise ManifestIntegrityError("manifest source summary conflicts with entries")
    if (
        manifest.parent_version_id != expected_parent_version_id
        or manifest.rollback_target_version_id != expected_rollback_target_version_id
    ):
        raise ManifestIntegrityError("manifest lineage conflicts with stored metadata")
    return manifest
