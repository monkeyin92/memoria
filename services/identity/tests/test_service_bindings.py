"""IdentityService device bindings: mode constraints, permissions, lifecycle,
manifest reads and ownership transfer intents."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from services.identity.domain import (
    ROLE_DEFAULT_PERMISSIONS,
    BindingManifest,
    BindingVersionConflictError,
    DeviceBinding,
    DeviceBindingRole,
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    ModeConstraintError,
    RoleConstraintError,
    TransferLifecycleError,
    effective_permissions,
    manifest_from_dict,
)
from services.identity.in_memory_store import InMemoryIdentityStore
from services.identity.service import IdentityService
from services.identity.tests.service_test_support import (
    _adult,
    _establish_guardian,
    _establish_relationship,
    _minor,
    _now,
    _receipt,
    _register,
)

# Re-exported (not redefined) so pytest discovers the fixture by name.
from services.identity.tests.service_test_support import (
    service as service,
)
from services.identity.tests.service_test_support import (
    store as store,
)


@pytest.mark.asyncio
async def test_unknown_subject_can_bind_self_but_remains_fail_closed(
    service: IdentityService,
) -> None:
    now = _now()
    unknown = await _register(service, name="未知", now=now)
    manifest = await service.create_binding(
        device_id="dev-self-unknown",
        declared_mode="self_use",
        account_owner_person_id=unknown,
        primary_subject_ids=(unknown,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )

    assert manifest.primary_subject_ids == (unknown,)
    assert manifest.account_owner_id == unknown


@pytest.mark.asyncio
async def test_parent_for_child_mode_requires_minor_and_guardian(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    await _establish_guardian(service, guardian=parent, ward=child, now=now)
    manifest = await service.create_binding(
        device_id="dev-child",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"),),
        service_profile_version="student-cn-v3",
        policy_bundle_version="policy-cn-minor-v5",
        now=now,
    )
    assert manifest.declared_mode == "parent_for_child"
    assert manifest.primary_subject_ids == (child,)
    assert manifest.guardian_ids == (parent,)
    assert manifest.account_owner_id == parent


@pytest.mark.asyncio
async def test_parent_for_child_rejects_missing_guardian_and_non_binding_role(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    with pytest.raises(ModeConstraintError, match="guardian"):
        await service.create_binding(
            device_id="dev-child-bad",
            declared_mode="parent_for_child",
            account_owner_person_id=parent,
            primary_subject_ids=(child,),
            service_profile_version="student-cn-v3",
            policy_bundle_version="policy-cn-minor-v5",
            now=now,
        )
    with pytest.raises(RoleConstraintError, match="unknown binding role"):
        await service.create_binding(
            device_id="dev-child-bad2",
            declared_mode="parent_for_child",
            account_owner_person_id=parent,
            primary_subject_ids=(child,),
            roles=((parent, "guardian"), (parent, "beneficiary")),  # type: ignore[arg-type]
            service_profile_version="student-cn-v3",
            policy_bundle_version="policy-cn-minor-v5",
            now=now,
        )


@pytest.mark.asyncio
async def test_self_use_mode_binds_owner_as_subject(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    relative = await _adult(service, "亲友", now)
    manifest = await service.create_binding(
        device_id="dev-self",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    assert manifest.primary_subject_ids == (owner,)
    with pytest.raises(ModeConstraintError):
        await service.create_binding(
            device_id="dev-self-bad",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            roles=((relative, "guardian"),),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now,
        )


@pytest.mark.asyncio
async def test_child_for_parent_mode_separates_payer_from_subject(
    service: IdentityService,
) -> None:
    now = _now()
    child_buyer = await _adult(service, "子女", now)
    father = await _adult(service, "父亲", now)
    await _establish_relationship(
        service,
        relation_type="child_of",
        source=child_buyer,
        target=father,
        now=now,
    )
    await _establish_relationship(
        service,
        relation_type="emergency_contact_for",
        source=child_buyer,
        target=father,
        now=now,
    )
    manifest = await service.create_binding(
        device_id="dev-senior",
        declared_mode="child_for_parent",
        account_owner_person_id=child_buyer,
        primary_subject_ids=(father,),
        roles=((child_buyer, "device_admin"), (child_buyer, "emergency_contact")),
        service_profile_version="senior-v1",
        policy_bundle_version="policy-senior-v1",
        now=now,
    )
    assert manifest.primary_subject_ids == (father,)
    assert manifest.account_owner_id == child_buyer
    assert child_buyer in manifest.device_admin_ids
    assert child_buyer in manifest.emergency_contact_ids


@pytest.mark.asyncio
async def test_child_for_parent_requires_admin_and_emergency_contact(
    service: IdentityService,
) -> None:
    now = _now()
    buyer = await _adult(service, "子女", now)
    father = await _adult(service, "父亲", now)
    with pytest.raises(ModeConstraintError, match="device_admin"):
        await service.create_binding(
            device_id="dev-senior-bad",
            declared_mode="child_for_parent",
            account_owner_person_id=buyer,
            primary_subject_ids=(father,),
            roles=((buyer, "emergency_contact"),),
            service_profile_version="senior-v1",
            policy_bundle_version="policy-senior-v1",
            now=now,
        )
    with pytest.raises(ModeConstraintError, match="emergency contact"):
        await service.create_binding(
            device_id="dev-senior-bad2",
            declared_mode="child_for_parent",
            account_owner_person_id=buyer,
            primary_subject_ids=(father,),
            roles=((buyer, "device_admin"),),
            service_profile_version="senior-v1",
            policy_bundle_version="policy-senior-v1",
            now=now,
        )


@pytest.mark.asyncio
async def test_family_shared_mode_requires_family_space_and_members(
    service: IdentityService,
) -> None:
    now = _now()
    parent = await _adult(service, "家长", now)
    child_a = await _minor(service, "孩子A", now)
    child_b = await _minor(service, "孩子B", now)
    await _establish_guardian(service, guardian=parent, ward=child_a, now=now)
    await _establish_guardian(service, guardian=parent, ward=child_b, now=now)
    manifest = await service.create_binding(
        device_id="dev-family",
        declared_mode="family_shared",
        account_owner_person_id=parent,
        primary_subject_ids=(child_a, child_b),
        roles=((parent, "guardian"), (parent, "member")),
        family_space_id="family-1",
        service_profile_version="family-v1",
        policy_bundle_version="policy-family-v1",
        now=now,
    )
    assert manifest.family_space_id == "family-1"
    assert set(manifest.primary_subject_ids) == {child_a, child_b}
    assert manifest.guardian_ids == (parent,)
    assert manifest.member_ids == (parent,)
    with pytest.raises(ModeConstraintError, match="family_space_id"):
        await service.create_binding(
            device_id="dev-family-bad",
            declared_mode="family_shared",
            account_owner_person_id=parent,
            primary_subject_ids=(child_a,),
            roles=((parent, "guardian"),),
            service_profile_version="family-v1",
            policy_bundle_version="policy-family-v1",
            now=now,
        )


@pytest.mark.asyncio
async def test_payer_and_admin_have_no_content_read_rights(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    aunt = await _adult(service, "姑姑", now)
    await _establish_guardian(service, guardian=parent, ward=child, now=now)
    await _establish_guardian(service, guardian=aunt, ward=child, now=now)
    manifest = await service.create_binding(
        device_id="dev-boundary",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"), (aunt, "device_admin")),
        service_profile_version="student-cn-v3",
        policy_bundle_version="policy-cn-minor-v5",
        now=now,
    )
    assert "content.read" not in effective_permissions(manifest, parent)
    assert "memory.private.read" not in effective_permissions(manifest, parent)
    assert "content.read" not in effective_permissions(manifest, aunt)
    assert "memory.guardian.summary" in effective_permissions(manifest, parent)
    assert "content.read" in effective_permissions(manifest, child)


@pytest.mark.asyncio
async def test_device_admin_cannot_revoke_or_transfer_binding(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    aunt = await _adult(service, "姑姑", now)
    await _establish_guardian(service, guardian=parent, ward=child, now=now)
    await _establish_guardian(service, guardian=aunt, ward=child, now=now)
    await service.create_binding(
        device_id="dev-boundary2",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"), (aunt, "device_admin")),
        service_profile_version="student-cn-v3",
        policy_bundle_version="policy-cn-minor-v5",
        now=now,
    )
    with pytest.raises(IdentityAccessDeniedError):
        await service.revoke_binding(
            device_id="dev-boundary2", actor_person_id=aunt, now=now
        )
    with pytest.raises(IdentityAccessDeniedError):
        await service.create_transfer_intent(
            device_id="dev-boundary2",
            to_account_owner_person_id=aunt,
            step_up_evidence_id="step-up-1",
            policy_receipt_id="receipt-1",
            idempotency_key=str(uuid.uuid4()),
            actor_person_id=aunt,
            now=now,
        )


@pytest.mark.asyncio
async def test_guardian_cannot_self_declare_and_unknown_permissions_rejected(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    with pytest.raises(RoleConstraintError, match="guardian"):
        await service.create_binding(
            device_id="dev-bad-role",
            declared_mode="parent_for_child",
            account_owner_person_id=child,
            primary_subject_ids=(child,),
            roles=((child, "guardian"),),
            service_profile_version="student-cn-v3",
            policy_bundle_version="policy-cn-minor-v5",
            now=now,
        )
    parent = await _adult(service, "家长", now)
    with pytest.raises(RoleConstraintError, match="permissions"):
        await service.create_binding(
            device_id="dev-bad-perms",
            declared_mode="parent_for_child",
            account_owner_person_id=parent,
            primary_subject_ids=(child,),
            roles=((parent, "guardian"),),
            role_permissions={((parent, "guardian")): frozenset({"read.everything"})},
            service_profile_version="student-cn-v3",
            policy_bundle_version="policy-cn-minor-v5",
            now=now,
        )


@pytest.mark.asyncio
async def test_supersede_keeps_monotonic_versions_and_audit_trail(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    await _establish_guardian(service, guardian=parent, ward=child, now=now)
    v1 = await service.create_binding(
        device_id="dev-versions",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"),),
        service_profile_version="student-cn-v3",
        policy_bundle_version="policy-cn-minor-v5",
        now=now,
    )
    v2 = await service.supersede_binding(
        device_id="dev-versions",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"),),
        service_profile_version="student-cn-v4",
        policy_bundle_version="policy-cn-minor-v6",
        actor_person_id=parent,
        now=now + timedelta(minutes=1),
    )
    assert v2.binding_version == 2
    assert v2.supersedes_binding_id == v1.binding_id
    versions = await service.list_binding_versions("dev-versions")
    assert [version.binding_version for version in versions] == [1, 2]
    assert versions[0].status == "superseded"
    assert versions[1].status == "active"
    active = await service.get_active_manifest(
        "dev-versions", now=now + timedelta(minutes=2)
    )
    assert active is not None and active.binding_version == 2
    assert active.service_profile_version == "student-cn-v4"
    v3 = await service.supersede_binding(
        device_id="dev-versions",
        declared_mode="self_use",
        account_owner_person_id=parent,
        primary_subject_ids=(parent,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        actor_person_id=parent,
        now=now + timedelta(minutes=3),
    )
    assert v3.binding_version == 3
    assert v3.declared_mode == "self_use"
    assert len(await service.list_binding_versions("dev-versions")) == 3


@pytest.mark.asyncio
async def test_supersede_rejects_a_stale_expected_binding_id(
    service: IdentityService,
) -> None:
    """Compare-and-set: a stale derivation cannot overwrite a newer version.

    Two concurrent appends both derive their subject list from version 1; the
    loser retries against version 2 instead of silently dropping the winner's
    member.  The stale attempt must leave no trace: same active subjects, same
    version history, no extra audit row.
    """
    now = _now()
    owner = await _adult(service, "本人", now)
    first = await _minor(service, "老大", now)
    second = await _minor(service, "老二", now)
    v1 = await service.create_binding(
        device_id="dev-cas",
        declared_mode="family_shared",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        family_space_id="family-cas",
        service_profile_version="family-v1",
        policy_bundle_version="policy-family-v1",
        now=now,
    )
    v2 = await service.supersede_binding(
        device_id="dev-cas",
        declared_mode="family_shared",
        primary_subject_ids=(owner, first),
        family_space_id="family-cas",
        service_profile_version="family-v1",
        policy_bundle_version="policy-family-v1",
        actor_person_id=owner,
        now=now + timedelta(minutes=1),
        expected_binding_id=v1.binding_id,
    )
    assert v2.binding_version == 2
    with pytest.raises(BindingVersionConflictError, match="changed"):
        await service.supersede_binding(
            device_id="dev-cas",
            declared_mode="family_shared",
            primary_subject_ids=(owner, second),
            family_space_id="family-cas",
            service_profile_version="family-v1",
            policy_bundle_version="policy-family-v1",
            actor_person_id=owner,
            now=now + timedelta(minutes=2),
            expected_binding_id=v1.binding_id,
        )
    active = await service.get_active_manifest("dev-cas", now=now + timedelta(minutes=3))
    assert active is not None
    assert active.binding_version == 2
    assert set(active.primary_subject_ids) == {owner, first}
    assert [
        manifest.binding_version
        for manifest in await service.list_binding_versions("dev-cas")
    ] == [1, 2]
    v3 = await service.supersede_binding(
        device_id="dev-cas",
        declared_mode="family_shared",
        primary_subject_ids=(owner, first, second),
        family_space_id="family-cas",
        service_profile_version="family-v1",
        policy_bundle_version="policy-family-v1",
        actor_person_id=owner,
        now=now + timedelta(minutes=4),
        expected_binding_id=v2.binding_id,
    )
    assert v3.binding_version == 3
    assert set(v3.primary_subject_ids) == {owner, first, second}
    assert v3.family_space_id == "family-cas"


@pytest.mark.asyncio
async def test_revoke_unbind_preserves_history(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    manifest = await service.create_binding(
        device_id="dev-revoke",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    revoked = await service.revoke_binding(
        device_id="dev-revoke", actor_person_id=owner, reason="device_lost", now=now
    )
    assert revoked.binding.status == "revoked"
    assert await service.get_active_manifest("dev-revoke", now=now) is None
    versions = await service.list_binding_versions("dev-revoke")
    assert len(versions) == 1
    assert versions[0].status == "revoked"
    assert versions[0].binding_id == manifest.binding_id
    with pytest.raises(IdentityNotFoundError):
        await service.revoke_binding(
            device_id="dev-revoke", actor_person_id=owner, now=now
        )


@pytest.mark.asyncio
async def test_expire_timeboxed_binding(service: IdentityService) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    await service.create_binding(
        device_id="dev-expiry",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        valid_until=now + timedelta(hours=1),
        now=now,
    )
    assert await service.get_active_manifest(
        "dev-expiry", now=now + timedelta(hours=2)
    ) is None
    assert await service.expire_bindings(now=now + timedelta(hours=2)) == 1
    versions = await service.list_binding_versions("dev-expiry")
    assert versions[0].status == "expired"


@pytest.mark.asyncio
async def test_list_active_manifests_for_person_is_scoped_to_owner_and_active_roles(
    service: IdentityService,
    store: InMemoryIdentityStore,
) -> None:
    now = _now()
    owner = await _adult(service, "列表主人", now)
    member = await _adult(service, "列表成员", now)
    child = await _minor(service, "列表孩子", now)
    stranger = await _adult(service, "无关用户", now)
    await _establish_guardian(service, guardian=owner, ward=child, now=now)
    await _establish_relationship(
        service,
        relation_type="family_member_of",
        source=member,
        target=child,
        now=now,
    )

    self_manifest = await service.create_binding(
        device_id="dev-list-self",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    family_manifest = await service.create_binding(
        device_id="dev-list-family",
        declared_mode="family_shared",
        account_owner_person_id=owner,
        primary_subject_ids=(child,),
        roles=((member, "member"),),
        family_space_id="family-list",
        service_profile_version="family-v1",
        policy_bundle_version="policy-family-v1",
        now=now,
    )

    owner_manifests = await service.list_active_manifests_for_person(
        owner, now=now, actor_person_id=owner
    )
    assert [manifest.binding_id for manifest in owner_manifests] == [
        family_manifest.binding_id,
        self_manifest.binding_id,
    ]
    member_manifests = await service.list_active_manifests_for_person(
        member, now=now, actor_person_id=member
    )
    assert [manifest.binding_id for manifest in member_manifests] == [
        family_manifest.binding_id
    ]
    child_manifests = await service.list_active_manifests_for_person(
        child, now=now, actor_person_id=child
    )
    assert [manifest.binding_id for manifest in child_manifests] == [
        family_manifest.binding_id
    ]
    assert await service.list_active_manifests_for_person(
        stranger, now=now, actor_person_id=stranger
    ) == ()

    await service.create_binding(
        device_id="dev-list-revoked",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    await service.revoke_binding(
        device_id="dev-list-revoked",
        actor_person_id=owner,
        now=now + timedelta(minutes=1),
    )
    await service.create_binding(
        device_id="dev-list-superseded",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now + timedelta(minutes=2),
    )
    await service.create_binding(
        device_id="dev-list-expired",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        valid_until=now + timedelta(hours=2),
        now=now + timedelta(minutes=3),
    )
    await service.supersede_binding(
        device_id="dev-list-superseded",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v2",
        policy_bundle_version="policy-self-v2",
        actor_person_id=owner,
        now=now + timedelta(minutes=4),
    )
    await service.expire_bindings(now=now + timedelta(hours=3))
    future_id = "binding-list-future"
    await store.persist_binding(
        DeviceBinding(
            binding_id=future_id,
            device_id="dev-list-future",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            binding_version=1,
            valid_from=now + timedelta(hours=4),
            created_at=now + timedelta(hours=4),
            roles=(
                DeviceBindingRole(
                    binding_id=future_id,
                    person_id=owner,
                    role="account_owner",
                    permissions=frozenset({"binding.manage"}),
                    granted_at=now + timedelta(hours=4),
                ),
            ),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
        )
    )

    active = await service.list_active_manifests_for_person(
        owner,
        now=now + timedelta(hours=3, minutes=1),
        actor_person_id=owner,
    )
    versions = {
        (manifest.device_id, manifest.binding_version) for manifest in active
    }
    assert ("dev-list-self", self_manifest.binding_version) in versions
    assert ("dev-list-family", family_manifest.binding_version) in versions
    assert ("dev-list-superseded", 2) in versions
    assert ("dev-list-revoked", 1) not in versions
    assert ("dev-list-superseded", 1) not in versions
    assert ("dev-list-expired", 1) not in versions
    assert ("dev-list-future", 1) not in versions


@pytest.mark.asyncio
async def test_list_active_bindings_for_person_actor_contract(
    service: IdentityService,
    store: InMemoryIdentityStore,
) -> None:
    """Store list mirrors the Postgres api-role double visibility."""
    now = _now()
    owner = await _adult(service, "契约主人", now)
    member = await _adult(service, "契约成员", now)
    child = await _minor(service, "契约孩子", now)
    stranger = await _adult(service, "契约外人", now)
    await _establish_guardian(service, guardian=owner, ward=child, now=now)
    await _establish_relationship(
        service,
        relation_type="family_member_of",
        source=member,
        target=child,
        now=now,
    )
    self_manifest = await service.create_binding(
        device_id="dev-actor-self",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    family_manifest = await service.create_binding(
        device_id="dev-actor-family",
        declared_mode="family_shared",
        account_owner_person_id=owner,
        primary_subject_ids=(child,),
        roles=((member, "member"),),
        family_space_id="family-actor",
        service_profile_version="family-v1",
        policy_bundle_version="policy-family-v1",
        now=now,
    )

    # 共享可见：actor 是参与者时按 person/actor 双重资格可见，
    # 而不是 actor != person 一律拒绝。
    owner_actors_for_member = await store.list_active_bindings_for_person(
        member, now=now, actor_person_id=owner
    )
    assert [binding.binding_id for binding in owner_actors_for_member] == [
        family_manifest.binding_id
    ]
    member_actors_for_owner = await store.list_active_bindings_for_person(
        owner, now=now, actor_person_id=member
    )
    assert [binding.binding_id for binding in member_actors_for_owner] == [
        family_manifest.binding_id
    ]

    # 无关 actor：person 参与但 actor 与绑定无关 -> 空。
    assert await store.list_active_bindings_for_person(
        member, now=now, actor_person_id=stranger
    ) == ()
    assert await store.list_active_bindings_for_person(
        owner, now=now, actor_person_id=stranger
    ) == ()

    # 已结束/撤销 role 的 actor 失去 actor 资格。
    revoked_id = "binding-actor-revoked"
    await store.persist_binding(
        DeviceBinding(
            binding_id=revoked_id,
            device_id="dev-actor-revoked",
            declared_mode="family_shared",
            account_owner_person_id=owner,
            primary_subject_ids=(child,),
            family_space_id="family-actor",
            binding_version=1,
            valid_from=now,
            created_at=now,
            roles=(
                DeviceBindingRole(
                    binding_id=revoked_id,
                    person_id=owner,
                    role="account_owner",
                    permissions=ROLE_DEFAULT_PERMISSIONS["account_owner"],
                    granted_at=now,
                ),
                DeviceBindingRole(
                    binding_id=revoked_id,
                    person_id=child,
                    role="primary_subject",
                    permissions=ROLE_DEFAULT_PERMISSIONS["primary_subject"],
                    granted_at=now,
                ),
                DeviceBindingRole(
                    binding_id=revoked_id,
                    person_id=member,
                    role="member",
                    permissions=ROLE_DEFAULT_PERMISSIONS["member"],
                    granted_at=now,
                    status="revoked",
                    ended_at=now + timedelta(minutes=1),
                ),
            ),
            service_profile_version="family-v1",
            policy_bundle_version="policy-family-v1",
        )
    )
    with_revoked_actor = await store.list_active_bindings_for_person(
        owner, now=now, actor_person_id=member
    )
    assert [binding.binding_id for binding in with_revoked_actor] == [
        family_manifest.binding_id
    ]

    # 无 actor fail closed；scope 值不解锁（PG api 池策略不看 scope GUC）。
    assert await store.list_active_bindings_for_person(owner, now=now) == ()
    assert await store.list_active_bindings_for_person(
        owner, now=now, actor_person_id=None, scope="migration"
    ) == ()
    migration_scoped = await store.list_active_bindings_for_person(
        owner, now=now, actor_person_id=owner, scope="migration"
    )
    assert {binding.binding_id for binding in migration_scoped} == {
        self_manifest.binding_id,
        family_manifest.binding_id,
        revoked_id,
    }


@pytest.mark.asyncio
async def test_transfer_intent_requires_two_party_accept_and_inherits_nothing(
    service: IdentityService,
) -> None:
    now = _now()
    child = await _minor(service, "孩子", now)
    parent = await _adult(service, "家长", now)
    new_owner = await _adult(service, "新主人", now)
    await _establish_guardian(service, guardian=parent, ward=child, now=now)
    v1 = await service.create_binding(
        device_id="dev-gift",
        declared_mode="parent_for_child",
        account_owner_person_id=parent,
        primary_subject_ids=(child,),
        roles=((parent, "guardian"),),
        service_profile_version="student-cn-v3",
        policy_bundle_version="policy-cn-minor-v5",
        now=now,
    )
    policy_id, step_up = _receipt(
        actor=parent,
        subject=new_owner,
        device="dev-gift",
        binding_id=v1.binding_id,
        binding_version=v1.binding_version,
        expires_at=now + timedelta(days=8),
    )
    intent = await service.create_transfer_intent(
        device_id="dev-gift",
        to_account_owner_person_id=new_owner,
        step_up_evidence_id=step_up,
        policy_receipt_id=policy_id,
        idempotency_key=str(uuid.uuid4()),
        actor_person_id=parent,
        now=now + timedelta(minutes=1),
    )
    assert intent.status == "pending"
    with pytest.raises(IdentityAccessDeniedError):
        await service.accept_transfer_intent(
            transfer_id=intent.transfer_id,
            actor_person_id=parent,
            declared_mode="self_use",
            primary_subject_ids=(parent,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now + timedelta(minutes=2),
            idempotency_key=str(uuid.uuid4()),
        )
    accept_key = str(uuid.uuid4())
    transferred = await service.accept_transfer_intent(
        transfer_id=intent.transfer_id,
        actor_person_id=new_owner,
        primary_subject_ids=(new_owner,),
        declared_mode="self_use",
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now + timedelta(minutes=3),
        idempotency_key=accept_key,
    )
    assert transferred.binding_version == 2
    assert transferred.reason == "transfer"
    assert transferred.account_owner_id == new_owner
    assert transferred.primary_subject_ids == (new_owner,)
    assert transferred.guardian_ids == ()
    assert transferred.device_admin_ids == ()
    assert all(role.person_id != child for role in transferred.roles)
    assert all(role.person_id != parent for role in transferred.roles)
    replayed = await service.accept_transfer_intent(
        transfer_id=intent.transfer_id,
        actor_person_id=new_owner,
        primary_subject_ids=(new_owner,),
        declared_mode="self_use",
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now + timedelta(minutes=4),
        idempotency_key=accept_key,
    )
    assert replayed.binding_id == transferred.binding_id
    versions = await service.list_binding_versions("dev-gift")
    assert len(versions) == 2
    assert versions[0].binding_id == v1.binding_id
    assert versions[0].status == "superseded"
    assert versions[0].primary_subject_ids == (child,)
    assert versions[1].status == "active"
    active = await service.get_active_manifest(
        "dev-gift", now=now + timedelta(minutes=6)
    )
    assert active is not None
    assert active.primary_subject_ids == (new_owner,)


@pytest.mark.asyncio
async def test_transfer_intent_requires_different_owner_and_evidence(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    other = await _adult(service, "其他人", now)
    await service.create_binding(
        device_id="dev-gift2",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    with pytest.raises(IdentityConflictError, match="different account owner"):
        await service.create_transfer_intent(
            device_id="dev-gift2",
            to_account_owner_person_id=owner,
            step_up_evidence_id="step-up-evidence-1",
            policy_receipt_id="policy-receipt-1",
            idempotency_key=str(uuid.uuid4()),
            actor_person_id=owner,
            now=now,
        )
    with pytest.raises(IdentityAccessDeniedError):
        await service.create_transfer_intent(
            device_id="dev-gift2",
            to_account_owner_person_id=other,
            step_up_evidence_id="step-up-evidence-1",
            policy_receipt_id="policy-receipt-1",
            idempotency_key=str(uuid.uuid4()),
            actor_person_id=other,
            now=now,
        )
    with pytest.raises(TransferLifecycleError, match="step-up"):
        await service.create_transfer_intent(
            device_id="dev-gift2",
            to_account_owner_person_id=other,
            step_up_evidence_id=" ",
            policy_receipt_id="policy-receipt-1",
            idempotency_key=str(uuid.uuid4()),
            actor_person_id=owner,
            now=now,
        )


@pytest.mark.asyncio
async def test_get_active_manifest_is_serializable_round_trip(
    service: IdentityService,
) -> None:
    now = _now()
    owner = await _adult(service, "本人", now)
    await service.create_binding(
        device_id="dev-json",
        declared_mode="self_use",
        account_owner_person_id=owner,
        primary_subject_ids=(owner,),
        service_profile_version="self-v1",
        policy_bundle_version="policy-self-v1",
        now=now,
    )
    manifest = await service.get_active_manifest("dev-json", now=now)
    assert manifest is not None
    assert isinstance(manifest, BindingManifest)
    decoded = manifest_from_dict(json.loads(manifest.to_json()))
    assert decoded.to_dict() == manifest.to_dict()
