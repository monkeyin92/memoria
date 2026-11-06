"""Session runtime Postgres: action-fence contracts, readiness probes, RLS and read snapshots."""
# ruff: noqa: F811 - imported pytest fixtures are shadowed by test injection

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    RuntimeProfileSignedV2,
)
from services.session_runtime.postgres_store import (
    SESSION_RUNTIME_SCHEMA_SQL,
    PostgresSessionRuntimeStore,
    SessionRuntimeAuthorityUnavailable,
    SessionRuntimeConflict,
    SessionRuntimeContext,
)
from services.session_runtime.tests.postgres_store_test_support import (
    _profile_event,
    _session_fence_receipt,
    _signed_unknown_profile,
    postgres_runtime,  # noqa: F401 - pytest discovers imported fixtures by name
)


def _action_fence_payload(
    context: SessionRuntimeContext,
    *,
    fence_kind: str,
    generation_id: int,
    turn_id: int,
    tool_epoch: int,
    occurred_at: datetime,
) -> dict[str, object]:
    return {
        "session_id": context.session_id,
        "runtime_profile_id": context.current_runtime_profile_id,
        "actor_id": context.actor_id,
        "device_id": context.device_id,
        "binding_id": context.binding_id,
        "binding_version": context.binding_version,
        "session_epoch": context.session_epoch,
        "active_subject_id": context.active_subject_id,
        "subject_revision": context.subject_revision,
        "fence_kind": fence_kind,
        "generation_id": generation_id,
        "turn_id": turn_id,
        "tool_epoch": tool_epoch,
        "occurred_at": occurred_at.isoformat(),
    }


async def _advance_fence_raw(
    store: PostgresSessionRuntimeStore,
    payload: dict[str, object],
    *,
    actor_id: str = "actor-a",
    device_id: str = "device-a",
) -> str:
    async with store.action_transaction(
        actor_id=actor_id,
        device_id=device_id,
    ) as connection:
        try:
            await connection.fetchval(
                "SELECT session_runtime_advance_action_fence($1::jsonb)",
                json.dumps(payload),
            )
        except asyncpg.PostgresError as exc:
            return exc.sqlstate or ""
    raise AssertionError("illegal action fence advance was accepted")


@pytest.mark.asyncio
async def test_postgres_initial_profile_roundtrip_is_generated_and_current(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-a",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-a-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"session-a-request").hexdigest(),
            idempotency_key="start-session-a",
        )
    async with store.read_transaction(actor_id="actor-a") as connection:
        loaded = await store.current_profile(connection, session_id="session-a")

    assert isinstance(loaded, RuntimeProfileSignedV2)
    assert loaded == profile


