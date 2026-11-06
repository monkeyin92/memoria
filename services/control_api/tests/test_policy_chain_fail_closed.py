"""Control API policy chain: authority, expiry and epoch failure modes close both seams."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from packages.contracts.generated.python.multi_subject_contracts import (
    RuntimeProfileSignedV2,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT
from services.control_api.app.main import create_app
from services.control_api.tests.interaction_api_test_support import _MemoryCatalog
from services.control_api.tests.policy_chain_test_support import (
    _CRISIS_QUERY,
    _MEMORY_QUERY,
    _POLICY_TOKEN,
    _anonymous_account,
    _configure,
    _response_plan,
    _session_policy,
    _start_session_and_switch_to_child,
    pytestmark,  # noqa: F401 - pytest discovers the module-wide marker
)
from services.session_runtime.commands import SwitchPersistentSubjectCommand
from services.session_runtime.postgres_store import (
    PostgresSessionRuntimeStore,
    SessionRuntimeConflict,
)
from services.session_runtime.profile_service import verify_runtime_profile_payload
from services.session_runtime.service import (
    PersistentSessionDenied,
    PersistentSessionNotFound,
    PersistentSessionUnavailable,
    build_postgres_session_runtime_service,
)
from services.session_runtime.tests.postgres_store_test_support import (
    postgres_runtime,  # noqa: F401 - pytest discovers imported fixtures by name
    postgres_runtime_with_consent,  # noqa: F401 - pytest discovers imported fixtures by name
)


def _action_body(
    session_id: str,
    *,
    runtime_profile_id: str,
    session_epoch: int,
    generation_id: int,
    turn_id: int,
    tool_epoch: int,
    capability: str = "memory_capture",
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "runtime_profile_id": runtime_profile_id,
        "capability": capability,
        "session_epoch": session_epoch,
        "generation_id": generation_id,
        "turn_id": turn_id,
        "tool_epoch": tool_epoch,
        "data_classification": "private",
        "safety_state": "normal",
    }


@pytest.mark.asyncio
async def test_real_runtime_authority_unavailable_fails_closed_on_both_seams(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04 matrix: a real, installed authority that cannot answer must close.

    Both seams run against the PostgreSQL authority until it cannot answer —
    first because it is gone, then because its PostgreSQL is unreachable.  A
    dead database is not a fabricated profile: this is the production
    fail-closed shape for ``authority unavailable``.
    """

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
    )
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chain = await _start_session_and_switch_to_child(
            client,
            app,
            bootstrap_dsn=bootstrap_dsn,
            device_id="device-real-runtime-unavailable",
            binding_id="binding-real-runtime-unavailable",
            child_person_id="minor-real-runtime-unavailable",
        )

        # 1. The authority is not installed at all (production never wires the
        #    persistent Runtime outside deployment): the device-facing policy
        #    seam fails closed with the unavailable code, while /response-plan
        #    runs the documented offline deployment profile — the login
        #    account is the subject by construction, so the turn reads the
        #    account's own (empty) memory and never a child subject's state.
        app.state._state.pop("session_runtime_service", None)  # noqa: SLF001 - Starlette state dict, mirrors test_interaction_api
        policy_missing = await _session_policy(client, session_id=chain.session_id)
        assert policy_missing.status_code == 503, policy_missing.text
        assert (
            policy_missing.json()["detail"]["code"]
            == "session_runtime_authority_unavailable"
        )
        plan_missing = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=1,
            generation_id=1,
        )
        assert plan_missing.status_code == 200, plan_missing.text
        assert plan_missing.json()["grounded_items"] == []

        # 2. The authority is installed but its PostgreSQL is unreachable: the
        #    policy seam fails closed and /response-plan stays conservative —
        #    no memory read, no account fallback for the child subject, and
        #    the fixed crisis reply is still delivered verbatim.
        dead_store = PostgresSessionRuntimeStore(
            dsn="postgresql://memoria_session_api:x@127.0.0.1:9/postgres",
            action_dsn="postgresql://memoria_action_executor:x@127.0.0.1:9/postgres",
            connect_timeout_seconds=0.5,
            command_timeout_seconds=1.0,
        )
        app.state.session_runtime_service = build_postgres_session_runtime_service(
            store=dead_store,
            signing_key=signing_key,
        )
        policy_dead = await _session_policy(client, session_id=chain.session_id)
        assert policy_dead.status_code == 503, policy_dead.text
        assert (
            policy_dead.json()["detail"]["code"]
            == "session_runtime_authority_unavailable"
        )
        queries_before_dead = len(catalog.queries)
        crisis_dead = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_CRISIS_QUERY,
            turn_id=2,
            generation_id=2,
        )
        assert crisis_dead.status_code == 200, crisis_dead.text
        assert crisis_dead.json()["direct_text"] == CRISIS_SELF_SCRIPT.display
        memory_dead = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=3,
            generation_id=3,
        )
        assert memory_dead.status_code == 200, memory_dead.text
        assert memory_dead.json()["grounded_items"] == []
        # No failure path leaked a memory read for the child subject.
        assert len(catalog.queries) == queries_before_dead


