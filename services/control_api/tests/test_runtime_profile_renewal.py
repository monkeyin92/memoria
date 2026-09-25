"""An expired control Runtime Profile renews in place on the same Session.

The PostgreSQL Session authority never reuses a session_id and denies a stored
profile once its TTL (5 minutes) lapses.  Before renewal existed, the fixed
app control Session answered 403 ``runtime_profile_rejected`` forever after
its first five minutes.  These tests drive the real authority: renewal must
reissue a signed profile on the same Session with a strictly higher epoch,
admit exactly one winner under concurrency, and still fail closed for a
superseded binding, a revoked device, a closed Session and an explicitly
named non-control Session.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import cast

import asyncpg
import pytest
from fastapi import HTTPException
from packages.contracts.generated.python.multi_subject_contracts import (
    RuntimeProfileSignedV2,
)
from services.control_api.app.multi_subject_runtime import (
    PostgresMultiSubjectRuntimeControl,
)
from services.control_api.app.routes.multi_subject import _session_denied
from services.identity.domain import BindingManifest, ManifestRole
from services.session_runtime.service import (
    PersistentSessionDenied,
    PersistentSessionExpired,
    PostgresSessionRuntimeService,
    StartPersistentSessionCommand,
    build_postgres_session_runtime_service,
)
from services.session_runtime.tests.test_postgres_store import (
    _SIGNING_KEY,
    postgres_runtime,  # noqa: F401 - pytest discovers imported fixtures by name
)

_OWNER = "account-renewal-owner"
_DEVICE = "dev_renewal"
_BINDING = "binding-renewal-v1"
_REBOUND = "binding-renewal-v2"
_FAMILY = "family-renewal"
_TTL = timedelta(minutes=5)

requires_postgres = pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the real Session authority",
)


def _manifest(binding_id: str, version: int, now: datetime) -> BindingManifest:
    return BindingManifest(
        binding_id=binding_id,
        device_id=_DEVICE,
        declared_mode="self_use",
        binding_version=version,
        status="active",
        reason="create" if version == 1 else "supersede",
        supersedes_binding_id=None if version == 1 else _BINDING,
        family_space_id=_FAMILY,
        account_owner_id=_OWNER,
        device_admin_ids=(_OWNER,),
        primary_subject_ids=(_OWNER,),
        guardian_ids=(),
        delegate_ids=(),
        emergency_contact_ids=(),
        member_ids=(),
        roles=(
            ManifestRole(_OWNER, "account_owner", ("binding.manage",)),
            ManifestRole(_OWNER, "device_admin", ("binding.manage",)),
            ManifestRole(_OWNER, "primary_subject", ("content.read",)),
        ),
        service_profile_version="self_use-v1",
        policy_bundle_version="multi-subject-v1",
        consent_snapshot_id=None,
        persona_assignment_id="starlight:v1",
        valid_from=now,
        valid_until=None,
        created_at=now,
    )


class _IdentityView:
    """Identity as production exposes it: FORCE RLS, no actor means no rows."""

    def __init__(self, manifest: BindingManifest) -> None:
        self.manifest = manifest

    async def get_active_manifest(
        self,
        device_id: str,
        _now: datetime | None = None,
        actor_person_id: str | None = None,
    ) -> BindingManifest | None:
        if actor_person_id != _OWNER or device_id != _DEVICE:
            return None
        return self.manifest

    async def list_persona_assignments(
        self,
        *,
        binding_id: str,
        actor_person_id: str | None = None,
    ) -> tuple[object, ...]:
        return ()


async def _insert_binding(
    connection: asyncpg.Connection,
    *,
    binding_id: str,
    version: int,
    now: datetime,
) -> None:
    await connection.execute(
        """
        INSERT INTO identity_device_bindings (
            binding_id, device_id, declared_mode, family_space_id,
            account_owner_person_id, binding_version, status, reason,
            valid_from, valid_until, supersedes_binding_id,
            service_profile_version, policy_bundle_version,
            consent_snapshot_id, persona_assignment_id, created_at
        ) VALUES ($1, $2, 'self_use', $3, $4, $5, 'active', $6,
                  $7, NULL, $8, 'self_use-v1', 'multi-subject-v1', NULL,
                  'starlight:v1', $7)
        """,
        binding_id,
        _DEVICE,
        _FAMILY,
        _OWNER,
        version,
        "create" if version == 1 else "supersede",
        now,
        None if version == 1 else _BINDING,
    )
    await connection.executemany(
        """
        INSERT INTO identity_device_binding_roles (
            binding_id, person_id, role, status, permissions_json,
            granted_at, ended_at
        ) VALUES ($1, $2, $3, 'active', $4::jsonb, $5, NULL)
        """,
        [
            (binding_id, _OWNER, "account_owner", '["binding.manage"]', now),
            (binding_id, _OWNER, "device_admin", '["binding.manage"]', now),
            (binding_id, _OWNER, "primary_subject", '["content.read"]', now),
        ],
    )


async def _seed(bootstrap_dsn: str, now: datetime) -> None:
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            """
            INSERT INTO identity_persons (
                person_id, display_name, subject_category, age_band,
                age_evidence_status, locale, timezone, status,
                created_at, updated_at
            ) VALUES ($1, $1, 'adult', 'adult', 'verified', 'zh-CN',
                      'Asia/Shanghai', 'active', $2, $2)
            """,
            _OWNER,
            now,
        )
        await _insert_binding(admin, binding_id=_BINDING, version=1, now=now)
        await admin.execute(
            """
            INSERT INTO device_fleet_devices (
                device_id, family_space_id, binding_id, binding_version,
                lifecycle_status, capability_manifest_hash, capabilities,
                firmware_version, firmware_security_version, firmware_sha256,
                bootloader_version, anti_rollback_floor_version,
                anti_rollback_floor_security_version, created_at, updated_at
            ) VALUES ($1, $2, $3, 1, 'bound', $4, '[]'::jsonb,
                      'test-fw', 1, $4, 'test-boot', 'test-fw', 1, $5, $5)
            """,
            _DEVICE,
            _FAMILY,
            _BINDING,
            hashlib.sha256(f"manifest:{_DEVICE}".encode()).hexdigest(),
            now,
        )
    finally:
        await admin.close()


async def _admin(bootstrap_dsn: str, sql: str, *args: object) -> None:
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(sql, *args)
    finally:
        await admin.close()


async def _active_profile_count(bootstrap_dsn: str, session_id: str) -> int:
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        count = await admin.fetchval(
            "SELECT count(*) FROM session_runtime_contexts "
            "WHERE session_id = $1 AND state = 'active'",
            session_id,
        )
    finally:
        await admin.close()
    return int(count)


async def _control(
    runtime: tuple[object, str],
    now: datetime,
) -> tuple[
    PostgresMultiSubjectRuntimeControl,
    PostgresSessionRuntimeService,
    _IdentityView,
    str,
]:
    store, bootstrap_dsn = runtime
    await _seed(bootstrap_dsn, now)
    identity = _IdentityView(_manifest(_BINDING, 1, now))
    sessions = build_postgres_session_runtime_service(
        store=store,  # type: ignore[arg-type]
        signing_key=_SIGNING_KEY,
    )
    control = PostgresMultiSubjectRuntimeControl(
        identity=identity,  # type: ignore[arg-type]
        sessions=sessions,
    )
    return control, sessions, identity, bootstrap_dsn


async def _read(
    control: PostgresMultiSubjectRuntimeControl,
    now: datetime,
    *,
    session_id: str | None = None,
) -> RuntimeProfileSignedV2:
    return await control.ensure_profile(
        device_id=_DEVICE,
        session_id=session_id,
        actor_id=_OWNER,
        now=now,
    )


def test_control_session_is_keyed_by_binding_and_actor() -> None:
    first = PostgresMultiSubjectRuntimeControl.control_session_id(
        _DEVICE, binding_id=_BINDING, actor_id=_OWNER
    )
    assert first.startswith(f"device-control:{_DEVICE}:{_BINDING}:")
    assert PostgresMultiSubjectRuntimeControl.is_control_session(first)
    assert first != PostgresMultiSubjectRuntimeControl.control_session_id(
        _DEVICE, binding_id=_REBOUND, actor_id=_OWNER
    )
    assert first != PostgresMultiSubjectRuntimeControl.control_session_id(
        _DEVICE, binding_id=_BINDING, actor_id="another-account"
    )
    long_id = PostgresMultiSubjectRuntimeControl.control_session_id(
        "d" * 128, binding_id=_BINDING, actor_id=_OWNER
    )
    assert len(long_id) <= 128
    assert PostgresMultiSubjectRuntimeControl.is_control_session(long_id)


def test_session_denial_reason_is_a_fixed_code_and_never_raw_sql() -> None:
    expired = _session_denied(
        PersistentSessionExpired("runtime profile is expired"),
        code="runtime_profile_rejected",
        route="device_runtime_profile",
        actor_id=_OWNER,
        device_id=_DEVICE,
    )
    assert isinstance(expired, HTTPException)
    assert expired.status_code == 403
    assert cast(dict[str, str], expired.detail) == {
        "code": "runtime_profile_rejected",
        "reason": "profile_expired",
    }
    closed = _session_denied(
        PersistentSessionDenied("session is closed"),
        code="runtime_profile_rejected",
        route="device_runtime_profile",
        actor_id=_OWNER,
    )
    assert cast(dict[str, str], closed.detail)["reason"] == "session_closed"
    trust = _session_denied(
        PersistentSessionDenied("device_lifecycle_not_bound"),
        code="runtime_profile_rejected",
        route="device_runtime_profile",
        actor_id=_OWNER,
    )
    assert cast(dict[str, str], trust.detail)["reason"] == "device_lifecycle_not_bound"
    raw = _session_denied(
        PersistentSessionDenied('relation "identity_x" does not exist'),
        code="runtime_profile_rejected",
        route="device_runtime_profile",
        actor_id=_OWNER,
    )
    assert cast(dict[str, str], raw.detail)["reason"] == "denied"


@requires_postgres
@pytest.mark.asyncio
async def test_expired_control_profile_renews_on_the_same_session(
    postgres_runtime: tuple[object, str],  # noqa: F811
) -> None:
    now = datetime.now(UTC)
    control, _sessions, _identity, bootstrap_dsn = await _control(postgres_runtime, now)

    first = await _read(control, now)
    assert first.session_epoch == 1
    assert first.expires_at - first.issued_at == _TTL

    # Inside the TTL the stored profile is reused, not reissued.
    same = await _read(control, now + timedelta(minutes=4))
    assert same == first

    later = now + timedelta(minutes=6)
    renewed = await _read(control, later)
    assert renewed.session_id == first.session_id
    assert renewed.runtime_profile_id != first.runtime_profile_id
    assert renewed.session_epoch == first.session_epoch + 1
    assert renewed.issued_at == later
    assert renewed.expires_at == later + _TTL
    assert renewed.binding_id == first.binding_id
    assert renewed.binding_version == first.binding_version
    # The expired profile's subject is not carried forward; the renewal
    # confirms the one-to-one binding's sole subject again from the binding.
    assert first.active_subject_id == _OWNER
    assert renewed.active_subject_id == _OWNER
    assert renewed.speaker_state.value == "confirmed"

    # The renewed profile is served until it expires in turn.
    assert await _read(control, later + timedelta(minutes=2)) == renewed
    assert await _active_profile_count(bootstrap_dsn, first.session_id) == 1

    # A confirmation on an expired control Session renews, then switches.
    confirmed_at = later + timedelta(minutes=7)
    confirmed = await control.switch_subject(
        session_id=first.session_id,
        subject_id=_OWNER,
        actor_id=_OWNER,
        now=confirmed_at,
    )
    assert confirmed.active_subject_id == _OWNER
    assert confirmed.speaker_state.value == "confirmed"
    assert confirmed.session_epoch == renewed.session_epoch + 2


@requires_postgres
@pytest.mark.asyncio
async def test_concurrent_renewals_commit_one_profile(
    postgres_runtime: tuple[object, str],  # noqa: F811
) -> None:
    now = datetime.now(UTC)
    control, _sessions, _identity, bootstrap_dsn = await _control(postgres_runtime, now)
    first = await _read(control, now)
    later = now + timedelta(minutes=6)

    results = await asyncio.gather(*(_read(control, later) for _ in range(4)))

    assert {item.runtime_profile_id for item in results} == {results[0].runtime_profile_id}
    assert {item.session_epoch for item in results} == {first.session_epoch + 1}
    assert await _active_profile_count(bootstrap_dsn, first.session_id) == 1
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        epoch, revision = await admin.fetchrow(
            "SELECT session_epoch, profile_revision FROM session_runtime_contexts "
            "WHERE session_id = $1",
            first.session_id,
        )
    finally:
        await admin.close()
    assert (epoch, revision) == (2, 2)


@requires_postgres
@pytest.mark.asyncio
async def test_rebind_moves_the_control_session_and_old_session_stays_fenced(
    postgres_runtime: tuple[object, str],  # noqa: F811
) -> None:
    # The action-context guard locks the binding at the database clock, so
    # the new binding must already be valid in real time.
    now = datetime.now(UTC) - timedelta(minutes=2)
    control, _sessions, identity, bootstrap_dsn = await _control(postgres_runtime, now)
    first = await _read(control, now)

    rebound_at = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "UPDATE identity_device_bindings SET status = 'superseded', "
            "valid_until = $2 WHERE binding_id = $1",
            _BINDING,
            rebound_at,
        )
        await _insert_binding(admin, binding_id=_REBOUND, version=2, now=rebound_at)
        await admin.execute(
            "UPDATE device_fleet_devices SET binding_id = $2, binding_version = 2 "
            "WHERE device_id = $1",
            _DEVICE,
            _REBOUND,
        )
    finally:
        await admin.close()
    identity.manifest = _manifest(_REBOUND, 2, rebound_at)

    # The default read follows the new binding onto a fresh control Session.
    moved = await _read(control, rebound_at + timedelta(seconds=1))
    assert moved.session_id != first.session_id
    assert moved.binding_id == _REBOUND
    assert moved.binding_version == 2
    assert moved.session_epoch == 1

    # The superseded binding's Session is never renewed onto anything.
    with pytest.raises(PersistentSessionDenied) as within_ttl:
        await _read(control, rebound_at + timedelta(seconds=2), session_id=first.session_id)
    assert not isinstance(within_ttl.value, PersistentSessionExpired)
    with pytest.raises(PersistentSessionDenied, match="no longer active"):
        await _read(control, now + timedelta(minutes=7), session_id=first.session_id)


@requires_postgres
@pytest.mark.asyncio
async def test_revoked_device_is_not_renewed(
    postgres_runtime: tuple[object, str],  # noqa: F811
) -> None:
    now = datetime.now(UTC)
    control, _sessions, _identity, bootstrap_dsn = await _control(postgres_runtime, now)
    first = await _read(control, now)
    await _admin(
        bootstrap_dsn,
        "UPDATE device_fleet_devices SET lifecycle_status = 'suspended' WHERE device_id = $1",
        _DEVICE,
    )
    with pytest.raises(PersistentSessionDenied, match="device_lifecycle_not_bound"):
        await _read(control, now + timedelta(minutes=6))
    assert await _active_profile_count(bootstrap_dsn, first.session_id) == 1


@requires_postgres
@pytest.mark.asyncio
async def test_closed_control_session_stays_closed(
    postgres_runtime: tuple[object, str],  # noqa: F811
) -> None:
    now = datetime.now(UTC)
    control, sessions, _identity, _bootstrap_dsn = await _control(postgres_runtime, now)
    first = await _read(control, now)
    closed = await sessions.close_session(
        actor_id=_OWNER,
        session_id=first.session_id,
        reason_code="test_closed",
        now=now + timedelta(minutes=1),
    )
    assert closed.applied is True
    for moment in (now + timedelta(minutes=2), now + timedelta(minutes=6)):
        with pytest.raises(PersistentSessionDenied, match="session is closed"):
            await _read(control, moment)


@requires_postgres
@pytest.mark.asyncio
async def test_explicit_session_ids_renew_only_for_control_sessions(
    postgres_runtime: tuple[object, str],  # noqa: F811
) -> None:
    now = datetime.now(UTC)
    control, sessions, _identity, _bootstrap_dsn = await _control(postgres_runtime, now)
    control_profile = await _read(control, now)
    media = await sessions.start(
        StartPersistentSessionCommand(
            session_id="media-session-renewal",
            actor_id=_OWNER,
            device_id=_DEVICE,
            expected_binding_version=1,
            idempotency_key="media-session-renewal",
            now=now,
            requested_capabilities=("chat",),
        )
    )
    later = now + timedelta(minutes=6)

    # Naming the control Session explicitly renews it like the default read.
    renewed = await _read(control, later, session_id=control_profile.session_id)
    assert renewed.session_id == control_profile.session_id
    assert renewed.session_epoch == control_profile.session_epoch + 1

    # Any other Session keeps strict expiry: the app never rotates it.
    with pytest.raises(PersistentSessionExpired):
        await _read(control, later, session_id=media.session_id)
    with pytest.raises(PersistentSessionExpired):
        await control.switch_subject(
            session_id=media.session_id,
            subject_id=_OWNER,
            actor_id=_OWNER,
            now=later,
        )
