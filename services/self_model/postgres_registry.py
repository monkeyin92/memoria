"""PostgreSQL/RLS registry for evidence-backed self-model material."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import asyncpg

from services.self_model.domain import (
    CognitiveClaim,
    CognitiveClaimType,
    DecisionCase,
    DecisionKind,
    InvalidSelfModelTransitionError,
    ItemStatus,
    RelationshipProfile,
    RelationshipProfileStatus,
    RelationshipReferenceError,
    SelfModelIdempotencyConflictError,
    SelfModelItem,
    SelfModelItemKind,
    SelfModelNotFoundError,
    SelfModelSource,
    SelfModelVersionConflictError,
    SourceInput,
    SourceRelation,
    UntrustedSelfModelSourceError,
)
from services.self_model.policy import HIGH_SENSITIVITY_CLAIM_TYPES, activation_decision

_ITEM_TRANSITIONS: dict[ItemStatus, frozenset[ItemStatus]] = {
    "candidate": frozenset({"confirmed", "disputed", "retracted", "superseded"}),
    "confirmed": frozenset({"disputed", "retracted", "superseded"}),
    "disputed": frozenset({"confirmed", "retracted", "superseded"}),
    "retracted": frozenset(),
    "superseded": frozenset(),
}

_ITEM_TABLES: dict[SelfModelItemKind, tuple[str, str, str]] = {
    "cognitive_claim": (
        "self_model_cognitive_claims",
        "self_model_cognitive_claim_sources",
        "claim_id",
    ),
    "decision_case": (
        "self_model_decision_cases",
        "self_model_decision_case_sources",
        "case_id",
    ),
    "relationship_profile": (
        "self_model_relationship_profiles",
        "self_model_relationship_profile_sources",
        "profile_id",
    ),
}
_EXPORT_TABLES = (
    "self_model_cognitive_claims",
    "self_model_cognitive_claim_sources",
    "self_model_decision_cases",
    "self_model_decision_case_sources",
    "self_model_relationship_profiles",
    "self_model_relationship_profile_sources",
    "self_model_audit_events",
    "self_model_command_receipts",
)
_DELETE_ORDER = (
    "self_model_cognitive_claim_sources",
    "self_model_decision_case_sources",
    "self_model_relationship_profile_sources",
    "self_model_cognitive_claims",
    "self_model_decision_cases",
    "self_model_relationship_profiles",
    "self_model_audit_events",
    "self_model_command_receipts",
)


def _source_inputs(sources: tuple[SourceInput, ...]) -> tuple[SourceInput, ...]:
    normalized: list[SourceInput] = []
    seen: set[str] = set()
    for source in sources:
        source_event_id = source.source_event_id.strip()
        if not source_event_id or len(source_event_id) > 128:
            raise ValueError("source_event_id is required")
        if source.relation not in {"support", "counterexample"}:
            raise ValueError("source relation is invalid")
        if source.adopted and source.relation != "support":
            raise ValueError("only supporting evidence can be adopted")
        if source_event_id in seen:
            raise ValueError("duplicate source")
        seen.add(source_event_id)
        normalized.append(
            SourceInput(
                source_event_id=source_event_id,
                relation=source.relation,
                adopted=source.adopted,
                negative=source.negative,
            )
        )
    return tuple(normalized)


def _source_payloads(sources: tuple[SourceInput, ...]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "source_event_id": source.source_event_id,
            "relation": source.relation,
            "adopted": source.adopted,
            "negative": source.negative,
        }
        for source in sources
    )


class PostgresSelfModelRegistry:
    """Keeps candidate material and derives effective material on every read."""

    def __init__(self, dsn: str) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("self model DSN must use PostgreSQL")
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10, command_timeout=15)
        if pool is None:  # pragma: no cover
            raise RuntimeError("failed to create PostgreSQL self model pool")
        root = Path(__file__).parents[1]
        schemas = (
            root / "archive" / "postgres_schema.sql",
            root / "archive" / "postgres_memory_schema.sql",
            Path(__file__).with_name("postgres_schema.sql"),
        )
        try:
            async with pool.acquire() as connection:
                for schema in schemas:
                    await connection.execute(schema.read_text(encoding="utf-8"))
        except Exception:
            await pool.close()
            raise
        self._pool = pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ready_pool(self) -> asyncpg.Pool:
        await self.initialize()
        if self._pool is None:  # pragma: no cover
            raise RuntimeError("PostgreSQL self model registry is not initialized")
        return self._pool

    @staticmethod
    async def _scope(connection: asyncpg.Connection, account_id: str) -> None:
        if not account_id.strip():
            raise ValueError("account_id is required")
        await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)

    @staticmethod
    async def _lock_account(connection: asyncpg.Connection, account_id: str) -> None:
        await connection.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"memoria-self-model:{account_id}",
        )

    async def create_cognitive_claim(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        claim_type: CognitiveClaimType,
        statement: str,
        context: str = "",
        confidence: float = 0.5,
        sharing_scope: str = "private",
        unresolved_conflict: bool = False,
        sources: tuple[SourceInput, ...] = (),
    ) -> CognitiveClaim:
        self._require_text(idempotency_key, "idempotency_key")
        self._require_text(statement, "statement")
        self._require_scope(sharing_scope)
        self._require_confidence(confidence)
        sources = _source_inputs(sources)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            payload = {"claim_type": claim_type, "statement": statement, "context": context,
                       "confidence": confidence, "sharing_scope": sharing_scope,
                       "unresolved_conflict": unresolved_conflict}
            if sources:
                payload["sources"] = _source_payloads(sources)
            duplicate = await self._duplicate(
                connection, account_id, idempotency_key, "create_cognitive_claim", payload
            )
            if duplicate is not None:
                return cast(CognitiveClaim, duplicate)
            claim_id = uuid.uuid4()
            await connection.execute(
                """
                INSERT INTO self_model_cognitive_claims (
                    claim_id, account_id, claim_type, statement, context, confidence,
                    sharing_scope, unresolved_conflict
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                claim_id,
                account_id,
                claim_type,
                statement,
                context,
                confidence,
                sharing_scope,
                unresolved_conflict,
            )
            await self._audit(connection, account_id, "create_claim", "cognitive_claim", claim_id)
            result_version = await self._attach_creation_sources(
                connection,
                account_id=account_id,
                item_kind="cognitive_claim",
                item_id=claim_id,
                sources=sources,
            )
            result = cast(
                CognitiveClaim,
                await self._get_item(connection, account_id, "cognitive_claim", claim_id),
            )
            await self._receipt(
                connection, account_id, idempotency_key, "create_cognitive_claim",
                payload, "cognitive_claim", claim_id, result_version
            )
            return result

    async def create_decision_case(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        kind: DecisionKind,
        context: str,
        options: Iterable[str],
        constraints: Iterable[str],
        chosen_option: str,
        rejected_options: Iterable[str] = (),
        outcome: str = "",
        reflection: str = "",
        still_endorsed: bool = True,
        sharing_scope: str = "private",
        unresolved_conflict: bool = False,
        sources: tuple[SourceInput, ...] = (),
    ) -> DecisionCase:
        self._require_text(idempotency_key, "idempotency_key")
        self._require_text(context, "context")
        self._require_text(chosen_option, "chosen_option")
        self._require_scope(sharing_scope)
        options_tuple = self._string_tuple(options, "options")
        constraints_tuple = self._string_tuple(constraints, "constraints")
        rejected_tuple = self._string_tuple(rejected_options, "rejected_options")
        sources = _source_inputs(sources)
        if not options_tuple:
            raise ValueError("options must not be empty")
        if chosen_option not in options_tuple:
            raise ValueError("chosen_option must be one of options")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            payload = {
                "kind": kind, "context": context, "options": options_tuple,
                "constraints": constraints_tuple, "chosen_option": chosen_option,
                "rejected_options": rejected_tuple, "outcome": outcome,
                "reflection": reflection, "still_endorsed": still_endorsed,
                "sharing_scope": sharing_scope, "unresolved_conflict": unresolved_conflict,
            }
            if sources:
                payload["sources"] = _source_payloads(sources)
            duplicate = await self._duplicate(
                connection, account_id, idempotency_key, "create_decision_case", payload
            )
            if duplicate is not None:
                return cast(DecisionCase, duplicate)
            case_id = uuid.uuid4()
            await connection.execute(
                """
                INSERT INTO self_model_decision_cases (
                    case_id, account_id, kind, context, options,
                    constraints, chosen_option, rejected_options, outcome, reflection,
                    still_endorsed, sharing_scope, unresolved_conflict
                ) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7,
                          $8::jsonb, $9, $10, $11, $12, $13)
                """,
                case_id,
                account_id,
                kind,
                context,
                json.dumps(options_tuple, ensure_ascii=False),
                json.dumps(constraints_tuple, ensure_ascii=False),
                chosen_option,
                json.dumps(rejected_tuple, ensure_ascii=False),
                outcome,
                reflection,
                still_endorsed,
                sharing_scope,
                unresolved_conflict,
            )
            await self._audit(
                connection, account_id, "create_decision_case", "decision_case", case_id
            )
            result_version = await self._attach_creation_sources(
                connection,
                account_id=account_id,
                item_kind="decision_case",
                item_id=case_id,
                sources=sources,
            )
            result = cast(
                DecisionCase,
                await self._get_item(connection, account_id, "decision_case", case_id),
            )
            await self._receipt(
                connection, account_id, idempotency_key, "create_decision_case",
                payload, "decision_case", case_id, result_version
            )
            return result

    async def create_relationship_profile(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        person_id: str,
        relationship_id: str,
        salutation: str,
        tone: str,
        advice_style: str,
        sharing_scope: str = "private",
        boundaries: Iterable[str],
        unresolved_conflict: bool = False,
        sources: tuple[SourceInput, ...] = (),
    ) -> RelationshipProfile:
        self._require_text(idempotency_key, "idempotency_key")
        self._require_scope(sharing_scope)
        person_uuid, relationship_uuid = self._uuid(person_id), self._uuid(relationship_id)
        boundaries_tuple = self._string_tuple(boundaries, "boundaries")
        sources = _source_inputs(sources)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            payload = {
                "person_id": person_id, "relationship_id": relationship_id,
                "salutation": salutation, "tone": tone, "advice_style": advice_style,
                "sharing_scope": sharing_scope, "boundaries": boundaries_tuple,
                "unresolved_conflict": unresolved_conflict,
            }
            if sources:
                payload["sources"] = _source_payloads(sources)
            duplicate = await self._duplicate(
                connection, account_id, idempotency_key, "create_relationship_profile", payload
            )
            if duplicate is not None:
                return cast(RelationshipProfile, duplicate)
            valid = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM person_entities WHERE person_id = $1 AND account_id = $3
                ) AND EXISTS (
                    SELECT 1 FROM relationships
                    WHERE relationship_id = $2 AND person_id = $1 AND account_id = $3
                )
                """,
                person_uuid,
                relationship_uuid,
                account_id,
            )
            if not valid:
                raise RelationshipReferenceError("person and relationship must belong to the account")
            version_number = 1
            profile_id = uuid.uuid4()
            await connection.execute(
                """
                INSERT INTO self_model_relationship_profiles (
                    profile_id, account_id, version_number, person_id,
                    relationship_id, salutation, tone, advice_style, sharing_scope, boundaries
                    , unresolved_conflict
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
                """,
                profile_id,
                account_id,
                version_number,
                person_uuid,
                relationship_uuid,
                salutation,
                tone,
                advice_style,
                sharing_scope,
                json.dumps(boundaries_tuple, ensure_ascii=False),
                unresolved_conflict,
            )
            await self._audit(
                connection, account_id, "create_relationship_profile", "relationship_profile", profile_id
            )
            await self._attach_creation_sources(
                connection,
                account_id=account_id,
                item_kind="relationship_profile",
                item_id=profile_id,
                sources=sources,
            )
            row = await connection.fetchrow(
                """SELECT * FROM self_model_relationship_profiles
                   WHERE profile_id = $1 AND account_id = $2 AND version_number = 1""",
                profile_id,
                account_id,
            )
            assert row is not None  # transaction-local insert
            result = await self._profile(connection, account_id, row)
            await self._receipt(
                connection, account_id, idempotency_key, "create_relationship_profile",
                payload, "relationship_profile", profile_id, 1
            )
            return result

    async def add_source(
        self,
        *,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: str,
        source_event_id: str,
        relation: SourceRelation,
        adopted: bool,
        negative: bool,
        expected_version: int,
        idempotency_key: str,
    ) -> SelfModelItem:
        source_input = _source_inputs(
            (
                SourceInput(
                    source_event_id=source_event_id,
                    relation=relation,
                    adopted=adopted,
                    negative=negative,
                ),
            )
        )[0]
        pool = await self._ready_pool()
        item_uuid = self._uuid(item_id)
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            payload = {
                "item_kind": item_kind, "item_id": item_id,
                **_source_payloads((source_input,))[0],
                "expected_version": expected_version,
            }
            duplicate = await self._duplicate(
                connection, account_id, idempotency_key, "add_source", payload
            )
            if duplicate is not None:
                return duplicate
            if item_kind != "relationship_profile":
                current = await self._require_item(connection, account_id, item_kind, item_uuid)
                if expected_version != int(current["version"]):
                    raise SelfModelVersionConflictError(item_id)
            else:
                current_profile = await self._profile_by_id(
                    connection, account_id, item_uuid, expected_version
                )
                if current_profile.status != "candidate" and not negative:
                    raise InvalidSelfModelTransitionError(
                        "only negative evidence can be added after relationship approval"
                    )
            source = await self._trusted_source(
                connection,
                account_id=account_id,
                source=source_input,
            )
            await self._add_sources(connection, account_id, item_kind, item_uuid, (source,))
            if item_kind != "relationship_profile":
                table, _, primary = _ITEM_TABLES[item_kind]
                await connection.execute(
                    f"""
                    UPDATE {table} SET version = version + 1, updated_at = now()
                    WHERE {primary} = $1 AND account_id = $2 AND version = $3
                    """,
                    item_uuid,
                    account_id,
                    expected_version,
                )
            await self._audit(connection, account_id, "add_source", item_kind, item_uuid)
            result = (
                await self._profile_by_id(connection, account_id, item_uuid, expected_version)
                if item_kind == "relationship_profile"
                else await self._get_item(connection, account_id, item_kind, item_uuid)
            )
            version = (
                result.version_number if isinstance(result, RelationshipProfile) else result.version
            )
            await self._receipt(
                connection, account_id, idempotency_key, "add_source", payload,
                item_kind, item_uuid, version
            )
            return result

    async def review_cognitive_claim(
        self,
        *,
        account_id: str,
        claim_id: str,
        status: ItemStatus,
        expected_version: int,
        step_up_verified: bool,
        idempotency_key: str,
    ) -> CognitiveClaim:
        return cast(
            CognitiveClaim,
            await self._review_item(
                account_id=account_id,
                item_kind="cognitive_claim",
                item_id=claim_id,
                status=status,
                expected_version=expected_version,
                step_up_verified=step_up_verified,
                idempotency_key=idempotency_key,
            ),
        )

    async def review_decision_case(
        self,
        *,
        account_id: str,
        case_id: str,
        status: ItemStatus,
        expected_version: int,
        step_up_verified: bool,
        idempotency_key: str,
    ) -> DecisionCase:
        return cast(
            DecisionCase,
            await self._review_item(
                account_id=account_id,
                item_kind="decision_case",
                item_id=case_id,
                status=status,
                expected_version=expected_version,
                step_up_verified=step_up_verified,
                idempotency_key=idempotency_key,
            ),
        )

    async def approve_relationship_profile(
        self,
        *,
        account_id: str,
        profile_id: str,
        step_up_verified: bool,
    ) -> RelationshipProfile:
        if not step_up_verified:
            raise InvalidSelfModelTransitionError("relationship approval requires step-up verification")
        pool = await self._ready_pool()
        item_uuid = self._uuid(profile_id)
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            row = await self._require_item(connection, account_id, "relationship_profile", item_uuid)
            if str(row["status"]) != "candidate":
                raise InvalidSelfModelTransitionError("only candidate relationship profiles can be approved")
            await connection.execute(
                """
                UPDATE self_model_relationship_profiles
                SET status = 'approved', owner_reviewed_at = now(), step_up_verified = true
                WHERE profile_id = $1 AND account_id = $2
                """,
                item_uuid,
                account_id,
            )
            await self._audit(
                connection,
                account_id,
                "approve_relationship_profile",
                "relationship_profile",
                item_uuid,
            )
            return cast(
                RelationshipProfile,
                await self._get_item(connection, account_id, "relationship_profile", item_uuid),
            )

    async def revoke_relationship_profile(
        self, *, account_id: str, profile_id: str
    ) -> RelationshipProfile:
        pool = await self._ready_pool()
        item_uuid = self._uuid(profile_id)
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            row = await self._require_item(connection, account_id, "relationship_profile", item_uuid)
            if str(row["status"]) != "approved":
                raise InvalidSelfModelTransitionError("only approved relationship profiles can be revoked")
            await connection.execute(
                """UPDATE self_model_relationship_profiles SET status = 'revoked'
                   WHERE profile_id = $1 AND account_id = $2""",
                item_uuid,
                account_id,
            )
            await self._audit(
                connection,
                account_id,
                "revoke_relationship_profile",
                "relationship_profile",
                item_uuid,
            )
            return cast(
                RelationshipProfile,
                await self._get_item(connection, account_id, "relationship_profile", item_uuid),
            )

    async def review_relationship_profile(
        self,
        *,
        account_id: str,
        profile_id: str,
        version_number: int,
        status: Literal["approved", "revoked"],
        expected_status: Literal["candidate", "approved"],
        step_up_verified: bool,
        idempotency_key: str,
    ) -> RelationshipProfile:
        if (expected_status, status) not in {("candidate", "approved"), ("approved", "revoked")}:
            raise InvalidSelfModelTransitionError("invalid relationship profile transition")
        if status == "approved" and not step_up_verified:
            raise InvalidSelfModelTransitionError("relationship approval requires step-up verification")
        item_uuid = self._uuid(profile_id)
        payload = {
            "profile_id": profile_id, "version_number": version_number, "status": status,
            "expected_status": expected_status, "step_up_verified": step_up_verified,
        }
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            duplicate = await self._duplicate(
                connection, account_id, idempotency_key, "review_relationship_profile", payload
            )
            if duplicate is not None:
                return cast(RelationshipProfile, duplicate)
            result = await connection.execute(
                """
                UPDATE self_model_relationship_profiles
                SET status = $4, owner_reviewed_at = now(), step_up_verified = $5
                WHERE account_id = $1 AND profile_id = $2 AND version_number = $3 AND status = $6
                """,
                account_id, item_uuid, version_number, status, step_up_verified, expected_status,
            )
            if result.endswith(" 0"):
                raise SelfModelVersionConflictError(profile_id)
            profile = await self._profile_by_id(connection, account_id, item_uuid, version_number)
            await self._audit(connection, account_id, "approve_relationship_profile" if status == "approved" else "revoke_relationship_profile", "relationship_profile", item_uuid)
            await self._receipt(connection, account_id, idempotency_key, "review_relationship_profile", payload, "relationship_profile", item_uuid, version_number)
            return profile

    async def revise_relationship_profile(
        self,
        *,
        account_id: str,
        profile_id: str,
        expected_version: int,
        salutation: str,
        tone: str,
        advice_style: str,
        boundaries: Iterable[str],
        idempotency_key: str,
        sharing_scope: str,
        unresolved_conflict: bool = False,
    ) -> RelationshipProfile:
        item_uuid = self._uuid(profile_id)
        boundaries_tuple = self._string_tuple(boundaries, "boundaries")
        payload = {
            "profile_id": profile_id, "expected_version": expected_version,
            "salutation": salutation, "tone": tone, "advice_style": advice_style,
            "boundaries": boundaries_tuple, "sharing_scope": sharing_scope,
            "unresolved_conflict": unresolved_conflict,
        }
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            duplicate = await self._duplicate(
                connection, account_id, idempotency_key, "revise_relationship_profile", payload
            )
            if duplicate is not None:
                return cast(RelationshipProfile, duplicate)
            current = await self._profile_by_id(connection, account_id, item_uuid, expected_version)
            latest = await connection.fetchval(
                """SELECT max(version_number) FROM self_model_relationship_profiles
                   WHERE account_id = $1 AND profile_id = $2""",
                account_id, item_uuid,
            )
            if int(latest) != expected_version:
                raise SelfModelVersionConflictError(profile_id)
            next_version = expected_version + 1
            await connection.execute(
                """UPDATE self_model_relationship_profiles SET status = 'superseded'
                   WHERE account_id = $1 AND profile_id = $2 AND version_number = $3""",
                account_id, item_uuid, expected_version,
            )
            await connection.execute(
                """
                INSERT INTO self_model_relationship_profiles (
                    profile_id, account_id, version_number, person_id, relationship_id,
                    salutation, tone, advice_style, sharing_scope, boundaries, unresolved_conflict
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
                """,
                item_uuid, account_id, next_version, self._uuid(current.person_id),
                self._uuid(current.relationship_id), salutation, tone, advice_style,
                sharing_scope, json.dumps(boundaries_tuple, ensure_ascii=False), unresolved_conflict,
            )
            profile = await self._profile_by_id(connection, account_id, item_uuid, next_version)
            await self._audit(connection, account_id, "create_relationship_profile", "relationship_profile", item_uuid)
            await self._receipt(connection, account_id, idempotency_key, "revise_relationship_profile", payload, "relationship_profile", item_uuid, next_version)
            return profile

    async def get_cognitive_claim(self, *, account_id: str, claim_id: str) -> CognitiveClaim:
        return cast(
            CognitiveClaim,
            await self._get(account_id, "cognitive_claim", self._uuid(claim_id)),
        )

    async def get_decision_case(self, *, account_id: str, case_id: str) -> DecisionCase:
        return cast(DecisionCase, await self._get(account_id, "decision_case", self._uuid(case_id)))

    async def get_relationship_profile(
        self, *, account_id: str, profile_id: str, version_number: int | None = None
    ) -> RelationshipProfile:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            return await self._profile_by_id(
                connection, account_id, self._uuid(profile_id), version_number
            )

    async def list_cognitive_claims(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[CognitiveClaim, ...]:
        return cast(
            tuple[CognitiveClaim, ...],
            await self._list(account_id, "cognitive_claim", effective_only),
        )

    async def list_decision_cases(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[DecisionCase, ...]:
        return cast(
            tuple[DecisionCase, ...],
            await self._list(account_id, "decision_case", effective_only),
        )

    async def list_relationship_profiles(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[RelationshipProfile, ...]:
        return cast(
            tuple[RelationshipProfile, ...],
            await self._list(account_id, "relationship_profile", effective_only),
        )

    async def cognitive_claims(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[CognitiveClaim, ...]:
        return await self.list_cognitive_claims(
            account_id=account_id, effective_only=effective_only
        )

    async def decision_cases(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[DecisionCase, ...]:
        return await self.list_decision_cases(
            account_id=account_id, effective_only=effective_only
        )

    async def relationship_profiles(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[RelationshipProfile, ...]:
        return await self.list_relationship_profiles(
            account_id=account_id, effective_only=effective_only
        )

    async def export_account(self, account_id: str) -> dict[str, list[dict[str, object]]]:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            result: dict[str, list[dict[str, object]]] = {}
            for table in _EXPORT_TABLES:
                rows = await connection.fetch(
                    f"SELECT * FROM {table} WHERE account_id = $1", account_id
                )
                result[table] = [self._portable_row(row) for row in rows]
            return result

    async def delete_account(self, account_id: str) -> dict[str, int]:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            await connection.execute("SELECT set_config('app.self_model_delete', 'true', true)")
            await self._audit(connection, account_id, "delete_account", "account", None)
            counts: dict[str, int] = {}
            for table in _DELETE_ORDER:
                status = await connection.execute(f"DELETE FROM {table} WHERE account_id = $1", account_id)
                counts[table] = int(status.rsplit(" ", 1)[-1])
            return counts

    async def _review_item(
        self,
        *,
        account_id: str,
        item_kind: Literal["cognitive_claim", "decision_case"],
        item_id: str,
        status: ItemStatus,
        expected_version: int,
        step_up_verified: bool,
        idempotency_key: str,
    ) -> SelfModelItem:
        if expected_version <= 0:
            raise ValueError("expected_version must be positive")
        pool = await self._ready_pool()
        item_uuid = self._uuid(item_id)
        table, _, primary = _ITEM_TABLES[item_kind]
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            payload = {
                "item_kind": item_kind, "item_id": item_id, "status": status,
                "expected_version": expected_version, "step_up_verified": step_up_verified,
            }
            duplicate = await self._duplicate(
                connection, account_id, idempotency_key, "review_item", payload
            )
            if duplicate is not None:
                return duplicate
            row = await self._require_item(connection, account_id, item_kind, item_uuid)
            if int(row["version"]) != expected_version:
                raise SelfModelVersionConflictError(item_id)
            current = await self._item_from_row(connection, account_id, item_kind, row)
            if status not in _ITEM_TRANSITIONS[cast(ItemStatus, current.status)]:
                raise InvalidSelfModelTransitionError(
                    f"cannot transition {current.status} to {status}"
                )
            if (
                isinstance(current, CognitiveClaim)
                and current.claim_type in HIGH_SENSITIVITY_CLAIM_TYPES
                and status == "confirmed"
            ):
                if not step_up_verified:
                    raise InvalidSelfModelTransitionError(
                        "high-sensitivity claims require owner step-up review"
                    )
                if not any(
                    source.speaker_class == "owner"
                    and source.relation == "counterexample"
                    and not source.negative
                    for source in current.sources
                ):
                    raise InvalidSelfModelTransitionError(
                        "high-sensitivity claims require an owner counterexample"
                    )
            updates = [
                "status = $3",
                "step_up_verified = $4",
                "owner_reviewed_at = now()",
                "version = version + 1",
                "updated_at = now()",
            ]
            args: list[object] = [item_uuid, account_id, status, step_up_verified]
            await connection.execute(
                f"UPDATE {table} SET {', '.join(updates)} "
                f"WHERE {primary} = $1 AND account_id = $2",
                *args,
            )
            await self._audit(
                connection,
                account_id,
                "review_claim" if item_kind == "cognitive_claim" else "review_decision_case",
                item_kind,
                item_uuid,
            )
            result = await self._get_item(connection, account_id, item_kind, item_uuid)
            await self._receipt(
                connection, account_id, idempotency_key, "review_item",
                payload, item_kind, item_uuid, expected_version + 1
            )
            return result

    async def _get(
        self, account_id: str, item_kind: SelfModelItemKind, item_id: uuid.UUID
    ) -> SelfModelItem:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            return await self._get_item(connection, account_id, item_kind, item_id)

    async def _list(
        self, account_id: str, item_kind: SelfModelItemKind, effective_only: bool
    ) -> tuple[SelfModelItem, ...]:
        pool = await self._ready_pool()
        table, _, _ = _ITEM_TABLES[item_kind]
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                f"SELECT * FROM {table} WHERE account_id = $1 ORDER BY created_at, {self._id_column(item_kind)}",
                account_id,
            )
            items = tuple(
                [await self._item_from_row(connection, account_id, item_kind, row) for row in rows]
            )
            return tuple(item for item in items if not effective_only or activation_decision(item).effective)

    async def _get_item(
        self,
        connection: asyncpg.Connection,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: uuid.UUID,
    ) -> SelfModelItem:
        row = await self._require_item(connection, account_id, item_kind, item_id)
        return await self._item_from_row(connection, account_id, item_kind, row)

    async def _profile_by_id(
        self,
        connection: asyncpg.Connection,
        account_id: str,
        profile_id: uuid.UUID,
        version_number: int | None = None,
    ) -> RelationshipProfile:
        row = await connection.fetchrow(
            """
            SELECT * FROM self_model_relationship_profiles
            WHERE profile_id = $1 AND account_id = $2
              AND ($3::integer IS NULL OR version_number = $3)
            ORDER BY version_number DESC LIMIT 1
            """,
            profile_id,
            account_id,
            version_number,
        )
        if row is None:
            raise SelfModelNotFoundError(str(profile_id))
        return await self._profile(connection, account_id, row)

    async def _require_item(
        self,
        connection: asyncpg.Connection,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: uuid.UUID,
    ) -> asyncpg.Record:
        table, _, primary = _ITEM_TABLES[item_kind]
        row = await connection.fetchrow(
            f"SELECT * FROM {table} WHERE {primary} = $1 AND account_id = $2",
            item_id,
            account_id,
        )
        if row is None:
            raise SelfModelNotFoundError(str(item_id))
        return row

    async def _item_from_row(
        self,
        connection: asyncpg.Connection,
        account_id: str,
        item_kind: SelfModelItemKind,
        row: asyncpg.Record,
    ) -> SelfModelItem:
        if item_kind == "cognitive_claim":
            return await self._claim(connection, account_id, row)
        if item_kind == "decision_case":
            return await self._decision(connection, account_id, row)
        return await self._profile(connection, account_id, row)

    async def _claim(
        self, connection: asyncpg.Connection, account_id: str, row: asyncpg.Record
    ) -> CognitiveClaim:
        return CognitiveClaim(
            claim_id=str(row["claim_id"]),
            account_id=account_id,
            claim_type=cast(CognitiveClaimType, str(row["claim_type"])),
            statement=str(row["statement"]),
            context=str(row["context"]),
            confidence=float(row["confidence"]),
            sharing_scope=str(row["sharing_scope"]),
            status=cast(ItemStatus, str(row["status"])),
            unresolved_conflict=bool(row["unresolved_conflict"]),
            sources=await self._sources(connection, account_id, "cognitive_claim", row["claim_id"]),
            owner_reviewed_at=cast(datetime | None, row["owner_reviewed_at"]),
            step_up_verified=bool(row["step_up_verified"]),
            version=int(row["version"]),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
        )

    async def _decision(
        self, connection: asyncpg.Connection, account_id: str, row: asyncpg.Record
    ) -> DecisionCase:
        return DecisionCase(
            case_id=str(row["case_id"]),
            account_id=account_id,
            kind=cast(DecisionKind, str(row["kind"])),
            context=str(row["context"]),
            options=tuple(cast(list[str], row["options"])),
            constraints=tuple(cast(list[str], row["constraints"])),
            chosen_option=str(row["chosen_option"]),
            rejected_options=tuple(cast(list[str], row["rejected_options"])),
            outcome=str(row["outcome"]),
            reflection=str(row["reflection"]),
            still_endorsed=bool(row["still_endorsed"]),
            sharing_scope=str(row["sharing_scope"]),
            status=cast(ItemStatus, str(row["status"])),
            unresolved_conflict=bool(row["unresolved_conflict"]),
            sources=await self._sources(connection, account_id, "decision_case", row["case_id"]),
            owner_reviewed_at=cast(datetime | None, row["owner_reviewed_at"]),
            step_up_verified=bool(row["step_up_verified"]),
            version=int(row["version"]),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
        )

    async def _profile(
        self, connection: asyncpg.Connection, account_id: str, row: asyncpg.Record
    ) -> RelationshipProfile:
        return RelationshipProfile(
            profile_id=str(row["profile_id"]),
            account_id=account_id,
            version_number=int(row["version_number"]),
            person_id=str(row["person_id"]),
            relationship_id=str(row["relationship_id"]),
            salutation=str(row["salutation"]),
            tone=str(row["tone"]),
            advice_style=str(row["advice_style"]),
            sharing_scope=str(row["sharing_scope"]),
            boundaries=tuple(cast(list[str], row["boundaries"])),
            status=cast(RelationshipProfileStatus, str(row["status"])),
            unresolved_conflict=bool(row["unresolved_conflict"]),
            sources=await self._sources(
                connection,
                account_id,
                "relationship_profile",
                row["profile_id"],
                profile_version=int(row["version_number"]),
            ),
            owner_reviewed_at=cast(datetime | None, row["owner_reviewed_at"]),
            step_up_verified=bool(row["step_up_verified"]),
            created_at=cast(datetime, row["created_at"]),
        )

    async def _sources(
        self,
        connection: asyncpg.Connection,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: uuid.UUID,
        profile_version: int | None = None,
    ) -> tuple[SelfModelSource, ...]:
        _, source_table, primary = _ITEM_TABLES[item_kind]
        profile_clause = (
            "AND source.profile_version = $3" if item_kind == "relationship_profile" else ""
        )
        rows = await connection.fetch(
            f"""
            SELECT source.source_event_id, source.relation, source.adopted, source.negative,
                   event.speaker_class, event.occurred_at
            FROM {source_table} AS source
            JOIN archive_evidence_events AS event
              ON event.event_id = source.source_event_id AND event.account_id = source.account_id
            WHERE source.{primary} = $1 AND source.account_id = $2 {profile_clause}
            ORDER BY event.occurred_at, source.source_event_id, source.relation
            """,
            item_id,
            account_id,
            *([profile_version] if item_kind == "relationship_profile" else []),
        )
        return tuple(
            SelfModelSource(
                source_event_id=str(row["source_event_id"]),
                relation=cast(SourceRelation, str(row["relation"])),
                adopted=bool(row["adopted"]),
                negative=bool(row["negative"]),
                speaker_class=cast(Any, str(row["speaker_class"])),
                occurred_at=cast(datetime, row["occurred_at"]),
            )
            for row in rows
        )

    async def _trusted_source(
        self,
        connection: asyncpg.Connection,
        *,
        account_id: str,
        source: SourceInput,
    ) -> SelfModelSource:
        event = await connection.fetchrow(
            """
            SELECT event_type, speaker_class, payload, occurred_at
            FROM archive_evidence_events
            WHERE event_id = $1 AND account_id = $2
            """,
            source.source_event_id,
            account_id,
        )
        if event is None:
            raise UntrustedSelfModelSourceError("source event is not in the account")
        event_payload = event["payload"]
        if isinstance(event_payload, str):
            event_payload = json.loads(event_payload)
        if (
            str(event["speaker_class"]) != "owner"
            or event_payload.get("owner_projection_eligible") is not True
            or event_payload.get("simulated_output") is True
            or event_payload.get("interaction_mode") not in {None, "companion"}
            or str(event["event_type"])
            not in {"speech.utterance_finalized", "owner.action_recorded"}
        ):
            raise UntrustedSelfModelSourceError(
                "only eligible owner evidence from a real companion interaction is allowed"
            )
        return SelfModelSource(
            source_event_id=source.source_event_id,
            relation=source.relation,
            adopted=source.adopted,
            negative=source.negative,
            speaker_class=cast(Any, str(event["speaker_class"])),
            occurred_at=cast(datetime, event["occurred_at"]),
        )

    async def _attach_creation_sources(
        self,
        connection: asyncpg.Connection,
        *,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: uuid.UUID,
        sources: tuple[SourceInput, ...],
    ) -> int:
        result_version = 1
        for source_input in sources:
            source = await self._trusted_source(
                connection,
                account_id=account_id,
                source=source_input,
            )
            await self._add_sources(connection, account_id, item_kind, item_id, (source,))
            if item_kind != "relationship_profile":
                table, _, primary = _ITEM_TABLES[item_kind]
                status = await connection.execute(
                    f"""
                    UPDATE {table} SET version = version + 1, updated_at = now()
                    WHERE {primary} = $1 AND account_id = $2 AND version = $3
                    """,
                    item_id,
                    account_id,
                    result_version,
                )
                if status.endswith(" 0"):
                    raise SelfModelVersionConflictError(str(item_id))
                result_version += 1
            await self._audit(connection, account_id, "add_source", item_kind, item_id)
        return result_version

    async def _add_sources(
        self,
        connection: asyncpg.Connection,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: uuid.UUID,
        sources: tuple[SelfModelSource, ...],
    ) -> None:
        _, source_table, primary = _ITEM_TABLES[item_kind]
        for source in sources:
            if item_kind == "relationship_profile":
                profile = await self._profile_by_id(connection, account_id, item_id)
                await connection.execute(
                    """
                    INSERT INTO self_model_relationship_profile_sources (
                        profile_id, profile_version, account_id, source_event_id,
                        relation, adopted, negative, occurred_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (profile_id, profile_version, source_event_id, relation)
                    DO UPDATE SET adopted = excluded.adopted, negative = excluded.negative
                    """,
                    item_id, profile.version_number, account_id, source.source_event_id,
                    source.relation, source.adopted, source.negative,
                    source.occurred_at.astimezone(UTC),
                )
                continue
            await connection.execute(
                f"""
                INSERT INTO {source_table} (
                    {primary}, account_id, source_event_id, relation, adopted, negative, occurred_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT ({primary}, source_event_id, relation) DO UPDATE
                SET adopted = excluded.adopted, negative = excluded.negative,
                    occurred_at = excluded.occurred_at
                """,
                item_id,
                account_id,
                source.source_event_id,
                source.relation,
                source.adopted,
                source.negative,
                source.occurred_at.astimezone(UTC),
            )

    async def _audit(
        self,
        connection: asyncpg.Connection,
        account_id: str,
        action: str,
        target_kind: str,
        target_id: uuid.UUID | None,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO self_model_audit_events (
                event_id, account_id, actor_account_id, transaction_id,
                action, target_kind, target_id, details, occurred_at
            ) VALUES ($1, $2, $2, $3, $4, $5, $6, '{}'::jsonb, now())
            """,
            uuid.uuid4(),
            account_id,
            uuid.uuid4(),
            action,
            target_kind,
            target_id,
        )

    async def _duplicate(
        self,
        connection: asyncpg.Connection,
        account_id: str,
        idempotency_key: str,
        command_type: str,
        payload: dict[str, object],
    ) -> SelfModelItem | None:
        digest = self._digest(command_type, payload)
        row = await connection.fetchrow(
            """SELECT command_type, payload_sha256, result_kind, result_id, result_version
               FROM self_model_command_receipts
               WHERE account_id = $1 AND idempotency_key = $2""",
            account_id,
            idempotency_key,
        )
        if row is None:
            return None
        if str(row["command_type"]) != command_type or str(row["payload_sha256"]) != digest:
            raise SelfModelIdempotencyConflictError(idempotency_key)
        kind = cast(SelfModelItemKind, str(row["result_kind"]))
        if kind == "relationship_profile":
            return await self._profile_by_id(
                connection, account_id, cast(uuid.UUID, row["result_id"]), int(row["result_version"])
            )
        return await self._get_item(
            connection, account_id, kind, cast(uuid.UUID, row["result_id"])
        )

    async def _receipt(
        self,
        connection: asyncpg.Connection,
        account_id: str,
        idempotency_key: str,
        command_type: str,
        payload: dict[str, object],
        result_kind: SelfModelItemKind,
        result_id: uuid.UUID,
        result_version: int,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO self_model_command_receipts (
                account_id, idempotency_key, command_type, payload_sha256,
                result_kind, result_id, result_version
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            account_id,
            idempotency_key,
            command_type,
            self._digest(command_type, payload),
            result_kind,
            result_id,
            result_version,
        )

    @staticmethod
    def _digest(command_type: str, payload: dict[str, object]) -> str:
        encoded = json.dumps(
            {"command_type": command_type, **payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _portable_row(row: asyncpg.Record) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, raw in dict(row).items():
            if isinstance(raw, uuid.UUID):
                value[key] = str(raw)
            elif isinstance(raw, datetime):
                value[key] = raw.astimezone(UTC).isoformat()
            else:
                value[key] = cast(object, raw)
        return value

    @staticmethod
    def _uuid(value: str) -> uuid.UUID:
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise SelfModelNotFoundError(value) from exc

    @staticmethod
    def _id_column(item_kind: SelfModelItemKind) -> str:
        return _ITEM_TABLES[item_kind][2]

    @staticmethod
    def _require_text(value: str, name: str) -> None:
        if not value.strip():
            raise ValueError(f"{name} is required")

    @staticmethod
    def _require_scope(value: str) -> None:
        if not value.strip():
            raise ValueError("sharing_scope is required")

    @staticmethod
    def _require_confidence(value: float) -> None:
        if not 0 <= value <= 1:
            raise ValueError("confidence must be between 0 and 1")

    @staticmethod
    def _string_tuple(values: Iterable[str], name: str) -> tuple[str, ...]:
        result = tuple(value.strip() for value in values)
        if any(not value for value in result):
            raise ValueError(f"{name} cannot contain blank values")
        return result
