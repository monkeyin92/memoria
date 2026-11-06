"""IdentityService relationships: two-party confirmation, lifecycle transitions,
delegation, declared guardians and crisis contacts, plus persona assignment
and custom persona behavior."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from services.identity.domain import (
    CustomPersonaLimitError,
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    ModeConstraintError,
    RelationshipLifecycleError,
    StructuredPersonaFields,
)
from services.identity.in_memory_store import InMemoryIdentityStore
from services.identity.service import IdentityService
from services.identity.tests.service_test_support import (
    _adult,
    _establish_relationship,
    _minor,
    _now,
)

# Re-exported (not redefined) so pytest discovers the fixture by name.
from services.identity.tests.service_test_support import (
    service as service,
)
from services.identity.tests.service_test_support import (
    store as store,
)


@pytest.mark.asyncio
async def test_relationship_requires_both_party_confirmation(
    service: IdentityService,
) -> None:
    now = _now()
    parent = await _adult(service, "家长", now)
    child = await _minor(service, "孩子", now)
    pending = await service.propose_relationship(
        source_person_id=parent,
        target_person_id=child,
        relation_type="guardian_of",
        established_evidence_id="evidence-1",
        actor_person_id=parent,
        now=now,
    )
    assert pending.status == "pending"
    one_side = await service.confirm_relationship(
        relationship_id=pending.relationship_id, person_id=parent, now=now
    )
    assert one_side.status == "pending"
    active = await service.confirm_relationship(
        relationship_id=pending.relationship_id, person_id=child, now=now
    )
    assert active.status == "active"
    assert active.confirmed_by_source_at is not None
    assert active.confirmed_by_target_at is not None
    with pytest.raises(IdentityConflictError):
        await service.confirm_relationship(
            relationship_id=pending.relationship_id, person_id=parent, now=now
        )
    outsider = await _adult(service, "外人", now)
    with pytest.raises(IdentityAccessDeniedError):
        await service.confirm_relationship(
            relationship_id=pending.relationship_id, person_id=outsider, now=now
        )


@pytest.mark.asyncio
async def test_relationship_lifecycle_transitions(
    service: IdentityService,
) -> None:
    now = _now()
    parent = await _adult(service, "家长", now)
    child = await _minor(service, "孩子", now)
    relationship = await service.propose_relationship(
        source_person_id=parent,
        target_person_id=child,
        relation_type="guardian_of",
        established_evidence_id="evidence-1",
        actor_person_id=parent,
        now=now,
    )
    relationship = await service.confirm_relationship(
        relationship_id=relationship.relationship_id, person_id=parent, now=now
    )
    relationship = await service.confirm_relationship(
        relationship_id=relationship.relationship_id, person_id=child, now=now
    )
    assert relationship.status == "active"
    relationship = await service.suspend_relationship(
        relationship_id=relationship.relationship_id,
        actor_person_id=parent,
        now=now,
    )
    assert relationship.status == "suspended"
    relationship = await service.resume_relationship(
        relationship_id=relationship.relationship_id,
        actor_person_id=child,
        now=now,
    )
    assert relationship.status == "active"
    relationship = await service.dispute_relationship(
        relationship_id=relationship.relationship_id,
        actor_person_id=parent,
        reason="监护范围争议",
        now=now,
    )
    assert relationship.status == "disputed"
    one_side = await service.acknowledge_dispute_resolution(
        relationship_id=relationship.relationship_id,
        person_id=child,
        now=now,
    )
    assert one_side.status == "disputed"
    relationship = await service.acknowledge_dispute_resolution(
        relationship_id=relationship.relationship_id,
        person_id=parent,
        now=now,
    )
    assert relationship.status == "active"
    revoked = await service.revoke_relationship(
        relationship_id=relationship.relationship_id,
        actor_person_id=parent,
        evidence_id="evidence-revoke",
        now=now,
    )
    assert revoked.status == "revoked"
    with pytest.raises(RelationshipLifecycleError):
        await service.suspend_relationship(
            relationship_id=relationship.relationship_id,
            actor_person_id=parent,
            now=now,
        )


@pytest.mark.asyncio
async def test_non_self_relationship_cannot_skip_confirmation(
    service: IdentityService,
) -> None:
    now = _now()
    parent = await _adult(service, "家长", now)
    child = await _minor(service, "孩子", now)
    with pytest.raises(RelationshipLifecycleError, match="confirmation"):
        await service.propose_relationship(
            source_person_id=parent,
            target_person_id=child,
            relation_type="emergency_contact_for",
            established_evidence_id="evidence-1",
            requires_confirmation=False,
            actor_person_id=parent,
            now=now,
        )
    with pytest.raises(IdentityAccessDeniedError, match="may not propose"):
        await service.propose_relationship(
            source_person_id=parent,
            target_person_id=child,
            relation_type="emergency_contact_for",
            established_evidence_id="evidence-1",
            actor_person_id=child,
            now=now,
        )


@pytest.mark.asyncio
async def test_delegation_chain_depth_is_capped(service: IdentityService) -> None:
    now = _now()
    people = [await _adult(service, f"人{i}", now) for i in range(6)]
    previous_id: str | None = None
    for index in range(1, 5):
        proposed = await service.propose_relationship(
            source_person_id=people[index - 1],
            target_person_id=people[index],
            relation_type="delegate_for",
            established_evidence_id=f"evidence-delegate-{index}",
            can_delegate=True,
            delegated_from_relationship_id=previous_id,
            actor_person_id=people[index - 1],
            now=now,
        )
        confirmed = await service.confirm_relationship(
            relationship_id=proposed.relationship_id,
            person_id=people[index - 1],
            now=now,
        )
        active = await service.confirm_relationship(
            relationship_id=confirmed.relationship_id,
            person_id=people[index],
            now=now,
        )
        assert active.status == "active"
        previous_id = active.relationship_id
    assert active.delegation_depth == 3
    with pytest.raises(RelationshipLifecycleError, match="depth"):
        await service.propose_relationship(
            source_person_id=people[4],
            target_person_id=people[5],
            relation_type="delegate_for",
            established_evidence_id="evidence-delegate-6",
            can_delegate=True,
            delegated_from_relationship_id=previous_id,
            actor_person_id=people[4],
            now=now,
        )


@pytest.mark.asyncio
async def test_expire_relationships_by_valid_until(service: IdentityService) -> None:
    now = _now()
    parent = await _adult(service, "家长", now)
    child = await _minor(service, "孩子", now)
    await service.propose_relationship(
        source_person_id=parent,
        target_person_id=child,
        relation_type="guardian_of",
        established_evidence_id="evidence-1",
        valid_until=now + timedelta(days=30),
        actor_person_id=parent,
        now=now,
    )
    assert await service.expire_relationships(now=now + timedelta(days=31)) == 1
    relationships = await service.list_relationships(
        person_id=child, statuses=("expired",)
    )
    assert len(relationships) == 1


async def _declare_owner_guardianship(
    service: IdentityService,
    *,
    owner: str,
    child: str,
    now: datetime,
) -> str:
    """The production declaration: source-confirmed, binding evidence, owner."""
    proposed = await service.propose_relationship(
        source_person_id=owner,
        target_person_id=child,
        relation_type="guardian_of",
        established_evidence_id="guardian_declaration_v1:device_binding",
        actor_person_id=owner,
        now=now,
    )
    await service.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=owner,
        now=now,
    )
    return proposed.relationship_id


@pytest.mark.asyncio
async def test_third_party_self_declaration_is_never_a_declared_guardian(
    service: IdentityService,
) -> None:
    """知道孩子的 person id 不足以成为声明监护人。

    The production binding writes the declaration; a stranger who merely
    invites ``guardian_of`` to a known person id and self-accepts must stay
    out of the declaration set, and must never take the ``guardian`` role on
    someone else's ``parent_for_child`` binding.
    """

    now = _now()
    owner = await _adult(service, "家长", now)
    child = await _minor(service, "孩子", now)
    stranger = await _adult(service, "外人", now)

    stranger_invite = await service.propose_relationship(
        source_person_id=stranger,
        target_person_id=child,
        relation_type="guardian_of",
        established_evidence_id="self-asserted-evidence",
        actor_person_id=stranger,
        now=now,
    )
    one_sided = await service.confirm_relationship(
        relationship_id=stranger_invite.relationship_id,
        person_id=stranger,
        now=now,
    )
    # The row really is a one-sided declaration; the scope check is what
    # refuses it, not a missing confirmation.
    assert one_sided.status == "pending"
    assert one_sided.confirmed_by_source_at is not None
    assert one_sided.confirmed_by_target_at is None

    assert await service.declared_guardians(subject_person_id=child) == ()

    await _declare_owner_guardianship(service, owner=owner, child=child, now=now)
    await service.create_binding(
        device_id="dev-declared-owner",
        declared_mode="parent_for_child",
        account_owner_person_id=owner,
        primary_subject_ids=(child,),
        roles=((owner, "guardian"), (owner, "device_admin")),
        service_profile_version="parent_for_child-v1",
        policy_bundle_version="multi-subject-v1",
        now=now,
    )
    # Only the binding-owning declarant resolves; the stranger stays out even
    # though their pending declaration is still in the store.
    assert await service.declared_guardians(subject_person_id=child) == (owner,)

    with pytest.raises(ModeConstraintError, match="account owner"):
        await service.create_binding(
            device_id="dev-stranger-guardian",
            declared_mode="parent_for_child",
            account_owner_person_id=owner,
            primary_subject_ids=(child,),
            roles=((stranger, "guardian"),),
            service_profile_version="parent_for_child-v1",
            policy_bundle_version="multi-subject-v1",
            now=now,
        )


@pytest.mark.asyncio
async def test_declared_guardian_drops_when_revoked_or_expired(
    service: IdentityService,
) -> None:
    """撤销或过期后不再作为声明监护人通知收件人。"""

    now = _now()
    owner = await _adult(service, "家长", now)
    child = await _minor(service, "孩子", now)
    relationship_id = await _declare_owner_guardianship(
        service, owner=owner, child=child, now=now
    )
    await service.create_binding(
        device_id="dev-declared-revoke",
        declared_mode="parent_for_child",
        account_owner_person_id=owner,
        primary_subject_ids=(child,),
        roles=((owner, "guardian"), (owner, "device_admin")),
        service_profile_version="parent_for_child-v1",
        policy_bundle_version="multi-subject-v1",
        now=now,
    )
    assert await service.declared_guardians(subject_person_id=child, now=now) == (
        owner,
    )

    await service.revoke_relationship(
        relationship_id=relationship_id,
        actor_person_id=owner,
        evidence_id="evidence-revoke-guardian",
        now=now,
    )
    assert await service.declared_guardians(subject_person_id=child, now=now) == ()

    # A second declaration that expires must drop out on its own.
    expiring = await service.propose_relationship(
        source_person_id=owner,
        target_person_id=child,
        relation_type="guardian_of",
        established_evidence_id="guardian_declaration_v1:device_binding",
        valid_until=now + timedelta(days=1),
        actor_person_id=owner,
        now=now,
    )
    await service.confirm_relationship(
        relationship_id=expiring.relationship_id,
        person_id=owner,
        now=now,
    )
    assert await service.declared_guardians(subject_person_id=child, now=now) == (
        owner,
    )
    assert (
        await service.declared_guardians(
            subject_person_id=child, now=now + timedelta(days=2)
        )
        == ()
    )


@pytest.mark.asyncio
async def test_persona_assignment_crud_is_idempotent_and_subject_scoped(
    service: IdentityService,
    store: InMemoryIdentityStore,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    child = await _adult(service, "孩子", now)
    manifest = await service.create_binding(
        device_id="dev-persona",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        persona_assignment_id="starlight:v1",
        now=now,
    )
    binding_id = manifest.binding_id

    # No explicit assignment yet: the lazy fallback is the binding default.
    assert (
        await service.get_persona_assignment(
            binding_id=binding_id, subject_id=child
        )
        is None
    )
    assert await service.list_persona_assignments(binding_id=binding_id) == ()

    record = await service.set_persona_assignment(
        binding_id=binding_id,
        subject_id=child,
        persona_selection="taoxi",
        actor_person_id=owner,
        now=now,
    )
    assert record.binding_id == binding_id
    assert record.subject_id == child
    assert record.assignment_id == "taoxi:v1"
    assert record.persona_id == "taoxi"
    assert record.persona_version == 1

    # An exact replay is idempotent: the persisted row is returned unchanged.
    replay = await service.set_persona_assignment(
        binding_id=binding_id,
        subject_id=child,
        persona_selection="taoxi",
        actor_person_id=owner,
        now=now + timedelta(minutes=1),
    )
    assert replay.created_at == record.created_at
    assert replay.updated_at == record.updated_at

    # A different persona overwrites the override but keeps created_at.
    replaced = await service.set_persona_assignment(
        binding_id=binding_id,
        subject_id=child,
        persona_selection="mianmian",
        actor_person_id=owner,
        now=now + timedelta(minutes=2),
    )
    assert replaced.persona_id == "mianmian"
    assert replaced.assignment_id == "mianmian:v1"
    assert replaced.created_at == record.created_at
    assert replaced.updated_at > record.updated_at

    listed = await service.list_persona_assignments(binding_id=binding_id)
    assert len(listed) == 1
    assert listed[0].subject_id == child
    assert listed[0].persona_id == "mianmian"

    # Deleting the override restores the lazy binding-default fallback.
    assert (
        await service.delete_persona_assignment(
            binding_id=binding_id, subject_id=child, now=now + timedelta(minutes=3)
        )
        is True
    )
    assert (
        await service.get_persona_assignment(
            binding_id=binding_id, subject_id=child
        )
        is None
    )
    assert (
        await service.delete_persona_assignment(
            binding_id=binding_id, subject_id=child, now=now + timedelta(minutes=4)
        )
        is False
    )


@pytest.mark.asyncio
async def test_persona_assignment_rejects_non_canonical_selection(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    manifest = await service.create_binding(
        device_id="dev-persona-invalid",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    with pytest.raises(ValueError):
        await service.set_persona_assignment(
            binding_id=manifest.binding_id,
            subject_id=owner,
            persona_selection="",
            actor_person_id=owner,
            now=now,
        )
    with pytest.raises(ValueError):
        await service.set_persona_assignment(
            binding_id=manifest.binding_id,
            subject_id=owner,
            persona_selection="x" * 40,
            actor_person_id=owner,
            now=now,
        )


def _structured(**overrides: object) -> StructuredPersonaFields:
    base: dict[str, object] = {
        "style_description": "爱追问，偶尔说点冷幽默",
        "warmth": "bright",
        "directness": "direct",
        "response_length": "brief",
        "question_frequency": "frequent",
        "interview_depth": "structured",
        "welcome_text": "嗨，我是小岸。",
        "conversation_instruction": "保持轻快、多追问，不装可爱。",
        "voice_instruction": "声音清亮、语速稍快。",
        "default_voice_emotion": "happy",
        "default_voice_rate": 1.02,
    }
    base.update(overrides)
    return StructuredPersonaFields(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_custom_persona_create_list_get_delete_is_account_scoped(
    service: IdentityService,
    store: InMemoryIdentityStore,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    record = await service.create_custom_persona(
        owner_person_id=owner,
        display_name="小岸",
        structured=_structured(),
        actor_person_id=owner,
        now=now,
    )
    # ``cu_`` + uuid4 hex, canonical id namespace, always v1.
    assert record.persona_id.startswith("cu_")
    assert len(record.persona_id) <= 32
    assert record.persona_version == 1
    assert record.fallback_designed_voice == "starlight"
    assert record.source == "user_created"

    listed = await service.list_custom_personas(
        owner_person_id=owner, actor_person_id=owner
    )
    assert [item.persona_id for item in listed] == [record.persona_id]

    fetched = await service.get_custom_persona(
        record.persona_id, owner_person_id=owner, actor_person_id=owner
    )
    assert fetched == record

    # Another account can neither see nor delete it.
    other = await _adult(service, "别人", now)
    assert (
        await service.list_custom_personas(
            owner_person_id=other, actor_person_id=other
        )
        == ()
    )
    with pytest.raises(IdentityNotFoundError):
        await service.get_custom_persona(
            record.persona_id, owner_person_id=other, actor_person_id=other
        )
    foreign = await service.delete_custom_persona(
        persona_id=record.persona_id, owner_person_id=other, actor_person_id=other
    )
    assert foreign.deleted is False
    assert (
        await service.get_custom_persona(
            record.persona_id, owner_person_id=owner, actor_person_id=owner
        )
        == record
    )

    outcome = await service.delete_custom_persona(
        persona_id=record.persona_id, owner_person_id=owner, actor_person_id=owner,
        now=now + timedelta(minutes=1),
    )
    assert outcome.deleted is True
    assert outcome.drifted_subjects == 0
    assert (
        await service.list_custom_personas(
            owner_person_id=owner, actor_person_id=owner
        )
        == ()
    )
    # Deleting again is an explicit not-found (idempotent-ish, no error).
    assert (
        await service.delete_custom_persona(
            persona_id=record.persona_id, owner_person_id=owner, actor_person_id=owner
        )
    ).deleted is False
    # The audit trail recorded create + delete.
    if isinstance(store, InMemoryIdentityStore):
        actions = [event.action for event in store._audit]
        assert "persona.custom.create" in actions
        assert "persona.custom.delete" in actions


@pytest.mark.asyncio
async def test_custom_persona_delete_drops_referencing_assignments(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    child = await _adult(service, "孩子", now)
    record = await service.create_custom_persona(
        owner_person_id=owner, display_name="小岸", structured=_structured(),
        actor_person_id=owner, now=now,
    )
    manifest = await service.create_binding(
        device_id="dev-custom-persona",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    await service.set_persona_assignment(
        binding_id=manifest.binding_id,
        subject_id=child,
        persona_selection=record.persona_id,
        actor_person_id=owner,
        now=now,
    )
    assert (
        await service.count_custom_persona_references(
            record.persona_id, actor_person_id=owner
        )
        == 1
    )
    outcome = await service.delete_custom_persona(
        persona_id=record.persona_id, owner_person_id=owner, actor_person_id=owner,
        now=now + timedelta(minutes=1),
    )
    assert outcome.deleted is True
    assert outcome.drifted_subjects == 1
    # No dangling override: the subject falls back to the binding default.
    assert (
        await service.get_persona_assignment(
            binding_id=manifest.binding_id, subject_id=child
        )
        is None
    )


@pytest.mark.asyncio
async def test_custom_persona_limit_is_enforced(service: IdentityService) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    for index in range(5):
        await service.create_custom_persona(
            owner_person_id=owner, display_name=f"人格{index}", structured=_structured(),
            actor_person_id=owner, now=now + timedelta(seconds=index),
        )
    assert await service.count_custom_personas(
        owner_person_id=owner, actor_person_id=owner
    ) == 5
    with pytest.raises(CustomPersonaLimitError) as excinfo:
        await service.create_custom_persona(
            owner_person_id=owner, display_name="第六个", structured=_structured(),
            actor_person_id=owner, now=now + timedelta(minutes=1),
        )
    assert excinfo.value.limit == 5


@pytest.mark.asyncio
async def test_custom_persona_only_owner_may_create(service: IdentityService) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    member = await _adult(service, "成员", now)
    with pytest.raises(IdentityAccessDeniedError):
        await service.create_custom_persona(
            owner_person_id=owner, display_name="越权", structured=_structured(),
            actor_person_id=member, now=now,
        )


@pytest.mark.asyncio
async def test_custom_persona_idempotent_replay_returns_same_record(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    first = await service.create_custom_persona(
        owner_person_id=owner, display_name="小岸", structured=_structured(),
        idempotency_key="create-1", actor_person_id=owner, now=now,
    )
    replay = await service.create_custom_persona(
        owner_person_id=owner, display_name="小岸", structured=_structured(),
        idempotency_key="create-1", actor_person_id=owner,
        now=now + timedelta(minutes=5),
    )
    assert replay.persona_id == first.persona_id
    assert await service.count_custom_personas(
        owner_person_id=owner, actor_person_id=owner
    ) == 1
    # Same key, different content is a conflict (never a silent second row).
    with pytest.raises(IdentityConflictError):
        await service.create_custom_persona(
            owner_person_id=owner, display_name="改名", structured=_structured(),
            idempotency_key="create-1", actor_person_id=owner, now=now,
        )


@pytest.mark.asyncio
async def test_custom_persona_rejects_out_of_domain_fields(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    # Out-of-domain enum values are refused (fail closed, never clamped).
    with pytest.raises(ValueError):
        await service.create_custom_persona(
            owner_person_id=owner, display_name="坏值",
            structured=_structured(warmth="甜"), actor_person_id=owner, now=now,
        )
    # Names and rate bounds mirror the database CHECK constraints.
    with pytest.raises(ValueError):
        await service.create_custom_persona(
            owner_person_id=owner, display_name="名字" * 9,
            structured=_structured(), actor_person_id=owner, now=now,
        )
    with pytest.raises(ValueError):
        await service.create_custom_persona(
            owner_person_id=owner, display_name="语速",
            structured=_structured(default_voice_rate=1.5),
            actor_person_id=owner, now=now,
        )
    assert await service.list_custom_personas(
        owner_person_id=owner, actor_person_id=owner
    ) == ()


@pytest.mark.asyncio
async def test_persona_assignment_accepts_own_custom_and_rejects_foreign(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "主人", now)
    other_owner = await _adult(service, "别人", now)
    mine = await service.create_custom_persona(
        owner_person_id=owner, display_name="小岸", structured=_structured(),
        actor_person_id=owner, now=now,
    )
    theirs = await service.create_custom_persona(
        owner_person_id=other_owner, display_name="他人人格", structured=_structured(),
        actor_person_id=other_owner, now=now,
    )
    manifest = await service.create_binding(
        device_id="dev-custom-assignment",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    record = await service.set_persona_assignment(
        binding_id=manifest.binding_id,
        subject_id=owner,
        persona_selection=mine.persona_id,
        actor_person_id=owner,
        now=now,
    )
    assert record.persona_id == mine.persona_id
    assert record.assignment_id == f"{mine.persona_id}:v1"
    # A custom persona owned by another account is refused (cross-account).
    with pytest.raises(ValueError):
        await service.set_persona_assignment(
            binding_id=manifest.binding_id,
            subject_id=owner,
            persona_selection=theirs.persona_id,
            actor_person_id=owner,
            now=now,
        )


@pytest.mark.asyncio
async def test_crisis_contacts_reach_an_elders_binding_delegate(
    service: IdentityService,
) -> None:
    """P0-04 D7: an elder's crisis reaches the child who bound the device."""

    now = _now()
    child_buyer = await _adult(service, "子女", now)
    father = await _adult(service, "父亲", now)
    stranger = await _adult(service, "外人", now)
    for relation_type in ("child_of", "emergency_contact_for"):
        await _establish_relationship(
            service, relation_type=relation_type, source=child_buyer, target=father, now=now
        )
    await service.attest_binding_relationship(
        source_person_id=child_buyer,
        target_person_id=father,
        relation_type="delegate_for",
        actor_person_id=child_buyer,
        now=now,
    )
    # An attestation without the binding is not enough.
    assert await service.crisis_contacts(subject_person_id=father, now=now) == ()

    await service.create_binding(
        device_id="dev-senior-crisis",
        declared_mode="child_for_parent",
        account_owner_person_id=child_buyer,
        primary_subject_ids=(father,),
        roles=((child_buyer, "device_admin"), (child_buyer, "emergency_contact")),
        service_profile_version="senior-v1",
        policy_bundle_version="policy-senior-v1",
        now=now,
    )

    assert await service.crisis_contacts(subject_person_id=father, now=now) == (child_buyer,)
    assert stranger not in await service.crisis_contacts(subject_person_id=father, now=now)
    # The elder is still nobody's declared guardian ward.
    assert await service.declared_guardians(subject_person_id=father, now=now) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("guardianship", ["attested", "declared"])
