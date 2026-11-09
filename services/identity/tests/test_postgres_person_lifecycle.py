"""Identity Postgres: personas, transfer intent, guardianship, audit and deletion."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from services.identity.authority import (
    TransferVerificationError,
)
from services.identity.domain import (
    CustomPersonaLimitError,
    ModeConstraintError,
    StructuredPersonaFields,
)
from services.identity.postgres_store import PostgresIdentityStore
from services.identity.service import REDACTED_DISPLAY_NAME, IdentityService
from services.identity.tests.identity_pg_test_support import (
    _bootstrap,
    _drop_database,
    _service,
    pytestmark,  # noqa: F401 - pytest discovers the module-wide marker
)


async def test_transfer_intent_two_party_and_atomic_events() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
        owner = (
            await service.register_person(
                display_name="本人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-pg-adult",
                now=now,
            )
        ).person_id
        target = (
            await service.register_person(
                display_name="新主人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-pg-adult",
                now=now,
            )
        ).person_id
        v1 = await service.create_binding(
            device_id="dev-pg-tx",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now,
        )
        policy_id, step_up = _pg_authority.mint(
            actor_person_id=owner,
            subject_person_id=target,
            device_id="dev-pg-tx",
            binding_id=v1.binding_id,
            binding_version=v1.binding_version,
            expires_at=now + timedelta(days=8),
        )
        intent = await service.create_transfer_intent(
            device_id="dev-pg-tx",
            to_account_owner_person_id=target,
            step_up_evidence_id=step_up,
            policy_receipt_id=policy_id,
            idempotency_key="idem-pg-0001",
            actor_person_id=owner,
            now=now + timedelta(minutes=1),
        )
        # Idempotent replay with the same key returns the same intent.
        replayed = await service.create_transfer_intent(
            device_id="dev-pg-tx",
            to_account_owner_person_id=target,
            step_up_evidence_id=step_up,
            policy_receipt_id=policy_id,
            idempotency_key="idem-pg-0001",
            actor_person_id=owner,
            now=now + timedelta(minutes=2),
        )
        assert replayed.transfer_id == intent.transfer_id
        # Forged tickets never create an intent.
        with pytest.raises(TransferVerificationError):
            await service.create_transfer_intent(
                device_id="dev-pg-tx",
                to_account_owner_person_id=target,
                step_up_evidence_id="step-up-1",
                policy_receipt_id="forged-receipt-id",
                idempotency_key="idem-pg-0002",
                actor_person_id=owner,
                now=now + timedelta(minutes=3),
            )
        accepted = await service.accept_transfer_intent(
            transfer_id=intent.transfer_id,
            actor_person_id=target,
            primary_subject_ids=(target,),
            declared_mode="self_use",
            now=now + timedelta(minutes=4),
            idempotency_key=str(uuid.uuid4()),
        )
        assert accepted.binding_version == 2
        assert accepted.account_owner_id == target
        assert all(role.person_id != owner for role in accepted.roles)

        admin = await asyncpg.connect(dsns["admin"])
        try:
            audit_actions = {
                str(row["action"])
                for row in await admin.fetch(
                    "SELECT action FROM identity_audit_events"
                )
            }
            assert "transfer.create" in audit_actions
            assert "transfer.accept" in audit_actions
            assert "binding.transfer" in audit_actions
            outbox_topics = {
                str(row["topic"])
                for row in await admin.fetch(
                    "SELECT topic FROM identity_outbox"
                )
            }
            assert "identity.transfer.created" in outbox_topics
            assert "identity.transfer.accepted" in outbox_topics
            assert "identity.binding.transferred" in outbox_topics
            # Audit and outbox share the business event identity.
            pairs = await admin.fetch(
                """
                SELECT a.event_id, o.event_id AS outbox_event_id
                FROM identity_audit_events a
                JOIN identity_outbox o ON o.topic = 'identity.transfer.created'
                WHERE a.action = 'transfer.create'
                """
            )
            assert pairs and str(pairs[0]["event_id"]) == str(
                pairs[0]["outbox_event_id"]
            )
        finally:
            await admin.close()
    finally:
        await store.close()
        await _drop_database(database)


@pytest.mark.asyncio
async def test_persona_assignments_are_subject_scoped_persistent_and_rls_bounded() -> None:
    database = f"memoria_persona_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
        owner = (
            await service.register_person(
                display_name="主人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-persona-owner",
                now=now,
            )
        ).person_id
        child = (
            await service.register_person(
                display_name="孩子",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-persona-child",
                now=now,
            )
        ).person_id
        plain_member = (
            await service.register_person(
                display_name="成员",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-persona-member",
                now=now,
            )
        ).person_id
        binding = await service.create_binding(
            device_id=f"persona-device-{uuid.uuid4().hex[:8]}",
            declared_mode="family_shared",
            account_owner_person_id=owner,
            primary_subject_ids=(owner, child, plain_member),
            family_space_id=f"family-{uuid.uuid4().hex[:8]}",
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            persona_assignment_id="starlight:v1",
            now=now,
        )

        # Two subjects on one binding resolve to two different personas.
        child_row = await service.set_persona_assignment(
            binding_id=binding.binding_id,
            subject_id=child,
            persona_selection="taoxi",
            actor_person_id=owner,
            now=now + timedelta(minutes=1),
        )
        owner_row = await service.set_persona_assignment(
            binding_id=binding.binding_id,
            subject_id=owner,
            persona_selection="mianmian:v1",
            actor_person_id=owner,
            now=now + timedelta(minutes=1),
        )
        assert (child_row.persona_id, child_row.assignment_id) == ("taoxi", "taoxi:v1")
        assert (owner_row.persona_id, owner_row.assignment_id) == (
            "mianmian",
            "mianmian:v1",
        )
        assert child_row.persona_id != owner_row.persona_id

        # An exact replay is idempotent: the persisted row is returned unchanged.
        replay = await service.set_persona_assignment(
            binding_id=binding.binding_id,
            subject_id=child,
            persona_selection="taoxi",
            actor_person_id=owner,
            now=now + timedelta(minutes=2),
        )
        assert replay == child_row

        # Re-pointing at another persona keeps created_at and moves updated_at.
        repointed = await service.set_persona_assignment(
            binding_id=binding.binding_id,
            subject_id=child,
            persona_selection="axu",
            actor_person_id=owner,
            now=now + timedelta(minutes=3),
        )
        assert repointed.assignment_id == "axu:v1"
        assert repointed.created_at == child_row.created_at
        assert repointed.updated_at > child_row.updated_at

        listed = await service.list_persona_assignments(
            binding_id=binding.binding_id, actor_person_id=owner
        )
        assert [item.subject_id for item in listed] == sorted([owner, child])
        assert {
            item.subject_id: item.persona_id for item in listed
        } == {owner: "mianmian", child: "axu"}

        # The row survives a fresh store/pool (it is persisted, not cached).
        reopened = PostgresIdentityStore(dsns["api"], registration_dsn=dsns["registration"])
        await reopened.initialize(expected_role="memoria_identity")
        try:
            reloaded = await reopened.get_persona_assignment(
                binding_id=binding.binding_id,
                subject_id=child,
                actor_person_id=owner,
            )
            assert reloaded == repointed
        finally:
            await reopened.close()

        admin = await asyncpg.connect(dsns["admin"])
        api = await asyncpg.connect(dsns["api"])
        try:
            # The audit trigger synthesized one row per mutation; the API role
            # never writes audit rows itself.
            actions = [
                str(row["action"])
                for row in await admin.fetch(
                    """
                    SELECT action FROM identity_audit_events
                    WHERE binding_id = $1
                      AND action LIKE 'binding.persona_assignment.%'
                    ORDER BY created_at
                    """,
                    binding.binding_id,
                )
            ]
            assert actions == [
                "binding.persona_assignment.set",
                "binding.persona_assignment.set",
                "binding.persona_assignment.set",
            ]
            assert await admin.fetchval(
                "SELECT has_table_privilege('memoria_identity', "
                "'identity_audit_events', 'INSERT')"
            ) is False
            assert await admin.fetchval(
                "SELECT has_table_privilege('memoria_identity', "
                "'identity_persona_assignments', 'DELETE')"
            ) is True

            # A foreign actor is invisible to RLS even with the row id in hand.
            foreign = (
                await service.register_person(
                    display_name="外部用户",
                    timezone="Asia/Shanghai",
                    subject_category="adult",
                    age_band="adult",
                    age_evidence_status="verified",
                    age_evidence_id="evidence-persona-foreign",
                    now=now,
                )
            ).person_id
            await api.fetchval(
                "SELECT set_config('app.identity_actor', $1, false)", foreign
            )
            assert await api.fetch(
                """
                SELECT * FROM identity_persona_assignments
                WHERE binding_id = $1
                """,
                binding.binding_id,
            ) == []
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await api.execute(
                    """
                    INSERT INTO identity_persona_assignments (
                        binding_id, subject_id, assignment_id, persona_id,
                        persona_version, created_at, updated_at
                    ) VALUES ($1, $2, 'axu:v1', 'axu', 1, $3, $3)
                    """,
                    binding.binding_id,
                    foreign,
                    now,
                )

            # A binding member without binding.manage may read, never write.
            await api.fetchval(
                "SELECT set_config('app.identity_actor', $1, false)", plain_member
            )
            visible = await api.fetch(
                """
                SELECT subject_id FROM identity_persona_assignments
                WHERE binding_id = $1
                """,
                binding.binding_id,
            )
            assert {str(row["subject_id"]) for row in visible} == {owner, child}
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await api.execute(
                    """
                    UPDATE identity_persona_assignments
                    SET persona_id = 'zhiyao', assignment_id = 'zhiyao:v1'
                    WHERE binding_id = $1 AND subject_id = $2
                    """,
                    binding.binding_id,
                    child,
                )

            # The identity of an assignment row is immutable even for the
            # managing owner: only the persona snapshot may move.
            await api.fetchval(
                "SELECT set_config('app.identity_actor', $1, false)", owner
            )
            with pytest.raises(asyncpg.PostgresError, match="immutable"):
                await api.execute(
                    """
                    UPDATE identity_persona_assignments
                    SET created_at = $3
                    WHERE binding_id = $1 AND subject_id = $2
                    """,
                    binding.binding_id,
                    child,
                    now,
                )
            with pytest.raises(asyncpg.PostgresError, match="immutable"):
                await api.execute(
                    """
                    UPDATE identity_persona_assignments
                    SET subject_id = 'someone-else'
                    WHERE binding_id = $1 AND subject_id = $2
                    """,
                    binding.binding_id,
                    child,
                )
        finally:
            await admin.close()
            await api.close()

        # Dropping one override restores only that subject's binding default.
        assert (
            await service.delete_persona_assignment(
                binding_id=binding.binding_id,
                subject_id=child,
                actor_person_id=owner,
                now=now + timedelta(minutes=4),
            )
            is True
        )
        assert (
            await service.get_persona_assignment(
                binding_id=binding.binding_id,
                subject_id=child,
                actor_person_id=owner,
            )
            is None
        )
        assert (
            await service.delete_persona_assignment(
                binding_id=binding.binding_id,
                subject_id=child,
                actor_person_id=owner,
                now=now + timedelta(minutes=5),
            )
            is False
        )
        remaining = await service.list_persona_assignments(
            binding_id=binding.binding_id, actor_person_id=owner
        )
        assert [(item.subject_id, item.persona_id) for item in remaining] == [
            (owner, "mianmian")
        ]
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


@pytest.mark.asyncio
async def test_custom_persona_immutability_is_enforced_in_the_database() -> None:
    """The real PostgreSQL trigger refuses any UPDATE, not the application."""
    database = f"memoria_custom_persona_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _authority = await _service(dsns)
        now = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
        owner = (
            await service.register_person(
                display_name="主人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-custom-owner",
                now=now,
            )
        ).person_id
        record = await service.create_custom_persona(
            owner_person_id=owner,
            display_name="小岸",
            structured=StructuredPersonaFields(warmth="bright", directness="direct"),
            idempotency_key="pg-create-1",
            actor_person_id=owner,
            now=now,
        )
        assert record.persona_id.startswith("cu_")
        assert record.persona_version == 1
        assert record.fallback_designed_voice == "starlight"

        # Idempotent replay returns the same row without a second one.
        replay = await service.create_custom_persona(
            owner_person_id=owner,
            display_name="小岸",
            structured=StructuredPersonaFields(warmth="bright", directness="direct"),
            idempotency_key="pg-create-1",
            actor_person_id=owner,
            now=now + timedelta(minutes=1),
        )
        assert replay.persona_id == record.persona_id
        assert await service.count_custom_personas(
            owner_person_id=owner, actor_person_id=owner
        ) == 1

        # The limit is enforced (5 per account).
        for index in range(4):
            await service.create_custom_persona(
                owner_person_id=owner,
                display_name=f"人格{index}",
                structured=StructuredPersonaFields(),
                actor_person_id=owner,
                now=now + timedelta(seconds=index + 1),
            )
        with pytest.raises(CustomPersonaLimitError):
            await service.create_custom_persona(
                owner_person_id=owner,
                display_name="第六个",
                structured=StructuredPersonaFields(),
                actor_person_id=owner,
                now=now + timedelta(minutes=2),
            )

        # A raw UPDATE is refused by the trigger.  The table owner / superuser
        # bypasses RLS and holds no grants restriction, so this proves the
        # immutability is enforced in the DATABASE, not merely by the app.
        admin = await asyncpg.connect(dsns["admin"])
        try:
            with pytest.raises(asyncpg.PostgresError, match="immutable"):
                await admin.execute(
                    "UPDATE identity_custom_personas SET display_name = '改名' "
                    "WHERE persona_id = $1",
                    record.persona_id,
                )
            # DELETE stays allowed (the ordinary delete path).
            await admin.execute(
                "DELETE FROM identity_custom_personas WHERE persona_id = $1",
                record.persona_id,
            )
            assert (
                await admin.fetchval(
                    "SELECT count(*) FROM identity_custom_personas "
                    "WHERE persona_id = $1",
                    record.persona_id,
                )
                == 0
            )
        finally:
            await admin.close()

        # Grant boundary: SELECT/INSERT/DELETE yes, UPDATE never.
        for privilege, expected in (
            ("SELECT", True),
            ("INSERT", True),
            ("DELETE", True),
            ("UPDATE", False),
        ):
            granted = await api_privilege(
                dsns["api"], "identity_custom_personas", privilege
            )
            assert granted is expected, f"{privilege} grant mismatch"

        # The audit trigger recorded the creates.
        admin = await asyncpg.connect(dsns["admin"])
        try:
            count = await admin.fetchval(
                "SELECT count(*) FROM identity_audit_events "
                "WHERE action = 'persona.custom.create'"
            )
            assert int(count) == 5
        finally:
            await admin.close()
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


async def api_privilege(dsn: str, table: str, privilege: str) -> bool:
    connection = await asyncpg.connect(dsn)
    try:
        return bool(
            await connection.fetchval(
                "SELECT has_table_privilege(current_user, $1::regclass, $2)",
                table,
                privilege,
            )
        )
    finally:
        await connection.close()


async def test_declared_guardianship_is_one_sided_and_never_verified() -> None:
    """A guardian declaration must stay a declaration in PostgreSQL.

    The subject created by an adult device binding has no account, so nothing
    can confirm the target endpoint.  Identity must report the relationship as
    a one-sided declaration, must let the binding exist on that basis, and must
    never upgrade it to an active/verified relationship without the subject's
    own confirmation.
    """

    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)
        guardian = (
            await service.register_person(
                display_name="家长",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-guardian-declaration",
                now=now,
            )
        ).person_id
        ward = (
            await service.register_person(
                display_name="孩子",
                timezone="Asia/Shanghai",
                subject_category="minor",
                age_band="under_14",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id

        proposed = await service.propose_relationship(
            source_person_id=guardian,
            target_person_id=ward,
            relation_type="guardian_of",
            established_evidence_id="guardian_declaration_v1:device_binding",
            actor_person_id=guardian,
            now=now,
        )
        await service.confirm_relationship(
            relationship_id=proposed.relationship_id,
            person_id=guardian,
            now=now,
        )

        assert await store.has_active_relationship(
            source_person_id=guardian,
            target_person_id=ward,
            relation_type="guardian_of",
            at=now,
        ) is False
        assert await store.has_source_confirmed_relationship(
            source_person_id=guardian,
            target_person_id=ward,
            relation_type="guardian_of",
            at=now,
        ) is True
        # The reverse direction is not a declaration of the ward.
        assert await store.has_source_confirmed_relationship(
            source_person_id=ward,
            target_person_id=guardian,
            relation_type="guardian_of",
            at=now,
        ) is False

        binding = await service.create_binding(
            device_id="dev-pg-declared-guardian",
            declared_mode="parent_for_child",
            account_owner_person_id=guardian,
            primary_subject_ids=(ward,),
            roles=((guardian, "guardian"), (guardian, "device_admin")),
            service_profile_version="parent_for_child-v1",
            policy_bundle_version="multi-subject-v1",
            now=now,
        )
        assert binding.primary_subject_ids == (ward,)
        assert await service.declared_guardians(subject_person_id=ward) == (guardian,)

        # Once the subject really confirms, the declaration stops being the
        # recorded basis and the relationship becomes verified.
        await service.confirm_relationship(
            relationship_id=proposed.relationship_id,
            person_id=ward,
            now=now + timedelta(minutes=1),
        )
        assert await store.has_active_relationship(
            source_person_id=guardian,
            target_person_id=ward,
            relation_type="guardian_of",
            at=now + timedelta(minutes=2),
        ) is True
        assert await store.has_source_confirmed_relationship(
            source_person_id=guardian,
            target_person_id=ward,
            relation_type="guardian_of",
            at=now + timedelta(minutes=2),
        ) is False
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


async def test_parent_for_child_still_requires_a_relationship_for_the_guardian_role() -> None:
    """The declaration path must not weaken the role check itself."""

    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 9, 16, 5, 0, tzinfo=UTC)
        owner = (
            await service.register_person(
                display_name="家长",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-owner",
                now=now,
            )
        ).person_id
        ward = (
            await service.register_person(
                display_name="孩子",
                timezone="Asia/Shanghai",
                subject_category="minor",
                age_band="under_14",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id

        with pytest.raises(ModeConstraintError, match="active confirmed"):
            await service.create_binding(
                device_id="dev-pg-undeclared-guardian",
                declared_mode="parent_for_child",
                account_owner_person_id=owner,
                primary_subject_ids=(ward,),
                roles=((owner, "guardian"), (owner, "device_admin")),
                service_profile_version="parent_for_child-v1",
                policy_bundle_version="multi-subject-v1",
                now=now,
            )
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


async def _bound_child_for_deletion(
    service: IdentityService, *, now: datetime
) -> tuple[str, str, str]:
    """One verified adult owner, one attested child they serve."""

    owner = (
        await service.register_person(
            display_name="家长",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="evidence-owner-delete",
            now=now,
        )
    ).person_id
    child = await service.register_person(
        display_name="小满",
        timezone="Asia/Shanghai",
        subject_category="minor",
        age_band="under_14",
        actor_person_id=owner,
        now=now,
    )
    await service.attest_binding_relationship(
        source_person_id=owner,
        target_person_id=child.person_id,
        relation_type="guardian_of",
        actor_person_id=owner,
        now=now,
    )
    manifest = await service.create_binding(
        device_id="dev-pg-delete",
        declared_mode="parent_for_child",
        account_owner_person_id=owner,
        primary_subject_ids=(child.person_id,),
        roles=((owner, "guardian"), (owner, "device_admin")),
        service_profile_version="parent_for_child-v1",
        policy_bundle_version="multi-subject-v1",
        consent_offer_ids=("offer_minor_voice_session_v1",),
        now=now,
    )
    assert manifest.primary_subject_ids == (child.person_id,)
    return owner, child.person_id, manifest.binding_id


async def test_unbind_without_a_switch_clears_the_child_and_keeps_id_only_audit() -> None:
    """Acceptance (#283): no nickname or age band survives an unbind."""

    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
        owner, child, _binding_id = await _bound_child_for_deletion(service, now=now)

        await service.revoke_binding(
            device_id="dev-pg-delete", actor_person_id=owner, now=now + timedelta(minutes=1)
        )
        cleared = await service.redact_bound_subject(
            person_id=child, actor_person_id=owner, now=now + timedelta(minutes=1)
        )
        assert cleared.display_name == REDACTED_DISPLAY_NAME
        assert (cleared.subject_category, cleared.age_band) == ("unknown", "unknown")
        assert cleared.age_evidence_status == "unverified"
        assert cleared.status == "disabled"

        admin = await asyncpg.connect(dsns["admin"])
        try:
            row = await admin.fetchrow(
                "SELECT display_name, subject_category, age_band, status"
                " FROM identity_persons WHERE person_id = $1",
                child,
            )
            assert dict(row) == {
                "display_name": REDACTED_DISPLAY_NAME,
                "subject_category": "unknown",
                "age_band": "unknown",
                "status": "disabled",
            }
            # The binding audit rows survive with ids and times only: no name
            # and no age band anywhere in them.
            rows = await admin.fetch(
                "SELECT payload_json FROM identity_audit_events"
                " WHERE binding_id IS NOT NULL AND device_id = $1",
                "dev-pg-delete",
            )
            assert rows, "the binding audit trail is kept"
            for audit in rows:
                payload = audit["payload_json"]
                serialized = payload if isinstance(payload, str) else json.dumps(payload)
                assert "小满" not in serialized
                assert "under_14" not in serialized
                assert REDACTED_DISPLAY_NAME not in serialized
            assert await admin.fetchval(
                "SELECT count(*) FROM identity_device_bindings WHERE device_id = $1",
                "dev-pg-delete",
            ) >= 1
        finally:
            await admin.close()
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


async def test_person_update_audit_keeps_only_changed_fields_actor_and_time() -> None:
    """Acceptance (#283): a new update audit holds no whole-row snapshot."""

    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 10, 21, 4, 0, tzinfo=UTC)
        owner, child, _binding_id = await _bound_child_for_deletion(service, now=now)
        before = await service.get_person(child, actor_person_id=owner)

        updated = await service.update_person_profile(
            person_id=owner,
            locale="en-US",
            actor_person_id=owner,
            now=now + timedelta(minutes=1),
        )
        assert updated.locale == "en-US"
        later = await service.get_person(child, actor_person_id=owner)
        assert later is not None
        del before

        admin = await asyncpg.connect(dsns["admin"])
        try:
            row = await admin.fetchrow(
                "SELECT payload_json FROM identity_audit_events"
                " WHERE action = 'person.self_update' AND person_id = $1"
                " ORDER BY created_at DESC LIMIT 1",
                owner,
            )
            payload = row["payload_json"]
            payload = json.loads(payload) if isinstance(payload, str) else dict(payload)
            assert payload["changed_fields"] == ["locale"]
            assert payload["actor_person_id"] == owner
            assert set(payload) == {"changed_fields", "actor_person_id", "occurred_at"}
            # No snapshot field leaks the name, the age band or any column.
            for column in (
                "display_name",
                "age_band",
                "subject_category",
                "age_evidence_status",
                "timezone",
                "status",
                "created_at",
                "updated_at",
                "person_id",
                "locale",
            ):
                assert column not in payload, column
        finally:
            await admin.close()

        # An age declaration reports the fields it changed, never the values.
        await service.declare_age_evidence(
            person_id=child, age_band="14_17", actor_person_id=owner, now=now + timedelta(minutes=2)
        )
        admin = await asyncpg.connect(dsns["admin"])
        try:
            row = await admin.fetchrow(
                "SELECT payload_json FROM identity_audit_events"
                " WHERE action = 'person.age_evidence.update' AND person_id = $1"
                " ORDER BY created_at DESC LIMIT 1",
                child,
            )
            payload = row["payload_json"]
            payload = json.loads(payload) if isinstance(payload, str) else dict(payload)
            # Only the band moved: category and evidence status did not.
            assert payload["changed_fields"] == ["age_band"]
            assert payload["actor_person_id"] == owner
            assert "14_17" not in json.dumps(payload)
        finally:
            await admin.close()
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


