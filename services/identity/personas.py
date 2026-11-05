"""Persona-assignment and custom-persona seam of ``IdentityService``.

``IdentityPersonaMixin`` is composed into ``IdentityService``; the store handle
stays owned by the facade and is only declared here for type checking. The
module exists so the persona read and write paths can be read on their own,
without the person, relationship, binding and transfer code around them.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from services.common.companions import DEFAULT_COMPANION_ID
from services.identity.domain import (
    MAX_CUSTOM_PERSONAS_PER_OWNER,
    CustomPersonaDeleteOutcome,
    CustomPersonaRecord,
    IdempotencyRecord,
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    PersonaAssignmentRecord,
    StructuredPersonaFields,
    canonical_persona_assignment_id,
    new_custom_persona_id,
)
from services.identity.repository import AuditEvent
from services.identity.service_helpers import (
    _new_id,
    _now,
    _replay_or_conflict,
    _request_hash,
    _resolve_persona_selection,
)

if TYPE_CHECKING:
    from services.identity.domain import DeviceBinding
    from services.identity.repository import IdentityStore


class IdentityPersonaMixin:
    """Pin one persona per subject and own each account's custom personas."""

    if TYPE_CHECKING:
        # Shared state owned by the IdentityService facade.
        _store: IdentityStore

        async def get_binding(
            self, binding_id: str, actor_person_id: str | None = None
        ) -> DeviceBinding: ...

    # ------------------------------------------------------------------
    # Persona assignments (binding + subject scoped)
    # ------------------------------------------------------------------

    async def set_persona_assignment(
        self,
        *,
        binding_id: str,
        subject_id: str,
        persona_selection: str,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> PersonaAssignmentRecord:
        """Pin one persona to one subject on one binding.

        ``(binding_id, subject_id)`` is the key: a subject holds at most one
        persona on a binding.  Replaying the same persona is idempotent and
        returns the persisted row untouched; a different persona replaces the
        override while keeping the original ``created_at``.

        Authorization stays in Control API (``_can_switch_subject`` plus
        ``_is_binding_member``); this facade only enforces that the binding
        exists and the persona is a known one.
        """
        timestamp = _now(now)
        binding = await self.get_binding(
            binding_id, actor_person_id=actor_person_id
        )
        custom_ids = await self._custom_persona_ids_for_owner(
            binding.account_owner_person_id, actor_person_id=actor_person_id
        )
        persona_id, persona_version = _resolve_persona_selection(
            persona_selection, custom_persona_ids=custom_ids
        )
        existing = await self._store.get_persona_assignment(
            binding_id=binding.binding_id,
            subject_id=subject_id,
            actor_person_id=actor_person_id,
        )
        record = PersonaAssignmentRecord(
            binding_id=binding.binding_id,
            subject_id=subject_id,
            assignment_id=canonical_persona_assignment_id(
                persona_id, persona_version
            ),
            persona_id=persona_id,
            persona_version=persona_version,
            created_at=existing.created_at if existing is not None else timestamp,
            updated_at=timestamp,
        )
        payload: dict[str, object] = record.to_dict()
        if existing is not None:
            payload["previous_persona_id"] = existing.persona_id
        return await self._store.upsert_persona_assignment(
            record,
            audit_event=AuditEvent(
                event_id=_new_id(),
                action="binding.persona_assignment.set",
                actor_person_id=actor_person_id,
                subject_person_id=subject_id,
                person_id=subject_id,
                device_id=binding.device_id,
                binding_id=binding.binding_id,
                relationship_id=None,
                payload=payload,
                created_at=timestamp,
            ),
            actor_person_id=actor_person_id,
            scope="api",
        )

    async def get_persona_assignment(
        self,
        *,
        binding_id: str,
        subject_id: str,
        actor_person_id: str | None = None,
    ) -> PersonaAssignmentRecord | None:
        """Return the subject override, or ``None`` for the binding default."""
        return await self._store.get_persona_assignment(
            binding_id=binding_id,
            subject_id=subject_id,
            actor_person_id=actor_person_id,
        )

    async def list_persona_assignments(
        self,
        *,
        binding_id: str,
        actor_person_id: str | None = None,
    ) -> tuple[PersonaAssignmentRecord, ...]:
        """Every subject override on one binding, ordered by subject id."""
        return await self._store.list_persona_assignments(
            binding_id=binding_id, actor_person_id=actor_person_id
        )

    async def delete_persona_assignment(
        self,
        *,
        binding_id: str,
        subject_id: str,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Drop a subject override; ``False`` when there was none to drop."""
        timestamp = _now(now)
        binding = await self.get_binding(
            binding_id, actor_person_id=actor_person_id
        )
        existing = await self._store.get_persona_assignment(
            binding_id=binding.binding_id,
            subject_id=subject_id,
            actor_person_id=actor_person_id,
        )
        if existing is None:
            return False
        return await self._store.delete_persona_assignment(
            binding_id=binding.binding_id,
            subject_id=subject_id,
            audit_event=AuditEvent(
                event_id=_new_id(),
                action="binding.persona_assignment.delete",
                actor_person_id=actor_person_id,
                subject_person_id=subject_id,
                person_id=subject_id,
                device_id=binding.device_id,
                binding_id=binding.binding_id,
                relationship_id=None,
                payload=existing.to_dict(),
                created_at=timestamp,
            ),
            actor_person_id=actor_person_id,
            scope="api",
        )

    # ------------------------------------------------------------------
    # Custom personas (account-level, create-once, immutable)
    # ------------------------------------------------------------------

    async def _custom_persona_ids_for_owner(
        self,
        owner_person_id: str,
        *,
        actor_person_id: str | None = None,
    ) -> frozenset[str]:
        """The persona ids this owner may assign (built-in ids are separate)."""
        records = await self._store.list_custom_personas(
            owner_person_id, actor_person_id=actor_person_id
        )
        return frozenset(record.persona_id for record in records)

    async def create_custom_persona(
        self,
        *,
        owner_person_id: str,
        display_name: str,
        structured: StructuredPersonaFields,
        fallback_designed_voice: str = DEFAULT_COMPANION_ID,
        idempotency_key: str | None = None,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> CustomPersonaRecord:
        """Create one immutable custom persona owned by ``owner_person_id``.

        Only the account owner may create (``actor_person_id`` must equal the
        owner).  The record is validated (enum/length/rate allowlist) before
        any adapter runs and persisted create-once; an account may hold at most
        ``MAX_CUSTOM_PERSONAS_PER_OWNER`` personas, enforced under the owner's
        storage lock.  With ``idempotency_key`` a replay returns the original
        record (reusing the identity ``Idempotency-Key`` store) and never a
        second row.
        """
        timestamp = _now(now)
        if actor_person_id is not None and actor_person_id != owner_person_id:
            raise IdentityAccessDeniedError(
                "only the account owner may create a custom persona"
            )
        record = CustomPersonaRecord(
            persona_id=new_custom_persona_id(),
            owner_person_id=owner_person_id,
            display_name=display_name,
            style_description=structured.style_description,
            warmth=structured.warmth,
            directness=structured.directness,
            response_length=structured.response_length,
            question_frequency=structured.question_frequency,
            interview_depth=structured.interview_depth,
            welcome_text=structured.welcome_text,
            conversation_instruction=structured.conversation_instruction,
            voice_instruction=structured.voice_instruction,
            default_voice_emotion=structured.default_voice_emotion,
            default_voice_rate=structured.default_voice_rate,
            fallback_designed_voice=fallback_designed_voice,
            persona_version=1,
            source="user_created",
            created_at=timestamp,
        )
        idempotency_record: IdempotencyRecord | None = None
        scope_key: str | None = None
        content_hash: str | None = None
        if idempotency_key is not None:
            key = idempotency_key.strip()
            if not key or len(key) > 64:
                raise IdentityConflictError(
                    "idempotency_key must be a bounded non-empty string"
                )
            scope_key = f"{record.owner_person_id}:persona.create"
            content_hash = _request_hash(
                owner_person_id=record.owner_person_id,
                display_name=record.display_name,
                fallback_designed_voice=record.fallback_designed_voice,
                style_description=structured.style_description,
                warmth=structured.warmth,
                directness=structured.directness,
                response_length=structured.response_length,
                question_frequency=structured.question_frequency,
                interview_depth=structured.interview_depth,
                welcome_text=structured.welcome_text,
                conversation_instruction=structured.conversation_instruction,
                voice_instruction=structured.voice_instruction,
                default_voice_emotion=structured.default_voice_emotion,
                default_voice_rate=structured.default_voice_rate,
            )
            existing = await self._store.get_idempotency_record(
                scope_key, key, actor_person_id=actor_person_id
            )
            if existing is not None:
                _replay_or_conflict(existing, content_hash=content_hash)
                return CustomPersonaRecord.from_mapping(existing.result_payload)
            idempotency_record = IdempotencyRecord(
                scope_key=scope_key,
                idempotency_key=key,
                operation="persona.create",
                content_hash=content_hash,
                result_payload=record.to_dict(),
                created_at=timestamp,
            )
        try:
            return await self._store.save_custom_persona(
                record,
                max_per_owner=MAX_CUSTOM_PERSONAS_PER_OWNER,
                idempotency_record=idempotency_record,
                audit_event=AuditEvent(
                    event_id=_new_id(),
                    action="persona.custom.create",
                    actor_person_id=actor_person_id,
                    subject_person_id=record.owner_person_id,
                    person_id=record.owner_person_id,
                    device_id=None,
                    binding_id=None,
                    relationship_id=None,
                    payload=record.to_dict(),
                    created_at=timestamp,
                ),
                actor_person_id=actor_person_id,
                scope="api",
            )
        except IdentityConflictError as exc:
            if (
                idempotency_record is None
                or "already used" not in str(exc)
                or scope_key is None
                or content_hash is None
            ):
                raise
            winner = await self._store.get_idempotency_record(
                scope_key, idempotency_record.idempotency_key,
                actor_person_id=actor_person_id,
            )
            if winner is None:
                raise exc from exc
            if winner.operation != "persona.create":
                raise IdentityConflictError(
                    "idempotency key already used by a different operation"
                ) from exc
            _replay_or_conflict(winner, content_hash=content_hash)
            return CustomPersonaRecord.from_mapping(winner.result_payload)

    async def get_custom_persona(
        self,
        persona_id: str,
        *,
        owner_person_id: str,
        actor_person_id: str | None = None,
    ) -> CustomPersonaRecord:
        """Return one owner's persona; a foreign/absent id is not found."""
        record = await self._store.get_custom_persona(
            persona_id, actor_person_id=actor_person_id
        )
        if record is None or record.owner_person_id != owner_person_id:
            raise IdentityNotFoundError(f"custom persona {persona_id} does not exist")
        return record

    async def list_custom_personas(
        self,
        *,
        owner_person_id: str,
        actor_person_id: str | None = None,
    ) -> tuple[CustomPersonaRecord, ...]:
        """Every custom persona owned by the account, oldest first."""
        return await self._store.list_custom_personas(
            owner_person_id, actor_person_id=actor_person_id
        )

    async def count_custom_personas(
        self,
        *,
        owner_person_id: str,
        actor_person_id: str | None = None,
    ) -> int:
        return await self._store.count_custom_personas(
            owner_person_id, actor_person_id=actor_person_id
        )

    async def count_custom_persona_references(
        self,
        persona_id: str,
        *,
        actor_person_id: str | None = None,
    ) -> int:
        """How many subject assignments reference the persona (in-use count)."""
        return await self._store.count_persona_assignment_references(
            persona_id, actor_person_id=actor_person_id
        )

    async def delete_custom_persona(
        self,
        *,
        persona_id: str,
        owner_person_id: str,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> CustomPersonaDeleteOutcome:
        """Delete one persona and every assignment referencing it.

        Only the owner may delete.  The count -> drop references -> drop
        persona steps run in ONE storage transaction, so no dangling override
        can survive; the affected subjects fall back to the binding default.
        An absent/foreign persona yields ``deleted=False``.
        """
        timestamp = _now(now)
        if actor_person_id is not None and actor_person_id != owner_person_id:
            raise IdentityAccessDeniedError(
                "only the account owner may delete a custom persona"
            )
        try:
            record = await self.get_custom_persona(
                persona_id, owner_person_id=owner_person_id,
                actor_person_id=actor_person_id,
            )
        except IdentityNotFoundError:
            return CustomPersonaDeleteOutcome(
                persona_id=persona_id, deleted=False, drifted_subjects=0
            )
        drifted = await self._store.delete_custom_persona(
            record.persona_id,
            owner_person_id=owner_person_id,
            audit_events=(
                AuditEvent(
                    event_id=_new_id(),
                    action="persona.custom.delete",
                    actor_person_id=actor_person_id,
                    subject_person_id=owner_person_id,
                    person_id=owner_person_id,
                    device_id=None,
                    binding_id=None,
                    relationship_id=None,
                    payload=record.to_dict(),
                    created_at=timestamp,
                ),
            ),
            actor_person_id=actor_person_id,
            scope="api",
        )
        return CustomPersonaDeleteOutcome(
            persona_id=record.persona_id,
            deleted=True,
            drifted_subjects=drifted,
        )
