"""MemoryScope's production Session + Identity authority snapshot."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from services.control_api.app.memory_scope_authority import (
    MemoryAuthorityDenied,
    MemoryAuthorityUnavailable,
    PostgresMemoryAuthority,
)
from services.control_api.app.multi_subject_runtime import (
    PostgresMultiSubjectRuntimeControl,
)
from services.identity.domain import (
    BindingManifest,
    IdentityAccessDeniedError,
    ManifestRole,
)
from services.session_runtime.postgres_store import SessionRuntimeContext
from services.session_runtime.service import PersistentSessionNotFound

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class _Sessions:
    def __init__(
        self,
        result: tuple[object, SessionRuntimeContext] | None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, str, datetime]] = []

    async def current(
        self,
        *,
        actor_id: str,
        session_id: str,
        now: datetime,
    ) -> tuple[object, SessionRuntimeContext]:
        self.calls.append((actor_id, session_id, now))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class _Runtime:
    def __init__(
        self,
        *,
        profile: object,
        context: SessionRuntimeContext,
        manifest: BindingManifest,
        manifest_error: Exception | None = None,
    ) -> None:
        self.sessions = _Sessions((profile, context))
        self.manifest = manifest
        self.manifest_error = manifest_error
        self.manifest_calls: list[tuple[str, str, datetime]] = []

    async def require_binding_member(
        self,
        *,
        device_id: str,
        user_id: str,
        now: datetime,
    ) -> BindingManifest:
        self.manifest_calls.append((device_id, user_id, now))
        if self.manifest_error is not None:
            raise self.manifest_error
        return self.manifest


def _profile(**overrides: object) -> object:
    values: dict[str, object] = {
        "runtime_profile_id": "profile-1",
        "device_id": "device-1",
        "session_id": "voice-session-1",
        "actor_id": "guardian-1",
        "binding_id": "binding-1",
        "binding_version": 3,
        "active_subject_id": "child-1",
        "subject_revision": 7,
        "subject_category": "minor",
        "age_band": "under_14",
        "speaker_state": "confirmed",
        "speaker_confidence": 0.97,
        "service_mode": "child_learning",
        "policy_bundle_version": "policy-minor-v5",
        "policy_receipt_ids": ("receipt-1", "receipt-2"),
        "session_epoch": 4,
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(**overrides: object) -> SessionRuntimeContext:
    values: dict[str, object] = {
        "session_id": "voice-session-1",
        "actor_id": "guardian-1",
        "device_id": "device-1",
        "binding_id": "binding-1",
        "binding_version": 3,
        "active_subject_id": "child-1",
        "subject_revision": 7,
        "session_epoch": 4,
        "profile_revision": 11,
        "current_runtime_profile_id": "profile-1",
        "generation_id": 12,
        "turn_id": 13,
        "tool_epoch": 14,
        "created_at": NOW - timedelta(minutes=1),
        "updated_at": NOW,
    }
    values.update(overrides)
    return SessionRuntimeContext(**values)  # type: ignore[arg-type]


def _manifest(
    *,
    roles: tuple[ManifestRole, ...] | None = None,
    **overrides: object,
) -> BindingManifest:
    values: dict[str, object] = {
        "binding_id": "binding-1",
        "device_id": "device-1",
        "declared_mode": "parent_for_child",
        "binding_version": 3,
        "status": "active",
        "reason": "create",
        "supersedes_binding_id": None,
        "family_space_id": "family-1",
        "account_owner_id": "guardian-1",
        "device_admin_ids": ("guardian-1",),
        "primary_subject_ids": ("child-1",),
        "guardian_ids": ("guardian-1",),
        "delegate_ids": (),
        "emergency_contact_ids": (),
        "member_ids": (),
        "roles": roles
        or (
            ManifestRole(
                person_id="guardian-1",
                role="account_owner",
                permissions=("binding.manage",),
            ),
            ManifestRole(
                person_id="guardian-1",
                role="guardian",
                permissions=("memory.guardian.summary",),
            ),
            ManifestRole(
                person_id="child-1",
                role="primary_subject",
                permissions=("memory.private.read",),
            ),
        ),
        "service_profile_version": "student-cn-v3",
        "policy_bundle_version": "policy-minor-v5",
        "consent_snapshot_id": "consent-1",
        "persona_assignment_id": "persona-assignment-1",
        "valid_from": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=1),
        "created_at": NOW - timedelta(days=1),
    }
    values.update(overrides)
    return BindingManifest(**values)  # type: ignore[arg-type]


def _authority(runtime: _Runtime) -> PostgresMemoryAuthority:
    return PostgresMemoryAuthority(cast(PostgresMultiSubjectRuntimeControl, runtime))


@pytest.mark.asyncio
async def test_current_reads_session_once_and_derives_the_complete_snapshot() -> None:
    profile = _profile()
    context = _context()
    runtime = _Runtime(
        profile=profile,
        context=context,
        manifest=_manifest(),
    )

    snapshot = await _authority(runtime).current(
        actor_id="guardian-1",
        session_id="voice-session-1",
        now=NOW,
    )

    assert runtime.sessions.calls == [("guardian-1", "voice-session-1", NOW)]
    assert runtime.manifest_calls == [("device-1", "guardian-1", NOW)]
    assert snapshot is not None
    assert snapshot.active_subject_id == "child-1"
    assert snapshot.actor_binding_role == "guardian"
    assert snapshot.actor_binding_roles == ("guardian", "account_owner")
    assert snapshot.family_space_id == "family-1"
    assert snapshot.consent_snapshot_id == "consent-1"
    assert snapshot.profile_revision == 11
    assert snapshot.generation_id == 12
    assert snapshot.turn_id == 13
    assert snapshot.tool_epoch == 14
    assert snapshot.policy_receipt_ids == ("receipt-1", "receipt-2")
    assert snapshot.service_mode == "child_learning"
    assert snapshot.fence.session_id == "voice-session-1"
    assert snapshot.fence.epoch == 4
    assert snapshot.fence.binding_version == 3
    assert snapshot.fence.subject_revision == 7
    assert snapshot.fence.generation_id == "12"
    assert snapshot.fence.turn_id == 13
    assert snapshot.fence.valid_until == profile.expires_at


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("actor_id", "other-actor"),
        ("device_id", "other-device"),
        ("binding_id", "other-binding"),
        ("binding_version", 4),
        ("active_subject_id", "other-subject"),
        ("subject_revision", 8),
        ("session_epoch", 5),
        ("current_runtime_profile_id", "other-profile"),
    ],
)
async def test_current_rejects_profile_context_mismatch(
    field_name: str,
    bad_value: object,
) -> None:
    runtime = _Runtime(
        profile=_profile(),
        context=_context(**{field_name: bad_value}),
        manifest=_manifest(),
    )

    with pytest.raises(MemoryAuthorityUnavailable, match="inconsistent"):
        await _authority(runtime).current(
            actor_id="guardian-1",
            session_id="voice-session-1",
            now=NOW,
        )

    assert len(runtime.sessions.calls) == 1
    assert runtime.manifest_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("device_id", "other-device"),
        ("binding_id", "other-binding"),
        ("binding_version", 4),
        ("policy_bundle_version", "other-policy"),
    ],
)
async def test_current_rejects_manifest_profile_mismatch(
    field_name: str,
    bad_value: object,
) -> None:
    manifest = replace(_manifest(), **{field_name: bad_value})
    runtime = _Runtime(
        profile=_profile(),
        context=_context(),
        manifest=manifest,
    )

    with pytest.raises(MemoryAuthorityUnavailable, match="does not match"):
        await _authority(runtime).current(
            actor_id="guardian-1",
            session_id="voice-session-1",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_current_maps_non_member_actor_to_denied() -> None:
    runtime = _Runtime(
        profile=_profile(),
        context=_context(),
        manifest=_manifest(),
        manifest_error=IdentityAccessDeniedError("not a member"),
    )

    with pytest.raises(MemoryAuthorityDenied, match="not a member"):
        await _authority(runtime).current(
            actor_id="guardian-1",
            session_id="voice-session-1",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_current_rejects_guest_active_subject() -> None:
    runtime = _Runtime(
        profile=_profile(active_subject_id="guest-1"),
        context=_context(active_subject_id="guest-1"),
        manifest=_manifest(),
    )

    with pytest.raises(MemoryAuthorityDenied, match="guest"):
        await _authority(runtime).current(
            actor_id="guardian-1",
            session_id="voice-session-1",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_current_returns_none_for_no_rls_visible_voice_session() -> None:
    runtime = _Runtime(
        profile=_profile(),
        context=_context(),
        manifest=_manifest(),
    )
    runtime.sessions = _Sessions(
        None,
        error=PersistentSessionNotFound("voice-session-1"),
    )

    snapshot = await _authority(runtime).current(
        actor_id="guardian-1",
        session_id="voice-session-1",
        now=NOW,
    )

    assert snapshot is None
    assert runtime.manifest_calls == []
