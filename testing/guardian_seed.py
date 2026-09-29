"""Identity rows the PostgreSQL guardian store checks but cannot write itself."""

from __future__ import annotations

from datetime import datetime

import asyncpg


async def declare_binding_guardian(
    connection: asyncpg.Connection, *, guardian_id: str, subject_id: str, at: datetime
) -> None:
    """The Identity rows that make ``guardian_id`` the subject's declared guardian.

    PostgreSQL re-validates a declared crisis recipient (P0-04): a pending
    ``guardian_of`` declaration by the owner of an active binding that names
    the subject as primary subject.
    """

    for person_id, category, band, evidence in (
        (guardian_id, "adult", "adult", "verified"),
        (subject_id, "minor", "under_14", "unverified"),
    ):
        await connection.execute(
            """
            INSERT INTO identity_persons(
                person_id, display_name, subject_category, age_band,
                age_evidence_status, locale, timezone, status,
                created_at, updated_at
            ) VALUES ($1,$1,$2,$3,$5,'zh-CN','Asia/Shanghai','active',$4,$4)
            ON CONFLICT (person_id) DO NOTHING
            """,
            person_id,
            category,
            band,
            at,
            evidence,
        )
    await connection.execute(
        """
        INSERT INTO identity_relationships(
            relationship_id, source_person_id, target_person_id,
            relation_type, status, valid_from, established_evidence_id,
            confirmed_by_source_at, confirmed_by_target_at,
            requires_confirmation, can_delegate, delegation_depth,
            permissions_json, auto_suspended, created_at, updated_at
        ) VALUES (
            'declaration-' || $2, $1, $2, 'guardian_of', 'pending', $3,
            'guardian_declaration_v1:device_binding',
            $3, NULL, TRUE, FALSE, 0, '[]'::jsonb, FALSE, $3, $3
        )
        """,
        guardian_id,
        subject_id,
        at,
    )
    await connection.execute(
        """
        INSERT INTO identity_device_bindings(
            binding_id, device_id, declared_mode, family_space_id,
            account_owner_person_id, binding_version, status, reason,
            valid_from, valid_until, supersedes_binding_id,
            service_profile_version, policy_bundle_version,
            consent_snapshot_id, persona_assignment_id, created_at
        ) VALUES (
            'binding-' || $2, 'device-' || $2, 'parent_for_child', NULL, $1, 1,
            'active', 'create', $3, NULL, NULL, 'parent_for_child-v1',
            'multi-subject-v1', NULL, 'starlight:v1', $3
        )
        """,
        guardian_id,
        subject_id,
        at,
    )
    await connection.execute(
        """
        INSERT INTO identity_device_binding_roles(
            binding_id, person_id, role, status, permissions_json,
            granted_at, ended_at
        ) VALUES
            ('binding-' || $2, $1, 'guardian', 'active', '[]'::jsonb, $3, NULL),
            ('binding-' || $2, $2, 'primary_subject', 'active', '[]'::jsonb, $3, NULL)
        """,
        guardian_id,
        subject_id,
        at,
    )