async def test_account_deletion_erases_the_person_rows_and_refuses_without_the_gate() -> None:
    """Acceptance (#283): no usage-person row survives an account deletion."""

    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)
        owner, child, _binding_id = await _bound_child_for_deletion(service, now=now)

        api = await asyncpg.connect(dsns["api"])
        try:
            # The API role holds no DELETE grant at all, and opening the gate
            # by hand does not give it one: the GUC is not a credential.
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with api.transaction():
                    await api.execute(
                        "SELECT set_config('app.identity_actor', $1, true)", owner
                    )
                    await api.execute(
                        "SELECT set_config('app.identity_account_deletion', '1', true)"
                    )
                    await api.execute(
                        "DELETE FROM identity_persons WHERE person_id = $1", child
                    )
            # Nor can it borrow the port for another account.
            with pytest.raises(asyncpg.RaiseError, match="actor context mismatch"):
                async with api.transaction():
                    await api.execute(
                        "SELECT set_config('app.identity_actor', 'someone-else', true)"
                    )
                    await api.execute(
                        "SELECT identity_delete_account_identity($1, $2)",
                        owner,
                        REDACTED_DISPLAY_NAME,
                    )
        finally:
            await api.close()

        counts = await service.delete_account_identity(account_person_id=owner)
        assert counts["persons"] >= 2

        admin = await asyncpg.connect(dsns["admin"])
        try:
            assert (
                await admin.fetchval(
                    "SELECT count(*) FROM identity_persons WHERE person_id = ANY($1::text[])",
                    [owner, child],
                )
                == 0
            )
            assert (
                await admin.fetchval(
                    "SELECT count(*) FROM identity_device_binding_roles"
                    " WHERE person_id = ANY($1::text[])",
                    [owner, child],
                )
                == 0
            )
            # Residue check used by the deletion pipeline's verified_empty.
            assert await service.remaining_account_identity(account_person_id=owner) == {}
            # Replay is idempotent: a retry after a crash converges.
            again = await service.delete_account_identity(account_person_id=owner)
            assert again["persons"] == 0
        finally:
            await admin.close()
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


