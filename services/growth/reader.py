"""SQLite reader that derives S4 map and task state from existing ledger rows."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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


class GrowthReader:
    def __init__(self, path: str) -> None:
        self._path = Path(path).expanduser().resolve()

    @classmethod
    def sqlite(cls, path: str) -> GrowthReader:
        return cls(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    async def tasks(self, *, account_id: str) -> tuple[GrowthTask, ...]:
        if not self._path.is_file():
            return ()
        with self._connect() as connection:
            if not self._table_exists(connection, "evidence_events"):
                return ()
            rows = connection.execute(
                """
                SELECT * FROM evidence_events WHERE account_id = ?
                  AND event_type IN ('learning.task_created', 'learning.task_transitioned', 'owner.action_recorded')
                ORDER BY occurred_at, event_id
                """,
                (account_id,),
            ).fetchall()
        tasks: dict[str, GrowthTask] = {}
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            task_id = payload.get("task_id")
            kind = payload.get("task_kind")
            if not isinstance(task_id, str) or kind not in {"natural_chat", "life_interview", "scenario_choice", "decision_review"}:
                continue
            action = self._task_action(str(row["event_type"]), payload)
            if action is None:
                continue
            try:
                task = apply_task_event(
                    tasks.get(task_id),
                    event_id=str(row["event_id"]),
                    kind=kind,
                    action=action,
                    expected_revision=payload.get("expected_revision"),
                    occurred_at=datetime.fromisoformat(str(row["occurred_at"])).astimezone(UTC),
                )
            except TaskConflictError:
                continue
            tasks[task_id] = GrowthTask(
                task.task_id,
                task.kind,
                task.status,
                task.revision,
                task.event_ids,
                str(payload.get("prompt_id")) if payload.get("prompt_id") else task.prompt_id,
                task.created_at,
                task.updated_at,
            )
        return tuple(sorted(tasks.values(), key=lambda item: (item.created_at or datetime.min.replace(tzinfo=UTC), item.task_id)))

    async def task(self, *, account_id: str, task_id: str) -> GrowthTask | None:
        return next((item for item in await self.tasks(account_id=account_id) if item.task_id == task_id), None)

    async def overview(self, *, account_id: str) -> dict[str, Any]:
        (
            adopted,
            rejected,
            negatives,
            latest_version,
            version_targets,
            version_target_events,
        ) = self._sources(account_id)
        result: list[dict[str, Any]] = []
        for dimension in DIMENSIONS:
            sources = adopted[dimension]
            conflicts, effective_targets = apply_negative_evidence(
                dimension,
                sources,
                version_target_events[dimension],
                negatives,
            )
            status: GrowthStatus = "conflicted" if conflicts else "supported" if sources else "empty"
            dimension_rejected = rejected[dimension]
            if not sources and dimension_rejected:
                status = "emerging"
            blockers = ["dependency_pending"] if dimension in DEPENDENCY_PENDING else []
            if blockers and sources and not conflicts:
                status = "emerging"
            readiness = self._version_readiness(
                dimension,
                effective_targets,
                latest_version,
                blockers,
            )
            result.append(
                {
                    "key": dimension,
                    "status": status,
                    "adopted_sources": sources,
                    "rejected_reason_counts": dict(dimension_rejected),
                    "conflicts": conflicts,
                    "recent_changes": [
                        {key: item[key] for key in ("event_id", "event_type", "occurred_at")}
                        for item in (sources[-5:] + conflicts[-5:])
                    ],
                    "dependency_blockers": blockers,
                    "version_readiness": readiness,
                }
            )
        return {"dimensions": result}

    async def target_belongs(self, *, account_id: str, target_kind: str, target_id: str) -> bool:
        table = {
            "memory_claim": "memory_claims",
            "persona_trait": "persona_traits",
            "source_event": "evidence_events",
            "digital_self_version": "digital_self_versions",
            "person_entity": "person_entities",
            "timeline_entry": "timeline_entries",
            "relationship": "relationships",
            "voice_profile": "voice_profiles",
        }.get(target_kind)
        if table is None or not self._path.is_file():
            return False
        with self._connect() as connection:
            if not self._table_exists(connection, table):
                return False
            column = {
                "memory_claim": "claim_id",
                "persona_trait": "trait_id",
                "source_event": "event_id",
                "digital_self_version": "version_id",
                "person_entity": "person_id",
                "timeline_entry": "timeline_id",
                "relationship": "relationship_id",
                "voice_profile": "profile_id",
            }[target_kind]
            return connection.execute(
                f"SELECT 1 FROM {table} WHERE account_id = ? AND {column} = ?",
                (account_id, target_id),
            ).fetchone() is not None

    @staticmethod
    def _task_action(event_type: str, payload: dict[str, Any]) -> TaskEventAction | None:
        if event_type == "learning.task_created":
            return "create"
        if event_type == "learning.task_transitioned":
            value = payload.get("to_status")
            return value if value in {"active", "paused", "completed", "cancelled"} else None
        if event_type == "owner.action_recorded" and payload.get("task_id"):
            return "response"
        return None

    def _sources(
        self,
        account_id: str,
    ) -> tuple[
        dict[GrowthDimension, list[dict[str, Any]]],
        dict[GrowthDimension, Counter[str]],
        list[dict[str, Any]],
        dict[str, Any] | None,
        dict[GrowthDimension, set[str]],
        dict[GrowthDimension, dict[str, set[str]]],
    ]:
        adopted: dict[GrowthDimension, list[dict[str, Any]]] = {key: [] for key in DIMENSIONS}
        rejected: dict[GrowthDimension, Counter[str]] = {key: Counter() for key in DIMENSIONS}
        version_targets: dict[GrowthDimension, set[str]] = {
            key: set() for key in DIMENSIONS
        }
        version_target_events: dict[GrowthDimension, dict[str, set[str]]] = {
            key: {} for key in DIMENSIONS
        }
        life_event_ids: set[str] = set()
        if not self._path.is_file():
            return (
                adopted,
                rejected,
                [],
                None,
                version_targets,
                version_target_events,
            )
        with self._connect() as connection:
            if not self._table_exists(connection, "memory_claims"):
                return (
                    adopted,
                    rejected,
                    [],
                    None,
                    version_targets,
                    version_target_events,
                )
            rows = connection.execute(
                """
                SELECT claim.claim_id, claim.category, claim.status, claim.value,
                       claim.source_event_id, source.*
                FROM memory_claims AS claim
                JOIN evidence_events AS source ON source.event_id = claim.source_event_id
                WHERE claim.account_id = ?
                ORDER BY source.occurred_at, claim.claim_id
                """,
                (account_id,),
            ).fetchall()
            people = connection.execute(
                "SELECT person.*, source.* FROM person_entities AS person JOIN evidence_events AS source ON source.event_id = person.source_event_id WHERE person.account_id = ?",
                (account_id,),
            ).fetchall()
            timelines = connection.execute(
                "SELECT timeline.*, source.* FROM timeline_entries AS timeline JOIN evidence_events AS source ON source.event_id = timeline.source_event_id WHERE timeline.account_id = ?",
                (account_id,),
            ).fetchall()
            relationships = connection.execute(
                "SELECT relationship.*, source.* FROM relationships AS relationship JOIN evidence_events AS source ON source.event_id = relationship.source_event_id WHERE relationship.account_id = ?",
                (account_id,),
            ).fetchall()
            traits = (
                connection.execute(
                    "SELECT * FROM persona_traits WHERE account_id = ? AND status = 'confirmed'",
                    (account_id,),
                ).fetchall()
                if self._table_exists(connection, "persona_traits")
                else []
            )
            negatives = self._negative_actions_with_connection(connection, account_id)
            latest = (
                connection.execute(
                    "SELECT * FROM digital_self_versions WHERE account_id = ? ORDER BY version_number DESC LIMIT 1",
                    (account_id,),
                ).fetchone()
                if self._table_exists(connection, "digital_self_versions")
                else None
            )
            voices = (
                connection.execute(
                    "SELECT * FROM voice_profiles WHERE account_id = ? ORDER BY version_number DESC",
                    (account_id,),
                ).fetchall()
                if self._table_exists(connection, "voice_profiles")
                else []
            )
        for row in rows:
            if str(row["status"]) != "confirmed":
                rejected["life_chapters"][f"claim_{row['status']}"] += 1
                continue
            event = EvidenceEvent(
                event_id=str(row["event_id"]), account_id=account_id, event_type=str(row["event_type"]),
                occurred_at=datetime.fromisoformat(str(row["occurred_at"])).astimezone(UTC),
                speaker_class=cast(SpeakerClass, str(row["speaker_class"])), source=str(row["source"]),
                payload=json.loads(str(row["payload_json"])),
            )
            contribution = confirmed_projection_contribution_for(event)
            if not contribution.accepted:
                rejected["life_chapters"][contribution.reason] += 1
                continue
            claim_id = str(row["claim_id"])
            version_targets["life_chapters"].add(claim_id)
            version_target_events["life_chapters"][claim_id] = {event.event_id}
            if event.event_id in life_event_ids:
                continue
            adopted["life_chapters"].append(self._source("memory_claim", claim_id, event, str(row["value"]), contribution.weight))
            life_event_ids.add(event.event_id)
        for row in people:
            event = self._event_from_row(row, account_id)
            contribution = confirmed_projection_contribution_for(event)
            if str(row["status"]) == "confirmed" and contribution.accepted:
                adopted["important_people"].append(self._source("person_entity", str(row["person_id"]), event, str(row["display_name"]), contribution.weight))
            else:
                rejected["important_people"][
                    "person_not_confirmed"
                    if str(row["status"]) != "confirmed"
                    else contribution.reason
                ] += 1
        for row in timelines:
            event = self._event_from_row(row, account_id)
            contribution = confirmed_projection_contribution_for(event)
            if str(row["status"]) != "confirmed":
                rejected["life_chapters"][f"timeline_{row['status']}"] += 1
            elif not contribution.accepted:
                rejected["life_chapters"][contribution.reason] += 1
            elif event.event_id not in life_event_ids:
                adopted["life_chapters"].append(self._source("timeline_entry", str(row["timeline_id"]), event, str(row["title"]), contribution.weight))
                life_event_ids.add(event.event_id)
        for row in relationships:
            event = self._event_from_row(row, account_id)
            contribution = confirmed_projection_contribution_for(event)
            if str(row["status"]) != "confirmed":
                rejected["relationship_models"][f"relationship_{row['status']}"] += 1
            elif not contribution.accepted:
                rejected["relationship_models"][contribution.reason] += 1
            else:
                rejected["relationship_models"][
                    "relationship_profile_pending_owner_approval"
                ] += 1
        with self._connect() as connection:
            for trait in traits:
                evidence = connection.execute(
                    "SELECT source.* FROM persona_evidence AS pe JOIN evidence_events AS source ON source.event_id = pe.source_event_id WHERE pe.account_id = ? AND pe.trait_id = ? ORDER BY source.occurred_at, source.event_id",
                    (account_id, trait["trait_id"]),
                ).fetchall()
                events = [self._event_from_row(row, account_id) for row in evidence]
                category = str(trait["category"])
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
                trait_id = str(trait["trait_id"])
                version_targets[dimension].add(trait_id)
                version_target_events[dimension][trait_id] = {
                    event.event_id for event in events
                }
                adopted[dimension].append(self._source("persona_trait", trait_id, events[0], str(trait["description"]), weakest))
        for voice in voices:
            voice_status = str(voice["status"])
            evaluation = str(voice["evaluation_status"])
            quality = str(voice["quality_status"])
            if voice_status == "active" and evaluation == "passed" and quality == "passed":
                event = EvidenceEvent(event_id=str(voice["profile_id"]), account_id=account_id, event_type="voice_profile.ready", occurred_at=datetime.fromisoformat(str(voice["created_at"])).astimezone(UTC), speaker_class="owner", source="voice_profile", payload={})
                adopted["voice"].append(self._source("voice_profile", str(voice["profile_id"]), event, "approved voice profile", "strong"))
            elif voice_status not in {"revoked", "failed"}:
                # An incomplete profile is an observable readiness signal, not an adopted identity fact.
                rejected["voice"]["voice_not_ready"] += 1
        latest_version = dict(latest) if latest is not None else None
        return (
            adopted,
            rejected,
            negatives,
            latest_version,
            version_targets,
            version_target_events,
        )

    def _negative_actions_with_connection(self, connection: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
                    "SELECT * FROM evidence_events WHERE account_id = ? AND event_type = 'owner.action_recorded' ORDER BY occurred_at, event_id",
                    (account_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if payload.get("action_type") in {"not_me", "would_not_say"}:
                result.append({"event_id": str(row["event_id"]), "event_type": str(row["event_type"]), "target_kind": str(payload.get("target_kind") or ""), "target_id": str(payload.get("target_id") or ""), "action": payload["action_type"], "occurred_at": str(row["occurred_at"])})
        return result

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    @staticmethod
    def _event_from_row(row: sqlite3.Row, account_id: str) -> EvidenceEvent:
        return EvidenceEvent(event_id=str(row["event_id"]), account_id=account_id, event_type=str(row["event_type"]), occurred_at=datetime.fromisoformat(str(row["occurred_at"])).astimezone(UTC), speaker_class=cast(SpeakerClass, str(row["speaker_class"])), source=str(row["source"]), payload=json.loads(str(row["payload_json"])))

    @staticmethod
    def _source(target_kind: str, target_id: str, event: EvidenceEvent, label: str, weight: str) -> dict[str, str]:
        return {"kind": "evidence", "target_kind": target_kind, "target_id": target_id, "event_id": event.event_id, "label": label, "weight": weight, "occurred_at": event.occurred_at.isoformat(), "event_type": event.event_type}

    @staticmethod
    def _version_readiness(dimension: GrowthDimension, targets: set[str], version: dict[str, Any] | None, blockers: list[str]) -> dict[str, Any]:
        if blockers:
            return {"status": "dependency_pending"}
        if version is None:
            return {"status": "not_built"}
        try:
            manifest = json.loads(str(version["manifest_json"]))
            if not isinstance(manifest, dict):
                raise TypeError
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