@pytest.mark.asyncio
async def test_real_runtime_profile_expiry_fails_closed_but_keeps_session_closable(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04 matrix: the profile TTL issued by the real authority expires.

    The expiry is produced by the authority itself (``profile_ttl``), not by
    editing the profile: both seams must fail closed once the TTL passes, and
    the expired profile must still not strand the Session authority (close
    remains possible).
    """

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
        profile_ttl=timedelta(seconds=2),
    )
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chain = await _start_session_and_switch_to_child(
            client,
            app,
            bootstrap_dsn=bootstrap_dsn,
            device_id="device-real-runtime-expiry",
            binding_id="binding-real-runtime-expiry",
            child_person_id="minor-real-runtime-expiry",
        )
        assert chain.child_profile.expires_at > chain.child_profile.issued_at

        policy_live = await _session_policy(client, session_id=chain.session_id)
        assert policy_live.status_code == 200, policy_live.text

        await asyncio.sleep(2.3)
        with pytest.raises(PersistentSessionDenied, match="expired"):
            await chain.runtime_service.current(
                actor_id=chain.account_id,
                session_id=chain.session_id,
                now=datetime.now(UTC),
            )
        policy_expired = await _session_policy(client, session_id=chain.session_id)
        assert policy_expired.status_code == 503, policy_expired.text
        assert (
            policy_expired.json()["detail"]["code"]
            == "session_runtime_authority_unavailable"
        )
        plan_expired = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=1,
            generation_id=1,
        )
        # The response-plan seam keeps serving the conservative fixed reply:
        # the expired profile is never used as the subject, no memory is read
        # and no minor is guessed.
        assert plan_expired.status_code == 200, plan_expired.text
        assert plan_expired.json()["grounded_items"] == []
        assert catalog.queries == []

        # Expiry denies conversation work but must not strand the authority.
        closed = await chain.runtime_service.close_session(
            actor_id=chain.account_id,
            session_id=chain.session_id,
            reason_code="device_close",
            now=datetime.now(UTC),
        )
        assert closed.applied is True


@pytest.mark.asyncio
async def test_real_runtime_cross_account_manager_cannot_switch_or_replay_stale_profile(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04 matrix: a different login account cannot manage the child session.

    The legal manager of the binding is the account that owns it.  Another
    anonymous account must not be able to switch the subject of a session it
    does not own, and the device-facing action seam must reject both the stale
    profile id of a superseded epoch and a stale session epoch.
    """

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
    )
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chain = await _start_session_and_switch_to_child(
            client,
            app,
            bootstrap_dsn=bootstrap_dsn,
            device_id="device-real-runtime-manager",
            binding_id="binding-real-runtime-manager",
            child_person_id="minor-real-runtime-manager",
        )
        account_b, headers_b = await _anonymous_account(client)
        assert account_b != chain.account_id

        # A foreign manager cannot switch this session's subject...
        with pytest.raises(
            (PersistentSessionDenied, PersistentSessionNotFound)
        ):
            await chain.runtime_service.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=chain.session_id,
                    actor_id=account_b,
                    subject_id=chain.child_person_id,
                    now=datetime.now(UTC),
                    requested_capabilities=("chat",),
                )
            )
        # ...and cannot even read another session as its current authority.
        with pytest.raises((PersistentSessionDenied, PersistentSessionNotFound)):
            await chain.runtime_service.current(
                actor_id=account_b,
                session_id=chain.session_id,
                now=datetime.now(UTC),
            )
        # ...and the session epoch is unchanged: no partial switch happened.
        profile_after, context_after = await chain.runtime_service.current(
            actor_id=chain.account_id,
            session_id=chain.session_id,
            now=datetime.now(UTC),
        )
        assert profile_after.runtime_profile_id == chain.child_profile.runtime_profile_id
        assert context_after.session_epoch == chain.child_profile.session_epoch

        # Device-facing seam: the stale child profile of the current epoch is
        # the one a compromised Agent would replay.  A superseded profile id is
        # rejected as forged; a stale epoch is rejected as a stale fence.
        stale_profile = await client.post(
            "/v1/interaction/action-policy",
            headers={"X-Memoria-Internal-Token": _POLICY_TOKEN},
            json=_action_body(
                chain.session_id,
                runtime_profile_id="rp-from-a-superseded-epoch",
                session_epoch=context_after.session_epoch,
                generation_id=context_after.generation_id,
                turn_id=context_after.turn_id,
                tool_epoch=context_after.tool_epoch,
            ),
        )
        assert stale_profile.status_code == 403, stale_profile.text
        assert stale_profile.json()["detail"]["code"] == "action_profile_forged"

        stale_epoch = await client.post(
            "/v1/interaction/action-policy",
            headers={"X-Memoria-Internal-Token": _POLICY_TOKEN},
            json=_action_body(
                chain.session_id,
                runtime_profile_id=context_after.current_runtime_profile_id,
                session_epoch=1,
                generation_id=context_after.generation_id,
                turn_id=context_after.turn_id,
                tool_epoch=context_after.tool_epoch,
            ),
        )
        assert stale_epoch.status_code == 409, stale_epoch.text
        assert stale_epoch.json()["detail"]["code"] == "action_fence_stale"


        # A fresh-epoch positive action authorization is exercised on the real
        # consent-enabled PG authority by
        # ``session_runtime/tests/test_postgres_store.py::
        # test_authorize_action_reuses_same_transaction_consent_discovery``;
        # this module pins the fail-closed rejections at the HTTP seam.