async def test_account_deletion_keeps_a_child_another_account_still_serves() -> None:
    """Only the deleting account's own graph is erased (#283)."""

    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
        first_owner, child, _binding_id = await _bound_child_for_deletion(service, now=now)
        second_owner = (
            await service.register_person(
                display_name="另一位家长",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-second-parent",
                now=now,
            )
        ).person_id
        await service.attest_binding_relationship(
            source_person_id=second_owner,
            target_person_id=child,
            relation_type="guardian_of",
            actor_person_id=second_owner,
            now=now,
        )
        await service.create_binding(
            device_id="dev-pg-delete-shared",
            declared_mode="parent_for_child",
            account_owner_person_id=second_owner,
            primary_subject_ids=(child,),
            roles=((second_owner, "guardian"), (second_owner, "device_admin")),
            service_profile_version="parent_for_child-v1",
            policy_bundle_version="multi-subject-v1",
            consent_offer_ids=("offer_minor_voice_session_v1",),
            now=now,
        )

        counts = await service.delete_account_identity(account_person_id=first_owner)

        # The first account is gone; the shared child still has a binding.
        assert counts["persons"] == 1
        remaining = await service.get_person(child, actor_person_id=second_owner)
        assert remaining.display_name == "小满"
        assert remaining.status == "active"
        assert (
            await service.get_person(second_owner, actor_person_id=second_owner)
        ).status == "active"
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)
