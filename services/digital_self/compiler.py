"""Canonical manifest encoding shared by the SQLite and PostgreSQL adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from services.digital_self.domain import (
    DigitalSelfManifest,
    DigitalSelfSourceSummary,
    EmptyDigitalSelfSourceError,
    ManifestEntry,
    ManifestIntegrityError,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    SourceSnapshotConflictError,
)

MANIFEST_SCHEMA_VERSION = "digital-self-manifest-v1"
DEFAULT_COMPILER_VERSION = "digital-self-compiler-v1"
DEFAULT_POLICY_VERSION = "digital-self-policy-v1"


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


def entry_sort_key(entry: ManifestEntry) -> tuple[str, str]:
    if isinstance(entry, MemoryClaimManifestEntry):
        return (entry.entry_type, entry.claim_id)
    return (entry.entry_type, entry.trait_id)


def _source_summary(
    entries: tuple[ManifestEntry, ...],
    *,
    persona_version_id: str | None,
) -> DigitalSelfSourceSummary:
    memory_count = sum(isinstance(entry, MemoryClaimManifestEntry) for entry in entries)
    persona_count = len(entries) - memory_count
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
    return {
        "schema_version": manifest.schema_version,
        "compiler_version": manifest.compiler_version,
        "policy_version": manifest.policy_version,
        "parent_version_id": manifest.parent_version_id,
        "rollback_target_version_id": manifest.rollback_target_version_id,
        "entries": [entry_dict(entry) for entry in manifest.entries],
        "source_summary": {
            "memory_claim_count": manifest.source_summary.memory_claim_count,
            "persona_trait_count": manifest.source_summary.persona_trait_count,
            "persona_version_id": manifest.source_summary.persona_version_id,
            "source_summary_sha256": manifest.source_summary.source_summary_sha256,
        },
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
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestIntegrityError("manifest entry is invalid") from exc
    raise ManifestIntegrityError("manifest entry type is invalid")


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
        summary = DigitalSelfSourceSummary(
            memory_claim_count=int(raw_summary["memory_claim_count"]),
            persona_trait_count=int(raw_summary["persona_trait_count"]),
            persona_version_id=(
                str(raw_summary["persona_version_id"])
                if raw_summary.get("persona_version_id") is not None
                else None
            ),
            source_summary_sha256=str(raw_summary["source_summary_sha256"]),
        )
        manifest = DigitalSelfManifest(
            schema_version=str(raw["schema_version"]),
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
    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestIntegrityError("manifest schema version is unsupported")
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