@pytest.mark.asyncio
async def test_postgres_force_rls_hides_other_binding_session(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    for suffix in ("a", "b"):
        profile = _signed_unknown_profile(
            session_id=f"session-{suffix}",
            actor_id=f"actor-{suffix}",
            device_id=f"device-{suffix}",
            binding_id=f"binding-{suffix}",
            profile_id=f"rp-session-{suffix}-1",
            issued_at=now,
        )
        async with store.action_transaction(
            actor_id=f"actor-{suffix}",
            device_id=f"device-{suffix}",
        ) as connection:
            await store.persist_initial(
                connection,
                context=SessionRuntimeContext.from_profile(
                    profile,
                    profile_revision=1,
                ),
                profile=profile,
                event=_profile_event(profile),
                request_hash=hashlib.sha256(
                    f"session-{suffix}-request".encode()
                ).hexdigest(),
                idempotency_key=f"start-session-{suffix}",
            )

    async with store.read_transaction(actor_id="actor-a") as connection:
        own = await store.current_profile(connection, session_id="session-a")
        other = await store.current_profile(connection, session_id="session-b")

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        forced = {
            str(row["relname"])
            for row in await admin.fetch(
                """
                SELECT relname FROM pg_class
                WHERE relname LIKE 'session_runtime_%'
                  AND relkind = 'r'
                  AND relforcerowsecurity
                """
            )
        }
        role = await admin.fetchrow(
            """
            SELECT rolsuper, rolbypassrls FROM pg_roles
            WHERE rolname = 'memoria_session_api'
            """
        )
    finally:
        await admin.close()

    assert own is not None
    assert other is None
    assert forced == {
        "session_runtime_contexts",
        "session_runtime_events",
        "session_runtime_idempotency",
        "session_runtime_outbox",
        "session_runtime_profile_receipts",
        "session_runtime_profiles",
        "session_runtime_tool_effect_intents",
        "session_runtime_tool_effect_outbox",
    }
    assert role is not None
    assert (role["rolsuper"], role["rolbypassrls"]) == (False, False)


@pytest.mark.asyncio
async def test_postgres_readiness_reports_routing_rls_policy_and_exec_state(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime

    assert await store.readiness() == {
        "session_runtime_schema": "ready",
        "session_runtime_rls": "ready",
        "session_runtime_policies": "ready",
        "session_runtime_read_role": "ready",
        "action_executor_role": "ready",
        "action_executor_exec": "ready",
        "action_executor_consent_discover_exec": "unavailable",
    }


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_force_rls(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "ALTER TABLE session_runtime_profiles NO FORCE ROW LEVEL SECURITY"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="RLS"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_required_policy(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "DROP POLICY session_runtime_api_profiles ON session_runtime_profiles"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="policies"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_action_executor_execute(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "REVOKE EXECUTE ON FUNCTION action_policy_insert_receipt(jsonb) "
            "FROM memoria_action_executor"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="EXECUTE"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_action_executor_table_privilege(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "GRANT SELECT ON TABLE identity_device_binding_roles "
            "TO memoria_action_executor"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="table privileges"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_action_fence_advances_turn_interrupt_and_tool_monotonically(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-fence-advance",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-fence-advance-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"session-fence-advance").hexdigest(),
            idempotency_key="start-session-fence-advance",
        )
        with pytest.raises(ValueError, match="monotonic"):
            await store.advance_action_fence(
                connection,
                context=context,
                fence_kind="turn",
                next_generation_id=0,
                next_turn_id=0,
                next_tool_epoch=0,
                occurred_at=now + timedelta(seconds=1),
            )
        turned = await store.advance_action_fence(
            connection,
            context=context,
            fence_kind="turn",
            next_generation_id=1,
            next_turn_id=1,
            next_tool_epoch=0,
            occurred_at=now + timedelta(seconds=1),
        )
        interrupted = await store.advance_action_fence(
            connection,
            context=turned,
            fence_kind="interrupt",
            next_generation_id=2,
            next_turn_id=1,
            next_tool_epoch=0,
            occurred_at=now + timedelta(seconds=2),
        )
        tooled = await store.advance_action_fence(
            connection,
            context=interrupted,
            fence_kind="tool",
            next_generation_id=3,
            next_turn_id=1,
            next_tool_epoch=1,
            occurred_at=now + timedelta(seconds=3),
        )

    assert (
        turned.generation_id,
        turned.turn_id,
        turned.tool_epoch,
    ) == (1, 1, 0)
    assert (
        interrupted.generation_id,
        interrupted.turn_id,
        interrupted.tool_epoch,
    ) == (2, 1, 0)
    assert (tooled.generation_id, tooled.turn_id, tooled.tool_epoch) == (3, 1, 1)
    assert tooled.updated_at == now + timedelta(seconds=3)

    async with store.read_transaction(actor_id="actor-a") as connection:
        current = await store.current_context(
            connection,
            session_id="session-fence-advance",
        )
    assert current is not None
    assert (
        current.generation_id,
        current.turn_id,
        current.tool_epoch,
    ) == (3, 1, 1)
    assert current.updated_at == now + timedelta(seconds=3)


@pytest.mark.asyncio
async def test_postgres_action_fence_rejects_illegal_transitions_at_database_boundary(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-fence-illegal",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-fence-illegal-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"session-fence-illegal").hexdigest(),
            idempotency_key="start-session-fence-illegal",
        )

    conflicting_advances = (
        ("turn", 1, 2, 0),  # turn skips a step
        ("turn", 0, 0, 0),  # no-op replay is not an advance
        ("turn", 1, 1, 1),  # turn mixed with a tool bump
        ("interrupt", 2, 1, 0),  # generation skips a step
        ("interrupt", 1, 1, 0),  # interrupt mixed with a turn bump
        ("tool", 1, 0, 2),  # tool epoch skips a step
    )
    for fence_kind, generation_id, turn_id, tool_epoch in conflicting_advances:
        sqlstate = await _advance_fence_raw(
            store,
            _action_fence_payload(
                context,
                fence_kind=fence_kind,
                generation_id=generation_id,
                turn_id=turn_id,
                tool_epoch=tool_epoch,
                occurred_at=now + timedelta(seconds=1),
            ),
        )
        assert sqlstate == "SR409", (fence_kind, generation_id, turn_id, tool_epoch)

    unknown_kind = await _advance_fence_raw(
        store,
        _action_fence_payload(
            context,
            fence_kind="chat",
            generation_id=1,
            turn_id=1,
            tool_epoch=0,
            occurred_at=now + timedelta(seconds=1),
        ),
    )
    assert unknown_kind == "SR400"

    async with store.read_transaction(actor_id="actor-a") as connection:
        current = await store.current_context(
            connection,
            session_id="session-fence-illegal",
        )
    assert current is not None
    assert (
        current.generation_id,
        current.turn_id,
        current.tool_epoch,
    ) == (0, 0, 0)


@pytest.mark.asyncio
async def test_postgres_action_fence_rejects_stale_or_foreign_context(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-fence-stale",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-fence-stale-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"session-fence-stale").hexdigest(),
            idempotency_key="start-session-fence-stale",
        )

    stale_epoch = SessionRuntimeContext(
        session_id=context.session_id,
        actor_id=context.actor_id,
        device_id=context.device_id,
        binding_id=context.binding_id,
        binding_version=context.binding_version,
        active_subject_id=context.active_subject_id,
        subject_revision=context.subject_revision,
        session_epoch=99,
        profile_revision=context.profile_revision,
        current_runtime_profile_id=context.current_runtime_profile_id,
        generation_id=context.generation_id,
        turn_id=context.turn_id,
        tool_epoch=context.tool_epoch,
        created_at=context.created_at,
        updated_at=context.updated_at,
    )
    foreign_profile = SessionRuntimeContext(
        session_id=context.session_id,
        actor_id=context.actor_id,
        device_id=context.device_id,
        binding_id=context.binding_id,
        binding_version=context.binding_version,
        active_subject_id=context.active_subject_id,
        subject_revision=context.subject_revision,
        session_epoch=context.session_epoch,
        profile_revision=context.profile_revision,
        current_runtime_profile_id="rp-not-current",
        generation_id=context.generation_id,
        turn_id=context.turn_id,
        tool_epoch=context.tool_epoch,
        created_at=context.created_at,
        updated_at=context.updated_at,
    )
    unknown_session = SessionRuntimeContext(
        session_id="session-none",
        actor_id=context.actor_id,
        device_id=context.device_id,
        binding_id=context.binding_id,
        binding_version=context.binding_version,
        active_subject_id=context.active_subject_id,
        subject_revision=context.subject_revision,
        session_epoch=context.session_epoch,
        profile_revision=context.profile_revision,
        current_runtime_profile_id=context.current_runtime_profile_id,
        generation_id=context.generation_id,
        turn_id=context.turn_id,
        tool_epoch=context.tool_epoch,
        created_at=context.created_at,
        updated_at=context.updated_at,
    )
    foreign_actor = SessionRuntimeContext(
        session_id=context.session_id,
        actor_id="actor-b",
        device_id=context.device_id,
        binding_id=context.binding_id,
        binding_version=context.binding_version,
        active_subject_id=context.active_subject_id,
        subject_revision=context.subject_revision,
        session_epoch=context.session_epoch,
        profile_revision=context.profile_revision,
        current_runtime_profile_id=context.current_runtime_profile_id,
        generation_id=context.generation_id,
        turn_id=context.turn_id,
        tool_epoch=context.tool_epoch,
        created_at=context.created_at,
        updated_at=context.updated_at,
    )
    foreign_binding = SessionRuntimeContext(
        session_id=context.session_id,
        actor_id=context.actor_id,
        device_id=context.device_id,
        binding_id="binding-b",
        binding_version=context.binding_version,
        active_subject_id=context.active_subject_id,
        subject_revision=context.subject_revision,
        session_epoch=context.session_epoch,
        profile_revision=context.profile_revision,
        current_runtime_profile_id=context.current_runtime_profile_id,
        generation_id=context.generation_id,
        turn_id=context.turn_id,
        tool_epoch=context.tool_epoch,
        created_at=context.created_at,
        updated_at=context.updated_at,
    )
    cases = (
        (stale_epoch, "actor-a", "device-a", "SR412"),
        (foreign_profile, "actor-a", "device-a", "SR412"),
        (unknown_session, "actor-a", "device-a", "SR412"),
        (foreign_actor, "actor-a", "device-a", "SR403"),
        (foreign_binding, "actor-a", "device-a", "SR403"),
    )
    for claimed, actor_id, device_id, expected in cases:
        sqlstate = await _advance_fence_raw(
            store,
            _action_fence_payload(
                claimed,
                fence_kind="turn",
                generation_id=1,
                turn_id=1,
                tool_epoch=0,
                occurred_at=now + timedelta(seconds=1),
            ),
            actor_id=actor_id,
            device_id=device_id,
        )
        assert sqlstate == expected, (claimed.session_id, actor_id)

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        advanced = await store.advance_action_fence(
            connection,
            context=context,
            fence_kind="turn",
            next_generation_id=1,
            next_turn_id=1,
            next_tool_epoch=0,
            occurred_at=now + timedelta(seconds=1),
        )
    assert (advanced.generation_id, advanced.turn_id, advanced.tool_epoch) == (
        1,
        1,
        0,
    )


@pytest.mark.asyncio
async def test_postgres_action_receipt_authority_locks_exact_fence_and_replays_idempotently(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-fence-receipt",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-fence-receipt-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"session-fence-receipt").hexdigest(),
            idempotency_key="start-session-fence-receipt",
        )

    receipt = _session_fence_receipt(
        receipt_id="receipt-fence-1",
        profile=profile,
        generation_id=0,
        turn_id=0,
        tool_epoch=0,
        issued_at=now,
    )
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        inserted = await admin.execute(
            """
            INSERT INTO policy_receipts_v2
            SELECT (jsonb_populate_record(NULL::policy_receipts_v2, $1::jsonb)).*
            """,
            json.dumps(receipt.model_dump(mode="json")),
        )
    finally:
        await admin.close()
    assert inserted == "INSERT 0 1"

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        locked = await store.lock_receipt_authority(
            connection,
            receipt=receipt,
            generation_id=0,
            turn_id=0,
            tool_epoch=0,
        )
        replayed = await store.lock_receipt_authority(
            connection,
            receipt=receipt,
            generation_id=0,
            turn_id=0,
            tool_epoch=0,
        )
        missing = await store.lock_receipt_authority(
            connection,
            receipt=receipt.model_copy(
                update={"receipt_id": "receipt-fence-missing"}
            ),
            generation_id=0,
            turn_id=0,
            tool_epoch=0,
        )
        assert locked == receipt
        assert replayed == receipt
        assert missing is None

        advanced = await store.advance_action_fence(
            connection,
            context=context,
            fence_kind="turn",
            next_generation_id=1,
            next_turn_id=1,
            next_tool_epoch=0,
            occurred_at=now + timedelta(seconds=1),
        )
        assert (advanced.generation_id, advanced.turn_id, advanced.tool_epoch) == (
            1,
            1,
            0,
        )

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        with pytest.raises(SessionRuntimeConflict, match="not current"):
            await store.lock_receipt_authority(
                connection,
                receipt=receipt,
                generation_id=0,
                turn_id=0,
                tool_epoch=0,
            )
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        with pytest.raises(SessionRuntimeConflict, match="mismatch"):
            await store.lock_receipt_authority(
                connection,
                receipt=receipt,
                generation_id=1,
                turn_id=1,
                tool_epoch=0,
            )


@pytest.mark.asyncio
async def test_postgres_action_fence_ports_are_security_definer_fixed_path_and_executor_only(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        rows = await admin.fetch(
            """
            SELECT p.proname,
                   p.prosecdef,
                   pg_get_userbyid(p.proowner) AS owner,
                   p.proconfig,
                   has_function_privilege(
                       'memoria_action_executor', p.oid, 'EXECUTE'
                   ) AS executor_exec,
                   has_function_privilege('public', p.oid, 'EXECUTE')
                       AS public_exec
            FROM pg_proc p
            WHERE p.oid IN (
                to_regprocedure(
                    'public.session_runtime_advance_action_fence(jsonb)'),
                to_regprocedure(
                    'public.session_runtime_action_receipt_authority(text,jsonb)')
            )
            ORDER BY p.proname
            """
        )
        helpers = await admin.fetch(
            """
            SELECT p.proname,
                   has_function_privilege('public', p.oid, 'EXECUTE')
                       AS public_exec
            FROM pg_proc p
            WHERE p.oid IN (
                to_regprocedure(
                    'public.session_runtime_fence_json_valid(jsonb,text[])'),
                to_regprocedure(
                    'public.session_runtime_fence_text_valid('
                    'jsonb,integer,integer)'),
                to_regprocedure(
                    'public.session_runtime_fence_int_valid(jsonb,integer)')
            )
            ORDER BY p.proname
            """
        )
    finally:
        await admin.close()

    assert {str(row["proname"]) for row in rows} == {
        "session_runtime_action_receipt_authority",
        "session_runtime_advance_action_fence",
    }
    for row in rows:
        assert row["prosecdef"] is True
        assert str(row["owner"]) == "memoria_session_owner"
        config = [str(item) for item in row["proconfig"] or []]
        assert "search_path=pg_catalog, public" in config
        assert "row_security=on" in config
        assert row["executor_exec"] is True
        assert row["public_exec"] is False
    assert {str(row["proname"]) for row in helpers} == {
        "session_runtime_fence_int_valid",
        "session_runtime_fence_json_valid",
        "session_runtime_fence_text_valid",
    }
    assert all(row["public_exec"] is False for row in helpers)


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_advance_action_fence_function(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "DROP FUNCTION session_runtime_advance_action_fence(jsonb)"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="schema"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_receipt_authority_execute(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "REVOKE EXECUTE ON FUNCTION "
            "session_runtime_action_receipt_authority(text, jsonb) "
            "FROM memoria_action_executor"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="EXECUTE"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_readiness_rejects_missing_binding_fence_execute(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    """The read-role binding fence must be a verified grant, not a hope.

    Without EXECUTE the fence cannot answer, and the read path would turn that
    into a 503 for every turn instead of denying only the withdrawn one -- so the
    grant is part of the contract and its absence has to fail readiness.
    """

    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "REVOKE EXECUTE ON FUNCTION "
            "action_identity_binding_is_current(text, text, integer, timestamptz) "
            "FROM memoria_session_api"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="EXECUTE"):
        await store.readiness()


@pytest.mark.asyncio
async def test_postgres_binding_fence_port_is_definer_fixed_path_and_read_role_only(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    """The binding fence is a fixed-path definer port granted to the read role.

    It reads an Identity table that the read role has no privilege on, so the
    security properties are part of the contract: definer rights, a fixed
    ``search_path``, RLS still on, no PUBLIC EXECUTE, and EXECUTE for exactly the
    read role.  The write-side ports are executor-only; this one deliberately is
    not, because the read path is what applies it.
    """

    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        row = await admin.fetchrow(
            """
            SELECT p.proname,
                   p.prosecdef,
                   p.provolatile::text AS provolatile,
                   pg_get_userbyid(p.proowner) AS owner,
                   p.proconfig,
                   has_function_privilege(
                       'memoria_session_api', p.oid, 'EXECUTE'
                   ) AS read_exec,
                   has_function_privilege(
                       'memoria_action_executor', p.oid, 'EXECUTE'
                   ) AS executor_exec,
                   has_function_privilege('public', p.oid, 'EXECUTE')
                       AS public_exec
            FROM pg_proc p
            WHERE p.oid = to_regprocedure(
                'public.action_identity_binding_is_current'
                '(text,text,integer,timestamptz)'
            )
            """
        )
    finally:
        await admin.close()

    assert row is not None
    assert str(row["proname"]) == "action_identity_binding_is_current"
    assert row["prosecdef"] is True
    assert str(row["owner"]) == "memoria_identity_owner"
    config = [str(item) for item in row["proconfig"] or []]
    assert "search_path=pg_catalog, public" in config
    assert "row_security=on" in config
    assert str(row["provolatile"]) == "s"
    assert row["read_exec"] is True
    assert row["public_exec"] is False
    # The action executor runs under its own binding lock; it must not be handed
    # this read-path predicate as well.
    assert row["executor_exec"] is False


@pytest.mark.asyncio
async def test_postgres_action_executor_cannot_read_fence_tables_directly(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        with pytest.raises(asyncpg.PostgresError) as contexts_exc:
            await connection.fetchval("SELECT count(*) FROM session_runtime_contexts")
        assert contexts_exc.value.sqlstate == "42501"
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        with pytest.raises(asyncpg.PostgresError) as receipts_exc:
            await connection.fetchval("SELECT count(*) FROM policy_receipts_v2")
        assert receipts_exc.value.sqlstate == "42501"


@pytest.mark.asyncio
async def test_postgres_consent_discover_action_fence_wiring_is_conditional(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    assert (
        await store.readiness()
    )["action_executor_consent_discover_exec"] == "unavailable"

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            """
            CREATE OR REPLACE FUNCTION consent_discover_action_fence(
                text, text, text, text, text, integer, text, text, timestamptz
            ) RETURNS boolean
            LANGUAGE sql
            AS $$ SELECT true $$
            """
        )
        await admin.execute(
            "REVOKE ALL ON FUNCTION consent_discover_action_fence("
            "text, text, text, text, text, integer, text, text, timestamptz"
            ") FROM PUBLIC"
        )
    finally:
        await admin.close()

    with pytest.raises(SessionRuntimeAuthorityUnavailable, match="EXECUTE"):
        await store.readiness()

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(SESSION_RUNTIME_SCHEMA_SQL)
    finally:
        await admin.close()
    report = await store.readiness()
    assert report["action_executor_consent_discover_exec"] == "ready"


def test_session_schema_never_grants_executor_consent_mutation_lock_ports() -> None:
    assert "consent_discover_action_fence" in SESSION_RUNTIME_SCHEMA_SQL
    assert not re.search(
        r"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+consent_lock_(?:authority|snapshot)_head"
        r"[\s\S]{0,240}?TO\s+memoria_action_executor",
        SESSION_RUNTIME_SCHEMA_SQL,
        re.IGNORECASE,
    )


@pytest.mark.asyncio
async def test_read_transaction_binding_fence_shares_profile_snapshot(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    """P0-04 read consistency: the binding fence reads the profile's snapshot.

    A binding revoked between the profile read and the fence check must not
    flip one ``current()`` call's answer mid-read: the open transaction still
    reports the binding current, while a fresh transaction observes the
    revocation. Fails on read-committed reads; passes on REPEATABLE READ.
    """

    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-read-fence",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-read-fence-1",
        issued_at=now,
    )
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=SessionRuntimeContext.from_profile(profile, profile_revision=1),
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"read-fence-request").hexdigest(),
            idempotency_key="start-session-read-fence",
        )
    async with store.read_transaction(actor_id="actor-a") as reader:
        loaded = await store.current_profile(reader, session_id="session-read-fence")
        assert loaded is not None
        admin = await asyncpg.connect(bootstrap_dsn)
        try:
            await admin.execute(
                "UPDATE identity_device_bindings SET status = 'superseded' "
                "WHERE binding_id = 'binding-a'",
            )
        finally:
            await admin.close()
        fenced = await store.binding_is_current(
            reader,
            device_id="device-a",
            binding_id="binding-a",
            binding_version=1,
            now=datetime.now(UTC),
        )
        assert fenced is True
    async with store.read_transaction(actor_id="actor-a") as fresh:
        withdrawn = await store.binding_is_current(
            fresh,
            device_id="device-a",
            binding_id="binding-a",
            binding_version=1,
            now=datetime.now(UTC),
        )
    assert withdrawn is False
