"""PostgreSQL/RLS counterpart to the ledger-derived growth reader."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any, cast

import asyncpg

from services.archive.domain import EvidenceEvent, SpeakerClass
from services.common.evidence_policy import confirmed_projection_contribution_for
from services.growth.domain import GrowthTask, TaskConflictError
from services.growth.policy import (
    DEPENDENCY_PENDING,
    DIMENSIONS,
    GrowthDimension,
    GrowthStatus,
    apply_negative_evidence,
    version_target_ids,
)
from services.growth.tasks import TaskEventAction, apply_task_event
from services.persona.domain import LEGACY_COGNITIVE_TRAIT_CATEGORIES
from services.self_model.domain import (
    CognitiveClaim,
    DecisionCase,
    RelationshipProfile,
    SelfModelItem,
    SelfModelRegistryPort,
)
from services.self_model.policy import activation_decision


class PostgresGrowthReader:
    def __init__(
        self,
        dsn: str,
        *,
        self_model_registry: SelfModelRegistryPort | None = None,
    ) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._self_model_registry = self_model_registry

    async def initialize(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _connection(self) -> asyncpg.Connection:
        await self.initialize()
        assert self._pool is not None
        connection = await self._pool.acquire()
        return connection

    async def tasks(self, *, account_id: str) -> tuple[GrowthTask, ...]:
        connection = await self._connection()
        try:
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)
                rows = await connection.fetch(
                """
                SELECT * FROM archive_evidence_events WHERE account_id = $1
                  AND event_type IN ('learning.task_created', 'learning.task_transitioned', 'owner.action_recorded')
                ORDER BY occurred_at, event_id
                """,
                account_id,
                )
        finally:
            assert self._pool is not None
            await self._pool.release(connection)
        tasks: dict[str, GrowthTask] = {}
        for row in rows:
            payload = _payload(row["payload"])
            task_id = payload.get("task_id")
            kind = payload.get("task_kind")
            if not isinstance(task_id, str) or kind not in {"natural_chat", "life_interview", "scenario_choice", "decision_review"}:
                continue
            action = _task_action(str(row["event_type"]), payload)
            if action is None:
                continue
            try:
                task = apply_task_event(tasks.get(task_id), event_id=str(row["event_id"]), kind=kind, action=action, expected_revision=cast(int | None, payload.get("expected_revision")), occurred_at=cast(datetime, row["occurred_at"]))
            except TaskConflictError:
                continue
            tasks[task_id] = GrowthTask(task.task_id, task.kind, task.status, task.revision, task.event_ids, str(payload.get("prompt_id")) if payload.get("prompt_id") else task.prompt_id, task.created_at, task.updated_at)
        return tuple(sorted(tasks.values(), key=lambda item: (item.created_at or datetime.min.replace(tzinfo=UTC), item.task_id)))

    async def task(self, *, account_id: str, task_id: str) -> GrowthTask | None:
        return next((task for task in await self.tasks(account_id=account_id) if task.task_id == task_id), None)

    async def overview(self, *, account_id: str) -> dict[str, Any]:
        connection = await self._connection()
        try:
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)
                rows = await connection.fetch(
                """
                SELECT claim.claim_id, claim.category, claim.status, claim.value, source.event_id, source.event_type,
                       source.occurred_at, source.speaker_class, source.source, source.payload
                FROM memory_claims AS claim
                JOIN archive_evidence_events AS source ON source.event_id = claim.source_event_id
                WHERE claim.account_id = $1 ORDER BY source.occurred_at, claim.claim_id
                """,
                account_id,
                )
                people = await connection.fetch(
                    "SELECT person.*, source.event_id, source.event_type, source.occurred_at, source.speaker_class, source.source, source.payload FROM person_entities AS person JOIN archive_evidence_events AS source ON source.event_id = person.source_event_id WHERE person.account_id = $1",
                    account_id,
                )
                relationships = await connection.fetch(
                    "SELECT relationship.*, source.event_id, source.event_type, source.occurred_at, source.speaker_class, source.source, source.payload FROM relationships AS relationship JOIN archive_evidence_events AS source ON source.event_id = relationship.source_event_id WHERE relationship.account_id = $1",
                    account_id,
                )
                timelines = await connection.fetch(
                    "SELECT timeline.*, source.event_id, source.event_type, source.occurred_at, source.speaker_class, source.source, source.payload FROM timeline_entries AS timeline JOIN archive_evidence_events AS source ON source.event_id = timeline.source_event_id WHERE timeline.account_id = $1",
                    account_id,
                )
                trait_rows = await connection.fetch(
                    "SELECT trait.trait_id, trait.category, trait.description, source.event_id, source.event_type, source.occurred_at, source.speaker_class, source.source, source.payload FROM persona_traits AS trait JOIN persona_evidence AS pe ON pe.trait_id = trait.trait_id AND pe.account_id = trait.account_id JOIN archive_evidence_events AS source ON source.event_id = pe.source_event_id WHERE trait.account_id = $1 AND trait.status = 'confirmed' ORDER BY trait.trait_id, source.occurred_at, source.event_id",
                    account_id,
                )
                voices = (
                    await connection.fetch(
                        "SELECT * FROM voice_profiles WHERE account_id = $1 ORDER BY version_number DESC",
                        account_id,
                    )
                    if await connection.fetchval("SELECT to_regclass('voice_profiles') IS NOT NULL")
                    else []
                )
                latest = await connection.fetchrow(
                    "SELECT * FROM digital_self_versions WHERE account_id = $1 ORDER BY version_number DESC LIMIT 1",
                    account_id,
                )
                negative_rows = await connection.fetch(
                "SELECT * FROM archive_evidence_events WHERE account_id = $1 AND event_type = 'owner.action_recorded' ORDER BY occurred_at, event_id",
                account_id,
                )
        finally:
            assert self._pool is not None
            await self._pool.release(connection)
        adopted: dict[GrowthDimension, list[dict[str, Any]]] = {key: [] for key in DIMENSIONS}
        rejected: dict[GrowthDimension, Counter[str]] = {key: Counter() for key in DIMENSIONS}
        version_targets: dict[GrowthDimension, set[str]] = {
            key: set() for key in DIMENSIONS
        }
        version_target_events: dict[GrowthDimension, dict[str, set[str]]] = {
            key: {} for key in DIMENSIONS
        }
        life_event_ids: set[str] = set()
        for row in rows:
            if str(row["status"]) != "confirmed":
                rejected["life_chapters"][f"claim_{row['status']}"] += 1
                continue
            event = EvidenceEvent(event_id=str(row["event_id"]), account_id=account_id, event_type=str(row["event_type"]), occurred_at=cast(datetime, row["occurred_at"]), speaker_class=cast(SpeakerClass, str(row["speaker_class"])), source=str(row["source"]), payload=_payload(row["payload"]))
            contribution = confirmed_projection_contribution_for(event)
            if not contribution.accepted:
                rejected["life_chapters"][contribution.reason] += 1
                continue
            claim_id = str(row["claim_id"])
            version_targets["life_chapters"].add(claim_id)
            version_target_events["life_chapters"][claim_id] = {event.event_id}
            if event.event_id in life_event_ids:
                continue
            adopted["life_chapters"].append(_source("memory_claim", claim_id, event, str(row["value"]), contribution.weight))
            life_event_ids.add(event.event_id)
        for row in people:
            event = _event(row, account_id)
            contribution = confirmed_projection_contribution_for(event)
            if str(row["status"]) == "confirmed" and contribution.accepted:
                adopted["important_people"].append(_source("person_entity", str(row["person_id"]), event, str(row["display_name"]), contribution.weight))
            else:
                rejected["important_people"][
                    "person_not_confirmed"
                    if str(row["status"]) != "confirmed"
                    else contribution.reason
                ] += 1
        for row in timelines:
            event = _event(row, account_id)
            contribution = confirmed_projection_contribution_for(event)
            if str(row["status"]) != "confirmed":
                rejected["life_chapters"][f"timeline_{row['status']}"] += 1
            elif not contribution.accepted:
                rejected["life_chapters"][contribution.reason] += 1
            elif event.event_id not in life_event_ids:
                adopted["life_chapters"].append(_source("timeline_entry", str(row["timeline_id"]), event, str(row["title"]), contribution.weight))
                life_event_ids.add(event.event_id)
        for row in relationships:
            event = _event(row, account_id)
            contribution = confirmed_projection_contribution_for(event)
            if str(row["status"]) != "confirmed":
                rejected["relationship_models"][f"relationship_{row['status']}"] += 1
            elif not contribution.accepted:
                rejected["relationship_models"][contribution.reason] += 1
            else:
                rejected["relationship_models"][
                    "relationship_profile_pending_owner_approval"
                ] += 1
        trait_events: dict[str, list[asyncpg.Record]] = {}
        for row in trait_rows:
            trait_events.setdefault(str(row["trait_id"]), []).append(row)
        for trait_id, evidence in trait_events.items():
            events = [_event(row, account_id) for row in evidence]
            category = str(evidence[0]["category"])
            if category in LEGACY_COGNITIVE_TRAIT_CATEGORIES:
                rejected["decision_cases"]["legacy_persona_candidate"] += 1
                continue
            dimension: GrowthDimension = "expression"
            if not events or not all(
                confirmed_projection_contribution_for(event).accepted
                for event in events
            ):
                rejected[dimension]["persona_source_ineligible"] += 1
                continue
            weights = [
                confirmed_projection_contribution_for(event).weight
                for event in events
            ]
            weakest = "weak" if "weak" in weights else "normal" if "normal" in weights else "strong"
            version_targets[dimension].add(trait_id)
            version_target_events[dimension][trait_id] = {
                event.event_id for event in events
            }
            adopted[dimension].append(_source("persona_trait", trait_id, events[0], str(evidence[0]["description"]), weakest))
        for voice in voices:
            if str(voice["status"]) == "active" and str(voice["evaluation_status"]) == "passed" and str(voice["quality_status"]) == "passed":
                event = EvidenceEvent(event_id=str(voice["profile_id"]), account_id=account_id, event_type="voice_profile.ready", occurred_at=cast(datetime, voice["created_at"]), speaker_class="owner", source="voice_profile", payload={})
                adopted["voice"].append(_source("voice_profile", str(voice["profile_id"]), event, "approved voice profile", "strong"))
            elif str(voice["status"]) not in {"revoked", "failed"}:
                rejected["voice"]["voice_not_ready"] += 1
        negatives = [
            {"event_id": str(row["event_id"]), "event_type": str(row["event_type"]), "target_kind": str(payload.get("target_kind") or ""), "target_id": str(payload.get("target_id") or ""), "action": payload["action_type"], "occurred_at": cast(datetime, row["occurred_at"]).isoformat()}
            for row in negative_rows
            if (payload := _payload(row["payload"])).get("action_type") in {"not_me", "would_not_say"}
        ]
        await self._append_self_model_sources(
            account_id=account_id,
            adopted=adopted,
            rejected=rejected,
            version_targets=version_targets,
            version_target_events=version_target_events,
        )
        dimensions: list[dict[str, Any]] = []
        for key in DIMENSIONS:
            sources = adopted[key]
            conflicts, effective_targets = apply_negative_evidence(
                key,
                sources,
                version_target_events[key],
                negatives,
            )
            blockers = ["dependency_pending"] if key in DEPENDENCY_PENDING else []
            status: GrowthStatus = "conflicted" if conflicts else "supported" if sources else "empty"
            dimension_rejected = rejected[key]
            if not sources and dimension_rejected:
                status = "emerging"
            if blockers and sources and not conflicts:
                status = "emerging"
            readiness = _version_readiness(
                key,
                effective_targets,
                dict(latest) if latest is not None else None,
                blockers,
            )
            dimensions.append({"key": key, "status": status, "adopted_sources": sources, "rejected_reason_counts": dict(dimension_rejected), "conflicts": conflicts, "recent_changes": [{field: item[field] for field in ("event_id", "event_type", "occurred_at")} for item in (sources[-5:] + conflicts[-5:])], "dependency_blockers": blockers, "version_readiness": readiness})
        return {"dimensions": dimensions}

    async def _append_self_model_sources(
        self,
        *,
        account_id: str,
        adopted: dict[GrowthDimension, list[dict[str, Any]]],
        rejected: dict[GrowthDimension, Counter[str]],
        version_targets: dict[GrowthDimension, set[str]],
        version_target_events: dict[GrowthDimension, dict[str, set[str]]],
    ) -> None:
        registry = self._self_model_registry
        if registry is None:
            return
        claims = await registry.cognitive_claims(account_id=account_id)
        decisions = await registry.decision_cases(account_id=account_id)
        profiles = await registry.relationship_profiles(account_id=account_id)
        items: tuple[SelfModelItem, ...] = (*claims, *decisions, *profiles)
        profiled_relationships: set[str] = set()
        for item in items:
            if (
                isinstance(item, RelationshipProfile)
                and item.relationship_id not in profiled_relationships
            ):
                profiled_relationships.add(item.relationship_id)
                pending = rejected["relationship_models"].get(
                    "relationship_profile_pending_owner_approval",
                    0,
                )
                if pending == 1:
                    del rejected["relationship_models"][
                        "relationship_profile_pending_owner_approval"
                    ]
                elif pending > 1:
                    rejected["relationship_models"][
                        "relationship_profile_pending_owner_approval"
                    ] -= 1
            dimension: GrowthDimension = (
                "relationship_models"
                if isinstance(item, RelationshipProfile)
                else "decision_cases"
            )
            activation = activation_decision(item)
            if not activation.effective:
                for reason in activation.reasons:
                    rejected[dimension][f"self_model_{reason}"] += 1
                continue
            source = next(
                source
                for source in item.sources
                if source.speaker_class == "owner"
                and source.relation == "support"
                and source.adopted
                and not source.negative
            )
            target_kind, target_id, version_target_id, label = _self_model_identity(item)
            adopted[dimension].append(
                {
                    "kind": "self_model",
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "version_target_id": version_target_id,
                    "event_id": source.source_event_id,
                    "label": label,
                    "weight": "strong",
                    "occurred_at": source.occurred_at.isoformat(),
                    "event_type": "self_model.approved",
                }
            )
            version_targets[dimension].add(version_target_id)
            version_target_events[dimension][version_target_id] = {
                evidence.source_event_id for evidence in item.sources
            }

    async def target_belongs(self, *, account_id: str, target_kind: str, target_id: str) -> bool:
        table, column = {
            "memory_claim": ("memory_claims", "claim_id"),
            "persona_trait": ("persona_traits", "trait_id"),
            "source_event": ("archive_evidence_events", "event_id"),
            "digital_self_version": ("digital_self_versions", "version_id"),
            "person_entity": ("person_entities", "person_id"),
            "timeline_entry": ("timeline_entries", "timeline_id"),
            "relationship": ("relationships", "relationship_id"),
            "voice_profile": ("voice_profiles", "profile_id"),
            "cognitive_claim": ("self_model_cognitive_claims", "claim_id"),
            "decision_case": ("self_model_decision_cases", "case_id"),
            "relationship_profile": ("self_model_relationship_profiles", "profile_id"),
        }.get(target_kind, ("", ""))
        if not table:
            return False
        try:
            value: object = (
                uuid.UUID(target_id)
                if target_kind
                in {
                    "memory_claim",
                    "persona_trait",
                    "digital_self_version",
                    "person_entity",
                    "timeline_entry",
                    "relationship",
                    "voice_profile",
                    "cognitive_claim",
                    "decision_case",
                    "relationship_profile",
                }
                else target_id
            )
        except ValueError:
            return False
        connection = await self._connection()
        try:
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)
                if not await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
                    return False
                return await connection.fetchval(f"SELECT 1 FROM {table} WHERE account_id = $1 AND {column} = $2", account_id, value) is not None
        finally:
            assert self._pool is not None
            await self._pool.release(connection)


def _payload(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        return cast(dict[str, Any], json.loads(value))
    return cast(dict[str, Any], value)


def _task_action(event_type: str, payload: dict[str, Any]) -> TaskEventAction | None:
    if event_type == "learning.task_created":
        return "create"
    if event_type == "learning.task_transitioned":
        value = payload.get("to_status")
        return cast(TaskEventAction, value) if value in {"active", "paused", "completed", "cancelled"} else None
    return "response" if event_type == "owner.action_recorded" and payload.get("task_id") else None


def _dimension(category: str) -> GrowthDimension:
    return "life_chapters" if category == "life_story" else "important_people" if category == "family_principle" else "expression"


def _event(row: asyncpg.Record, account_id: str) -> EvidenceEvent:
    return EvidenceEvent(event_id=str(row["event_id"]), account_id=account_id, event_type=str(row["event_type"]), occurred_at=cast(datetime, row["occurred_at"]), speaker_class=cast(SpeakerClass, str(row["speaker_class"])), source=str(row["source"]), payload=_payload(row["payload"]))


def _source(target_kind: str, target_id: str, event: EvidenceEvent, label: str, weight: str) -> dict[str, str]:
    return {"kind": "evidence", "target_kind": target_kind, "target_id": target_id, "event_id": event.event_id, "label": label, "weight": weight, "occurred_at": event.occurred_at.isoformat(), "event_type": event.event_type}


def _self_model_identity(item: SelfModelItem) -> tuple[str, str, str, str]:
    if isinstance(item, CognitiveClaim):
        return ("cognitive_claim", item.claim_id, item.claim_id, item.statement)
    if isinstance(item, DecisionCase):
        return ("decision_case", item.case_id, item.case_id, item.context)
    version_target_id = f"{item.profile_id}@{item.version_number}"
    return (
        "relationship_profile",
        item.profile_id,
        version_target_id,
        item.salutation,
    )


def _version_readiness(dimension: GrowthDimension, targets: set[str], version: dict[str, Any] | None, blockers: list[str]) -> dict[str, Any]:
    if blockers:
        return {"status": "dependency_pending"}
    if version is None:
        return {"status": "not_built"}
    try:
        raw_manifest = version["manifest_json"]
        manifest = (
            json.loads(raw_manifest)
            if isinstance(raw_manifest, str)
            else cast(dict[str, Any], raw_manifest)
        )
        targets, entry_ids = version_target_ids(dimension, targets, manifest)
    except (json.JSONDecodeError, TypeError):
        return {"status": "stale", "version_id": str(version["version_id"]), "version_status": str(version["status"])}
    if not targets and not entry_ids:
        return {
            "status": "not_built",
            "version_id": str(version["version_id"]),
            "version_status": str(version["status"]),
        }
    ready = targets == entry_ids and str(version["status"]) in {"approved", "frozen"}
    return {"status": "ready" if ready else "stale", "version_id": str(version["version_id"]), "version_status": str(version["status"])}