async def test_declared_owner_guardian_is_also_the_childs_emergency_contact(
    service: IdentityService,
    guardianship: str,
) -> None:
    """Binding a child with the emergency-contact offer must not conflict.

    The Mini Program adds the account owner as ``emergency_contact`` when the
    emergency offer is accepted; the owner's own ``guardian_of`` already makes
    them the child's crisis contact, so no second ``emergency_contact_for``
    relationship is needed. A non-owner still is.

    The device-binding route records the owner's attestation, an *active*
    relationship; 20260927-child-binding-v1 only accepted the pending
    declaration and kept refusing every real child binding.
    """

    now = _now()
    owner = await _adult(service, "家长", now)
    child = await _minor(service, "孩子", now)
    other = await _adult(service, "外人", now)
    if guardianship == "attested":
        await service.attest_binding_relationship(
            source_person_id=owner,
            target_person_id=child,
            relation_type="guardian_of",
            actor_person_id=owner,
            now=now,
        )
    else:
        await _declare_owner_guardianship(service, owner=owner, child=child, now=now)

    manifest = await service.create_binding(
        device_id="dev-child-emergency",
        declared_mode="parent_for_child",
        account_owner_person_id=owner,
        primary_subject_ids=(child,),
        roles=((owner, "guardian"), (owner, "device_admin"), (owner, "emergency_contact")),
        service_profile_version="parent_for_child-v1",
        policy_bundle_version="multi-subject-v1",
        now=now,
    )
    assert manifest.declared_mode == "parent_for_child"

    with pytest.raises(ModeConstraintError, match="emergency_contact_for"):
        await service.create_binding(
            device_id="dev-child-emergency-other",
            declared_mode="parent_for_child",
            account_owner_person_id=owner,
            primary_subject_ids=(child,),
            roles=((owner, "guardian"), (owner, "device_admin"), (other, "emergency_contact")),
            service_profile_version="parent_for_child-v1",
            policy_bundle_version="multi-subject-v1",
            now=now,
        )