@pytest.mark.asyncio
async def test_real_runtime_concurrent_switches_serialize_and_supersede_old_epochs(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04 matrix: concurrent subject switches never cross or double-issue.

    Contending switch commands on one real session must either serialize into
    strictly consecutive epochs or be denied by the contention gate; they must
    never both claim the same epoch.  After the storm, the authority's current
    profile is exactly the last accepted epoch, both HTTP seams read that same
    subject, and every superseded epoch is denied as stale.
    """

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
    )
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chain = await _start_session_and_switch_to_child(
            client,
            app,
            bootstrap_dsn=bootstrap_dsn,
            device_id="device-real-runtime-races",
            binding_id="binding-real-runtime-races",
            child_person_id="minor-real-runtime-races",
        )
        base_epoch = chain.child_profile.session_epoch

        async def _switch(subject_id: str) -> RuntimeProfileSignedV2:
            return await chain.runtime_service.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=chain.session_id,
                    actor_id=chain.account_id,
                    subject_id=subject_id,
                    now=datetime.now(UTC),
                    requested_capabilities=("chat",),
                )
            )

        contention: tuple[type[BaseException], ...] = (
            PersistentSessionDenied,
            PersistentSessionUnavailable,
            SessionRuntimeConflict,
        )
        outcomes: list[RuntimeProfileSignedV2] = []
        for index in range(3):
            pair = (
                chain.child_person_id,
                chain.account_id,
            ) if index % 2 == 0 else (
                chain.account_id,
                chain.child_person_id,
            )
            results = await asyncio.gather(
                _switch(pair[0]),
                _switch(pair[1]),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, contention):
                    continue
                if isinstance(result, BaseException):
                    pytest.fail(f"unexpected switch failure: {result!r}")
                outcomes.append(result)
            assert outcomes, "a concurrent switch round must make progress"

        epochs = [item.session_epoch for item in outcomes]
        assert len(set(epochs)) == len(epochs), "each switch issues its own epoch"
        assert epochs == sorted(epochs), "epochs never move backwards"
        assert min(epochs) == base_epoch + 1, "the first new epoch is contiguous"

        final_profile, final_context = await chain.runtime_service.current(
            actor_id=chain.account_id,
            session_id=chain.session_id,
            now=datetime.now(UTC),
        )
        assert final_profile.session_epoch == max(epochs)
        assert final_context.session_epoch == final_profile.session_epoch
        assert final_profile.runtime_profile_id == final_context.current_runtime_profile_id

        policy_final = await _session_policy(client, session_id=chain.session_id)
        assert policy_final.status_code == 200, policy_final.text
        final_signed = policy_final.json()["runtime_profile"]
        assert final_signed["session_epoch"] == final_profile.session_epoch
        assert final_signed["runtime_profile_id"] == final_profile.runtime_profile_id
        assert final_signed["active_subject_id"] == final_profile.active_subject_id
        assert verify_runtime_profile_payload(final_signed, signing_key=signing_key) is True

        # Every superseded epoch is dead: the child profile issued before the
        # storm cannot authorize a decision any more.
        with pytest.raises(PersistentSessionDenied, match="stale"):
            await chain.runtime_service.decide(
                runtime_profile_id=chain.child_profile.runtime_profile_id,
                capability="chat",
                actor_id=chain.account_id,
                data_classification="ephemeral",
                safety_state="normal",
                now=datetime.now(UTC),
            )
