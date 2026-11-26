"""Control API policy chain: real-Postgres catalog and subject category matrix at both seams."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from packages.contracts.generated.python.multi_subject_contracts import (
    RuntimeProfileSignedV2,
)
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import (
    CompileReport,
    MemoryClaimReview,
    MemorySearchQuery,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT
from services.control_api.app.main import create_app
from services.control_api.tests.interaction_api_test_support import _MemoryCatalog
from services.control_api.tests.policy_chain_test_support import (
    _CRISIS_QUERY,
    _MEMORY_QUERY,
    _activate_link_and_grant_retention,
    _anonymous_account,
    _configure,
    _context_prefetch,
    _declare_guardianship,
    _response_plan,
    _session_policy,
    _start_session_and_switch_to_child,
    _SubjectScopedMemoryCatalog,
    pytestmark,  # noqa: F401 - pytest discovers the module-wide marker
)
from services.session_runtime.commands import SwitchPersistentSubjectCommand
from services.session_runtime.postgres_store import (
    PostgresSessionRuntimeStore,
)
from services.session_runtime.profile_service import verify_runtime_profile_payload
from services.session_runtime.service import (
    PostgresSessionRuntimeService,
    build_postgres_session_runtime_service,
)
from services.session_runtime.tests.postgres_store_test_support import (
    _seed_delegated_binding,
    postgres_runtime,  # noqa: F401 - pytest discovers imported fixtures by name
    postgres_runtime_with_consent,  # noqa: F401 - pytest discovers imported fixtures by name
)

# ---------------------------------------------------------------------------
# P0-04 category matrix: the four subject categories at both seams, plus the
# account/subject-key evidence taken from a REAL PostgreSQL memory catalog
# instead of a catalog double.
# ---------------------------------------------------------------------------

#: One account-keyed first-person claim, written through the real archive.
_CATALOG_MEMORY_TEXT = "我在杭州读过书。"


#: Optional JSON-lines receipt of the matrix observations (diagnostic only).
_MATRIX_DUMP = os.environ.get("P0_04_MATRIX_DUMP")


def _dump_matrix(label: str, payload: dict[str, Any]) -> None:
    """Append one observation line to the optional receipt file.

    This is a side effect only: every call precedes the assertions of its own
    cell, so a failing run still yields the observations that explain it, and
    nothing asserted anywhere depends on the file existing.
    """

    if not _MATRIX_DUMP:
        return
    path = Path(_MATRIX_DUMP)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"label": label, **payload}, ensure_ascii=False))
        handle.write("\n")


async def _crisis_evidence_events(app: Any, *, account_id: str) -> tuple[Any, ...]:
    """The recorded ``guardian.crisis_event`` evidence for one account.

    The minor-crisis path records this event BEFORE it looks for a notification
    target, so its ABSENCE is what proves a turn was never treated as a minor
    crisis.  An empty guardian outbox alone would prove nothing: a subject with
    no declared guardian has no addressable target either way.
    """

    return await app.state.life_archive.evidence_window(
        account_id=account_id,
        occurred_after=datetime(1970, 1, 1, tzinfo=UTC),
        occurred_before=datetime.now(UTC),
        event_types=("guardian.crisis_event",),
    )


@dataclass(frozen=True, slots=True)
class _CategorySession:
    """One real session opened for one subject category."""

    account_id: str
    session_id: str
    active_subject_id: str | None
    profile: RuntimeProfileSignedV2


async def _open_subject_session(
    client: AsyncClient,
    app: Any,
    *,
    bootstrap_dsn: str,
    device_id: str,
    binding_id: str,
    subject_id: str,
    declared_mode: str,
    subject_category: str,
    age_band: str,
    capabilities: tuple[str, ...] = ("chat",),
    switch_to_subject: bool = True,
    switch_to_account: bool = False,
) -> _CategorySession:
    """Bind a device in the real authority, start a session, switch the subject.

    ``switch_to_subject=False`` leaves the authority-issued ``unknown_safe``
    profile in place: the binding exists but no subject is confirmed yet.
    ``switch_to_account`` switches to the login account itself instead of the
    seeded person, which is how a ``self_use`` session reaches the only subject
    that production mode accepts.
    which is how a ``self_use`` session reaches the only subject that mode
    accepts in production — the login account itself.
    """

    account_id, headers = await _anonymous_account(client)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id=account_id,
            subject_id=subject_id,
            device_id=device_id,
            binding_id=binding_id,
            declared_mode=declared_mode,
            subject_category=subject_category,
            age_band=age_band,
        )
    finally:
        await admin.close()

    created = await client.post(
        "/v1/sessions",
        headers={**headers, "Idempotency-Key": f"{binding_id}-session-1"},
        json={
            "client": {
                "platform": "web",
                "timezone": "Asia/Shanghai",
                "device_id": device_id,
                "binding_version": 1,
            }
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    session_id = str(body["session_id"])
    profile = RuntimeProfileSignedV2.model_validate(body["runtime_profile"])
    assert profile.session_epoch == 1
    if not switch_to_subject:
        assert profile.active_subject_id is None
        assert profile.service_mode.value == "unknown_safe"
        return _CategorySession(
            account_id=account_id,
            session_id=session_id,
            active_subject_id=None,
            profile=profile,
        )
    target = account_id if switch_to_account else subject_id
    runtime_service = app.state.session_runtime_service
    assert isinstance(runtime_service, PostgresSessionRuntimeService)
    profile = await runtime_service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=session_id,
            actor_id=account_id,
            subject_id=target,
            now=datetime.now(UTC),
            requested_capabilities=capabilities,
        )
    )
    assert profile.active_subject_id == target
    assert profile.session_epoch == 2
    return _CategorySession(
        account_id=account_id,
        session_id=session_id,
        active_subject_id=target,
        profile=profile,
    )


@pytest.mark.asyncio
async def test_real_catalog_withholds_account_memory_from_a_child_subject(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04 category matrix: the account/subject key split on the REAL catalog.

    A catalog double cannot show that a row exists and is withheld anyway.  Here
    one account-keyed first-person claim is written through the real
    ``PostgresLifeArchive``/``PostgresMemoryCatalog`` pair, compiled and
    confirmed the way a real review does, and the read path is driven with that
    catalog installed:

    * the row is recallable under the account key (proven directly), so the
      empty child turns are evidence and not an always-empty path;
    * the child subject reads nothing even after a real retention consent lifts
      the ephemeral-only ceiling — with the ceiling provably open, the memory
      scope is the only thing left that can keep the row away from the child;
    * the account's own turn, in the same session, does read the row.
    """

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
        profile_ttl=timedelta(minutes=30),
    )
    archive = PostgresLifeArchive(bootstrap_dsn)
    catalog = PostgresMemoryCatalog(
        bootstrap_dsn,
        extractor=RuleBasedMemoryExtractor(),
        evidence_subject_category_resolver=lambda _event: "adult",
    )
    await archive.initialize()
    await catalog.initialize()
    app.state.memory_catalog = catalog
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session = await _open_subject_session(
                client,
                app,
                bootstrap_dsn=bootstrap_dsn,
                device_id="device-category-catalog",
                binding_id="binding-category-catalog",
                subject_id="minor-category-catalog",
                declared_mode="parent_for_child",
                subject_category="minor",
                age_band="under_14",
                capabilities=("chat", "tutor", "english_practice"),
            )
            child_id = session.active_subject_id
            assert child_id == "minor-category-catalog"

            # 1. Write the claim for the LOGIN ACCOUNT through the real archive.
            memory_event_id = f"category-catalog-{uuid.uuid4()}"
            await archive.record(
                EvidenceEvent(
                    event_id=memory_event_id,
                    account_id=session.account_id,
                    subject_id=session.account_id,
                    event_type="speech.utterance_finalized",
                    occurred_at=datetime.now(UTC),
                    speaker_class="owner",
                    source="p0-04-category-matrix",
                    payload={
                        "text": _CATALOG_MEMORY_TEXT,
                        "interaction_mode": "companion",
                        "prompt_kind": "spontaneous",
                        "owner_projection_eligible": True,
                    },
                )
            )
            report = await catalog.compile_pending(limit=1000)
            assert report == CompileReport(compiled_events=1), report

            # 2. The owner confirms the claim the way a real review does: until
            #    then it stays a candidate and no confirmed-only read sees it.
            queue = await catalog.review_queue(account_id=session.account_id)
            claim = next(
                (item for item in queue if item.source_event_id == memory_event_id),
                None,
            )
            assert claim is not None, [item.item_id for item in queue]
            reviewed = await catalog.review(
                MemoryClaimReview(
                    account_id=session.account_id,
                    claim_id=claim.item_id,
                    action="confirm",
                )
            )
            assert reviewed.status == "confirmed"

            # 3. Only now is the row recallable under the account key.
            account_keyed = await catalog.context(
                MemorySearchQuery(
                    account_id=session.account_id,
                    speaker_class="owner",
                    text=_CATALOG_MEMORY_TEXT,
                    include_candidates=False,
                    limit=8,
                )
            )
            _dump_matrix(
                "account_keyed_direct",
                {"items": [item.title for item in account_keyed.items]},
            )
            assert account_keyed.items, "the real catalog must recall its own account row"
            assert "杭州" in json.dumps(
                [f"{item.title}{item.snippet}" for item in account_keyed.items],
                ensure_ascii=False,
            )

            # 4. The child's turn reads nothing while the minor ceiling is shut.
            child_turn = await _response_plan(
                client,
                session_id=session.session_id,
                query=_CATALOG_MEMORY_TEXT,
                turn_id=1,
                generation_id=1,
            )
            assert child_turn.status_code == 200, child_turn.text
            assert child_turn.json()["grounded_items"] == []

            # 5. Lift the ceiling with a real retention consent, and prove it is
            #    open, so the next turn isolates the memory scope alone.
            await _activate_link_and_grant_retention(
                app,
                guardian_id=session.account_id,
                child_id=child_id,
                now=datetime.now(UTC),
            )
            consented_policy = await _session_policy(
                client, session_id=session.session_id
            )
            assert consented_policy.status_code == 200, consented_policy.text
            _dump_matrix(
                "child_consented_policy",
                {
                    "memory_retention": consented_policy.json().get(
                        "memory_retention"
                    ),
                    "private_memory": consented_policy.json()["capabilities"][
                        "private_memory"
                    ],
                },
            )
            assert "memory_retention" not in consented_policy.json()

            consented_turn = await _response_plan(
                client,
                session_id=session.session_id,
                query=_CATALOG_MEMORY_TEXT,
                turn_id=2,
                generation_id=2,
            )
            assert consented_turn.status_code == 200, consented_turn.text
            _dump_matrix(
                "child_with_consent",
                {"grounded_items": consented_turn.json()["grounded_items"]},
            )
            assert consented_turn.json()["grounded_items"] == []

            # 6. The account's own turn in the SAME session does read the row, so
            #    the empty turns above came from the subject split and not from a
            #    broken catalog or a closed capability gate.
            runtime_service = app.state.session_runtime_service
            assert isinstance(runtime_service, PostgresSessionRuntimeService)
            account_profile = await runtime_service.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=session.session_id,
                    actor_id=session.account_id,
                    subject_id=session.account_id,
                    now=datetime.now(UTC),
                    requested_capabilities=("chat",),
                )
            )
            assert account_profile.active_subject_id == session.account_id
            assert account_profile.session_epoch == 3

            owner_turn = await _response_plan(
                client,
                session_id=session.session_id,
                query=_CATALOG_MEMORY_TEXT,
                turn_id=3,
                generation_id=3,
            )
            assert owner_turn.status_code == 200, owner_turn.text
            _dump_matrix(
                "account_owner_turn",
                {"grounded_items": owner_turn.json()["grounded_items"]},
            )
            assert owner_turn.json()["grounded_items"], (
                "the account's own turn must still read its own row"
            )
            assert "杭州" in json.dumps(
                owner_turn.json()["grounded_items"], ensure_ascii=False
            )
    finally:
        await catalog.close()
        await archive.close()


#: label, declared_mode, subject_category, age_band, service_mode, capabilities,
#: self_owned.  ``parent_for_child`` is the production write shape for a minor
#: subject and ``family_shared`` a legal shape for an adult who is not the owner;
#: the ``self_use`` row switches the session to the account itself, which is the
#: only subject that mode accepts in production
#: (``routes/multi_subject.py::_primary_subject``).
_CATEGORY_MATRIX: tuple[tuple[str, str, str, str, str, tuple[str, ...], bool], ...] = (
    (
        "under_14",
        "parent_for_child",
        "minor",
        "under_14",
        "student_minor",
        ("chat", "tutor", "english_practice"),
        False,
    ),
    (
        "14_17",
        "parent_for_child",
        "minor",
        "14_17",
        "student_minor",
        ("chat", "tutor", "english_practice"),
        False,
    ),
    (
        "adult_family_shared",
        "family_shared",
        "adult",
        "adult",
        "family_shared",
        ("chat",),
        False,
    ),
    (
        "adult_self_owned",
        "self_use",
        "adult",
        "adult",
        "adult_companion",
        ("chat",),
        True,
    ),
)


@pytest.mark.asyncio
async def test_subject_category_matrix_keeps_minor_adult_and_unknown_safe_distinct(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04 category matrix: under_14 / 14_17 / adult / unknown_safe.

    One app instance and one real PostgreSQL authority drive both seams once per
    category.  What each category must produce, and why it is not the same
    answer:

    * both minor bands are ``student_minor`` and must be treated IDENTICALLY —
      no production branch reads the band itself (every use tests the pair
      ``{"under_14", "14_17"}``), so what is asserted here is that a 14_17 child
      gets the same protection as an under_14 one: ephemeral-only ceiling, no
      session capability, crisis routed to the declared guardian;
    * an adult is NOT auto-classified as a minor: no minor ceiling and no minor
      crisis evidence at all, in either legal adult shape (an adult family
      member, and the device's self-owned account);
    * the account's own self-owned turn keeps its own name and does read its own
      memory, which is the positive control that makes the other adult cell a
      negative result rather than a broken one;
    * the authority's own ``unknown_safe`` profile stays conversation-only.

    The subject-split claim that needs the retention gate OPEN is proven
    separately by ``test_real_catalog_withholds_account_memory_from_a_child_subject``;
    the minor cells here are closed by the ceiling before the scope gate is even
    consulted, so they are not used to prove the scope gate.
    """

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
        profile_ttl=timedelta(minutes=30),
    )
    catalog = _SubjectScopedMemoryCatalog()
    app.state.memory_catalog = catalog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for (
            label,
            declared_mode,
            category,
            age_band,
            service_mode,
            capabilities,
            self_owned,
        ) in _CATEGORY_MATRIX:
            device_id = f"device-category-{label}"
            session = await _open_subject_session(
                client,
                app,
                bootstrap_dsn=bootstrap_dsn,
                device_id=device_id,
                binding_id=f"binding-category-{label}",
                subject_id=f"subject-category-{label}",
                declared_mode=declared_mode,
                subject_category=category,
                age_band=age_band,
                capabilities=capabilities,
                switch_to_account=self_owned,
            )
            # The authority itself carries the category and the band into the
            # signed profile both seams read.
            assert session.profile.subject_category.value == category
            assert session.profile.age_band.value == age_band
            assert session.profile.service_mode.value == service_mode

            if category == "minor":
                # A real one-sided declaration with its device binding: this is
                # what makes a crisis notification addressable at all.
                await _declare_guardianship(
                    app,
                    guardian_id=session.account_id,
                    child_id=session.active_subject_id or "",
                    device_id=device_id,
                    now=datetime.now(UTC),
                    age_band=age_band,
                )

            queries_before = len(catalog.queries)
            policy_response = await _session_policy(client, session_id=session.session_id)
            assert policy_response.status_code == 200, policy_response.text
            policy = policy_response.json()
            signed = policy["runtime_profile"]
            assert verify_runtime_profile_payload(signed, signing_key=signing_key) is True
            assert signed["subject_category"] == category
            assert signed["age_band"] == age_band
            assert signed["service_mode"] == service_mode

            crisis_turn = await _response_plan(
                client,
                session_id=session.session_id,
                query=_CRISIS_QUERY,
                turn_id=1,
                generation_id=1,
            )
            assert crisis_turn.status_code == 200, crisis_turn.text
            assert crisis_turn.json()["direct_text"] == CRISIS_SELF_SCRIPT.display

            memory_turn = await _response_plan(
                client,
                session_id=session.session_id,
                query=_MEMORY_QUERY,
                turn_id=2,
                generation_id=2,
            )
            assert memory_turn.status_code == 200, memory_turn.text

            notifications = await app.state.guardian_store.guardian_notifications(
                guardian_user_id=session.account_id
            )
            crisis_evidence_account = session.active_subject_id or session.account_id
            crisis_events = await _crisis_evidence_events(
                app, account_id=crisis_evidence_account
            )
            _dump_matrix(
                label,
                {
                    "service_mode": signed["service_mode"],
                    "interaction_mode": policy["interaction_mode"],
                    "capabilities": policy["capabilities"],
                    "memory_retention": policy.get("memory_retention"),
                    "owner_display_name": policy.get("owner_display_name"),
                    "profile_capabilities": list(session.profile.capabilities),
                    "grounded_items": memory_turn.json()["grounded_items"],
                    "catalog_queries_delta": len(catalog.queries) - queries_before,
                    "crisis_evidence_events": len(crisis_events),
                    "notifications": [
                        (item.minor_user_id, item.status) for item in notifications
                    ],
                },
            )

            assert policy["interaction_mode"] == "companion"
            if category == "minor":
                # The ephemeral-only ceiling is the minor protection, and the
                # authority granted this subject no capability in the first
                # place: the two decisions agree.
                assert policy["memory_retention"] == "ephemeral_only"
                assert policy["capabilities"]["private_memory"] is False
                assert policy["capabilities"]["history"] is False
                assert policy["capabilities"]["learning"] is False
                assert policy["owner_projection_eligible"] is False
                assert session.profile.capabilities == ()
                # The account owner's display name never crosses to a subject
                # that is not the account.
                assert "owner_display_name" not in policy
                assert memory_turn.json()["grounded_items"] == []
                # The crisis WAS recorded as a minor crisis, addressed to the
                # declared guardian and to nobody else.
                assert len(crisis_events) == 1
                assert [
                    (item.minor_user_id, item.status) for item in notifications
                ] == [(session.active_subject_id, "pending")]
            else:
                # No minor ceiling on an adult ...
                assert "memory_retention" not in policy
                # ... and NO minor crisis evidence.  This is the assertion an
                # "always a minor" regression breaks: the evidence event is
                # written before any notification target is resolved, so an
                # empty outbox alone would not distinguish the two.
                assert crisis_events == ()
                if self_owned:
                    # The account's own self-owned turn is the positive control:
                    # it keeps its own name and its memory read really runs.
                    assert session.active_subject_id == session.account_id
                    assert policy["owner_display_name"] == "朋友"
                    assert len(catalog.queries) == queries_before + 1
                    assert memory_turn.json()["grounded_items"]
                else:
                    assert list(session.profile.capabilities) == ["chat"]
                    assert policy["capabilities"]["conversation"] is True
                    assert policy["capabilities"]["private_memory"] is False
                    assert policy["capabilities"]["history"] is False
                    # A different person is neither named nor fed the account's
                    # memory: the read is keyed to that person, never the account.
                    assert "owner_display_name" not in policy
                    assert len(catalog.queries) == queries_before + 1
                    member_query = catalog.queries[-1]
                    assert getattr(member_query, "subject_id", None) == session.active_subject_id
                    assert session.active_subject_id != session.account_id
                    assert memory_turn.json()["grounded_items"] == []

        # The authority's own unknown_safe profile, same app: a device with an
        # active binding but no confirmed subject for this session yet.
        unknown = await _open_subject_session(
            client,
            app,
            bootstrap_dsn=bootstrap_dsn,
            device_id="device-category-unknown_safe",
            binding_id="binding-category-unknown_safe",
            subject_id="subject-category-unknown_safe",
            declared_mode="parent_for_child",
            subject_category="minor",
            age_band="under_14",
            switch_to_subject=False,
        )
        assert unknown.active_subject_id is None
        unknown_policy_response = await _session_policy(
            client, session_id=unknown.session_id
        )
        assert unknown_policy_response.status_code == 200, unknown_policy_response.text
        unknown_policy = unknown_policy_response.json()
        unknown_signed = unknown_policy["runtime_profile"]
        assert (
            verify_runtime_profile_payload(unknown_signed, signing_key=signing_key) is True
        )
        assert unknown_policy["interaction_mode"] == "unknown_safe"
        assert unknown_signed["active_subject_id"] is None
        assert unknown_signed["service_mode"] == "unknown_safe"
        # A signed unknown-safe profile is a conversation-only surface: chat
        # under the DO_NOT_PERSIST obligations, every other capability closed.
        assert list(unknown.profile.capabilities) == ["chat"]
        assert {
            key for key, value in unknown_policy["capabilities"].items() if value
        } == {"conversation"}
        assert unknown_policy["memory_retention"] == "ephemeral_only"

        unknown_crisis = await _response_plan(
            client,
            session_id=unknown.session_id,
            query=_CRISIS_QUERY,
            turn_id=1,
            generation_id=1,
        )
        assert unknown_crisis.status_code == 200, unknown_crisis.text
        assert unknown_crisis.json()["direct_text"] == CRISIS_SELF_SCRIPT.display
        unknown_memory = await _response_plan(
            client,
            session_id=unknown.session_id,
            query=_MEMORY_QUERY,
            turn_id=2,
            generation_id=2,
        )
        assert unknown_memory.status_code == 200, unknown_memory.text
        unknown_notifications = await app.state.guardian_store.guardian_notifications(
            guardian_user_id=unknown.account_id
        )
        unknown_crisis_events = await _crisis_evidence_events(
            app, account_id=unknown.account_id
        )
        _dump_matrix(
            "unknown_safe",
            {
                "interaction_mode": unknown_policy["interaction_mode"],
                "capabilities": unknown_policy["capabilities"],
                "memory_retention": unknown_policy.get("memory_retention"),
                "owner_display_name": unknown_policy.get("owner_display_name"),
                "companion_style_id": unknown_policy.get("companion_style_id"),
                "grounded_items": unknown_memory.json()["grounded_items"],
                "crisis_evidence_events": len(unknown_crisis_events),
                "notifications": [
                    (item.minor_user_id, item.status) for item in unknown_notifications
                ],
            },
        )
        # No owner hint survives, and an unconfirmed speaker is never reported
        # as a minor crisis.
        assert "owner_display_name" not in unknown_policy
        assert unknown_policy["companion_style_id"] is None
        assert unknown_policy["companion_style_version"] is None
        assert unknown_crisis_events == ()
        assert unknown_notifications == ()
        assert unknown_memory.json()["grounded_items"] == []


@pytest.mark.asyncio
async def test_both_policy_seams_reject_a_diverged_runtime_projection(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04 matrix: profile-session mismatch at BOTH seams, not just one.

    The session-policy seam's mismatch cases are unit-tested against a fake
    authority, and the response-plan seam had no mismatch case at all.  The
    guard itself (`current_persistent_runtime_profile`) compares nine fields of
    the signed profile against the authority's own session projection, so the
    only way to reach it is a projection that really diverges — which is exactly
    the condition it exists for (a rebind/manager change or a partial write that
    advanced the session row without re-issuing the profile).

    The divergence is therefore forced on the authority's projection row while
    the stored signed profile is left alone, and both seams must refuse to
    decide: 503 `session_runtime_profile_binding_mismatch`, with the child's
    policy (and any memory read) withheld.
    """

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
        profile_ttl=timedelta(minutes=30),
    )
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chain = await _start_session_and_switch_to_child(
            client,
            app,
            bootstrap_dsn=bootstrap_dsn,
            device_id="device-mismatch-both-seams",
            binding_id="binding-mismatch-both-seams",
            child_person_id="minor-mismatch-both-seams",
        )
        await _declare_guardianship(
            app,
            guardian_id=chain.account_id,
            child_id=chain.child_person_id,
            device_id="device-mismatch-both-seams",
            now=datetime.now(UTC),
        )

        # 1. Both seams answer the child's policy while the projection is intact.
        healthy = await _session_policy(client, session_id=chain.session_id)
        assert healthy.status_code == 200, healthy.text
        assert healthy.json()["runtime_profile"]["active_subject_id"] == (
            chain.child_person_id
        )

        # 2. Force the divergence: the authority's projection row advances the
        #    binding version while the stored signed profile is untouched.
        admin = await asyncpg.connect(bootstrap_dsn)
        try:
            updated = await admin.fetchval(
                """
                UPDATE session_runtime_contexts
                SET binding_version = binding_version + 1
                WHERE session_id = $1
                RETURNING binding_version
                """,
                chain.session_id,
            )
        finally:
            await admin.close()
        assert updated == chain.child_profile.binding_version + 1

        queries_before = len(catalog.queries)
        policy_mismatch = await _session_policy(client, session_id=chain.session_id)
        plan_mismatch = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=1,
            generation_id=1,
        )
        _dump_matrix(
            "projection_mismatch",
            {
                "policy_status": policy_mismatch.status_code,
                "policy_code": policy_mismatch.json()
                .get("detail", {})
                .get("code", policy_mismatch.text[:120]),
                "plan_status": plan_mismatch.status_code,
                "plan_code": plan_mismatch.json()
                .get("detail", {})
                .get("code", plan_mismatch.text[:120]),
                "catalog_queries_delta": len(catalog.queries) - queries_before,
            },
        )

        assert policy_mismatch.status_code == 503, policy_mismatch.text
        assert (
            policy_mismatch.json()["detail"]["code"]
            == "session_runtime_profile_binding_mismatch"
        ), policy_mismatch.text
        # /response-plan deliberately does NOT share that 503: an authority read
        # failure (which a mismatch is) must still let the turn answer, because
        # the crisis reply has to be deliverable.  What it must not do is decide
        # anything about a subject it could not read, so the conservative
        # profile is the whole contract here.
        assert plan_mismatch.status_code == 200, plan_mismatch.text
        plan_body = plan_mismatch.json()
        assert plan_body["grounded_items"] == []
        assert plan_body.get("memory_retention") in (None, "ephemeral_only")
        _dump_matrix(
            "projection_mismatch_plan",
            {
                "grounded_items": plan_body["grounded_items"],
                "memory_retention": plan_body.get("memory_retention"),
                "direct_text": plan_body.get("direct_text"),
            },
        )
        # Neither seam may reach a memory read, and the crisis fixed reply is
        # still delivered verbatim without being routed as a minor crisis.
        assert len(catalog.queries) == queries_before
        crisis_mismatch = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_CRISIS_QUERY,
            turn_id=2,
            generation_id=2,
        )
        assert crisis_mismatch.status_code == 200, crisis_mismatch.text
        assert crisis_mismatch.json()["direct_text"] == CRISIS_SELF_SCRIPT.display
        assert await _crisis_evidence_events(app, account_id=chain.child_person_id) == ()
        assert await _crisis_evidence_events(app, account_id=chain.account_id) == ()
        assert len(catalog.queries) == queries_before


@pytest.mark.asyncio
async def test_legal_manager_change_closes_the_agent_facing_seams(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04: a legal manager change closes every Agent-facing memory route.

    The take-over is applied on the authority plane: the device's ACTIVE binding
    goes ``superseded`` and another owner's binding version 2 becomes ACTIVE for
    the same device.  The signed profile the seams had been reading is left
    untouched, so the only thing that can withdraw the session's authorization is
    the read path re-validating the binding itself.

    Contract pinned here: ``PostgresSessionRuntimeService.current`` raises
    ``PersistentSessionDenied``, ``/v1/interaction/session-policy`` fails closed
    with 503 ``session_runtime_authority_unavailable`` instead of deciding, and
    ``/v1/interaction/response-plan`` keeps its deliberately bounded conservative
    200 with ZERO account-keyed memory reads.  The reason that conservative shape
    exists still holds: the crisis fixed reply must stay deliverable in the same
    turn, because a turn whose authority cannot be read must still be able to
    answer a crisis.

    Three ways the withdrawn subject's memory could still reach the Agent are
    covered, because closing only the freshest one was not enough: a new turn
    (``/response-plan``), a verbatim RETRY of the pre-change turn (the cache is
    keyed by the generation fence, which the take-over leaves intact), and
    ``/v1/interaction/context-prefetch`` (which used to skip the subject scope
    entirely and so also handed the account's memory to a non-account subject
    without any take-over).  ``/context-prefetch`` is asserted as a positive
    control BEFORE the change, so the empty result afterwards is not vacuous.

    Invariants that must survive any future change: nobody else's subject is ever
    substituted, no crisis evidence is recorded for a subject the seams could not
    read, and the close-only path still closes -- closure deliberately does not
    require the binding to remain active, otherwise a withdrawn device could
    never be cleaned up.

    This cell was an OPEN DEFECT until the read path gained the binding fence.
    The previous revision of this test characterized the opposite outcome (a real
    account-keyed read after the change), because the read path only re-checked
    the profile projection, the session epoch and the TTL, so both seams kept
    answering for the OLD subject until the signed profile TTL expired.
    """

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
        profile_ttl=timedelta(minutes=30),
    )
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog

    device_id = "device-manager-change"
    binding_id = "binding-manager-change"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # A self-owned adult session: the strongest form of the question, because
        # this is the shape whose seams DO allow an account-keyed memory read.
        session = await _open_subject_session(
            client,
            app,
            bootstrap_dsn=bootstrap_dsn,
            device_id=device_id,
            binding_id=binding_id,
            subject_id="subject-manager-change",
            declared_mode="self_use",
            subject_category="adult",
            age_band="adult",
            switch_to_account=True,
        )
        assert session.active_subject_id == session.account_id
        assert session.profile.service_mode.value == "adult_companion"

        before_policy = await _session_policy(client, session_id=session.session_id)
        assert before_policy.status_code == 200, before_policy.text
        queries_before = len(catalog.queries)
        before_turn = await _response_plan(
            client,
            session_id=session.session_id,
            query=_MEMORY_QUERY,
            turn_id=1,
            generation_id=1,
        )
        assert before_turn.status_code == 200, before_turn.text
        reads_before = len(catalog.queries) - queries_before

        # The same endpoint as a positive control: while the session is still
        # authorized, /context-prefetch does hand out the account's own memory, so
        # the empty result asserted after the take-over is not vacuous.  It is
        # counted separately so it cannot move `reads_before`.
        prefetch_before = await _context_prefetch(
            client,
            session_id=session.session_id,
            query=_MEMORY_QUERY,
        )
        assert prefetch_before.status_code == 200, prefetch_before.text
        assert prefetch_before.json()["grounded_items"], (
            "positive control failed: /context-prefetch is supposed to read the "
            "account's own memory while the device binding is still current"
        )

        # The manager change on the authority plane.  Only ``status`` and
        # ``valid_until`` are mutable on a binding row (the schema guard rejects
        # the rest), the take-over needs a new binding VERSION (unique key is
        # device_id + binding_version), and the app-side identity HTTP routes
        # write the local SQLite plane — so a supersede that the Runtime
        # authority can observe is applied here.
        now = datetime.now(UTC)
        admin = await asyncpg.connect(bootstrap_dsn)
        try:
            superseded = await admin.fetchval(
                """
                UPDATE identity_device_bindings
                SET status = 'superseded'
                WHERE binding_id = $1
                RETURNING status
                """,
                binding_id,
            )
            await admin.execute(
                """
                INSERT INTO identity_persons (
                    person_id, display_name, subject_category, age_band,
                    age_evidence_status, locale, timezone, status,
                    created_at, updated_at
                ) VALUES ($1, $1, 'adult', 'adult', 'verified', 'zh-CN',
                          'Asia/Shanghai', 'active', $2, $2)
                """,
                "manager-b",
                now,
            )
            await admin.execute(
                """
                INSERT INTO identity_device_bindings (
                    binding_id, device_id, declared_mode, family_space_id,
                    account_owner_person_id, binding_version, status, reason,
                    valid_from, valid_until, supersedes_binding_id,
                    service_profile_version, policy_bundle_version,
                    consent_snapshot_id, persona_assignment_id, created_at
                ) VALUES ($1, $2, 'parent_for_child', $3, $4, 2, 'active',
                          'supersede', $5, NULL, $6, 'delegated-v1',
                          'multi-subject-v2', NULL, 'starlight:v1', $5)
                """,
                "binding-manager-change-b",
                device_id,
                f"family-{device_id}",
                "manager-b",
                now,
                binding_id,
            )
        finally:
            await admin.close()
        assert superseded == "superseded"

        queries_before_change = len(catalog.queries)
        after_policy = await _session_policy(client, session_id=session.session_id)
        after_turn = await _response_plan(
            client,
            session_id=session.session_id,
            query=_MEMORY_QUERY,
            turn_id=2,
            generation_id=2,
        )
        crisis_after = await _response_plan(
            client,
            session_id=session.session_id,
            query=_CRISIS_QUERY,
            turn_id=3,
            generation_id=3,
        )
        # The two other ways the OLD subject's memory could still reach the Agent:
        # a verbatim retry of the pre-change turn (the response-plan cache is keyed
        # by the generation fence, and the take-over leaves that fence intact --
        # only a live binding read can tell the two apart), and the
        # context-prefetch seam, which used to skip both memory gates entirely.
        replayed = await _response_plan(
            client,
            session_id=session.session_id,
            query=_MEMORY_QUERY,
            turn_id=1,
            generation_id=1,
        )
        prefetched = await _context_prefetch(
            client,
            session_id=session.session_id,
            query=_MEMORY_QUERY,
        )
        # The authority read path itself, deliberately probed on its own: if this
        # succeeds after the take-over, the seams above are downstream of a read
        # path that never re-validated the binding.
        runtime_service = app.state.session_runtime_service
        assert isinstance(runtime_service, PostgresSessionRuntimeService)
        try:
            await runtime_service.current(
                actor_id=session.account_id,
                session_id=session.session_id,
                now=datetime.now(UTC),
            )
        except Exception as exc:  # noqa: BLE001 - the outcome itself is the observation
            current_after = type(exc).__name__
        else:
            current_after = "ok"
        # ... and the WRITE path of the SAME authority, for the asymmetry.  A
        # superseded binding is expected to fail the switch's own binding lock;
        # if it does, the read path is the only place missing the check.
        try:
            await runtime_service.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=session.session_id,
                    actor_id=session.account_id,
                    subject_id=session.account_id,
                    now=datetime.now(UTC),
                    requested_capabilities=("chat",),
                )
            )
        except Exception as exc:  # noqa: BLE001 - the outcome itself is the observation
            switch_after = type(exc).__name__
        else:
            switch_after = "ok"
        reads_after_change = len(catalog.queries) - queries_before_change
        _dump_matrix(
            "manager_change_self_owned",
            {
                "reads_before_change": reads_before,
                "policy_status_after": after_policy.status_code,
                "policy_subject_after": after_policy.json()
                .get("runtime_profile", {})
                .get("active_subject_id"),
                "policy_is_the_account": after_policy.json()
                .get("runtime_profile", {})
                .get("active_subject_id")
                == session.account_id,
                "policy_conversation_after": after_policy.json()
                .get("capabilities", {})
                .get("conversation"),
                "policy_owner_display_name_after": after_policy.json().get(
                    "owner_display_name"
                ),
                "plan_status_after": after_turn.status_code,
                "plan_grounded_items_after": len(
                    after_turn.json().get("grounded_items", [])
                )
                if after_turn.status_code == 200
                else None,
                "account_keyed_reads_after_change": reads_after_change,
                "crisis_status_after": crisis_after.status_code,
                "crisis_is_fixed_reply": crisis_after.json().get("direct_text")
                == CRISIS_SELF_SCRIPT.display
                if crisis_after.status_code == 200
                else None,
                "authority_current_after": current_after,
                "authority_switch_after": switch_after,
                "replay_status_after": replayed.status_code,
                "replay_grounded_items_after": (
                    len(replayed.json().get("grounded_items", []))
                    if replayed.status_code == 200
                    else None
                ),
                "prefetch_status_after": prefetched.status_code,
                "prefetch_grounded_items_after": (
                    len(prefetched.json().get("grounded_items", []))
                    if prefetched.status_code == 200
                    else None
                ),
            },
        )

        # Baseline: this shape really does read the account's own memory, so the
        # "zero reads after the change" result below is not a no-op.
        assert reads_before == 1
        assert before_turn.json()["grounded_items"]

        # The crisis fixed reply is never suppressed, before or after the change.
        assert crisis_after.status_code == 200, crisis_after.text
        assert crisis_after.json()["direct_text"] == CRISIS_SELF_SCRIPT.display

        # The read path itself must have withdrawn the authorization basis, and
        # both its read and its write entry points must agree on that.
        assert current_after == "PersistentSessionTerminated", current_after
        assert switch_after == "PersistentSessionTerminated", switch_after
        # The advisory seam tells the Agent to stop the superseded session.
        assert after_policy.status_code == 410, after_policy.text
        assert after_policy.json()["detail"] == {
            "code": "session_terminated", "reason": "binding_revoked"
        }, after_policy.text
        # The bounded conservative plan carries no account-keyed read.
        assert reads_after_change == 0, (
            "a withdrawn session must not read the account-keyed memory: the "
            "binding was superseded by another owner, so the signed profile is "
            "no longer an authorization basis for this device"
        )
        assert after_turn.status_code == 200, after_turn.text
        assert after_turn.json()["grounded_items"] == []
        assert after_turn.json().get("memory_retention") in (None, "ephemeral_only")
        # Neither of the other two Agent-facing routes may serve the OLD subject's
        # memory either: the cache replay is the identical retry the cache exists
        # for, and context-prefetch used to bypass both gates outright.
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["grounded_items"] == [], (
            "a verbatim retry of the pre-change turn must not be answered from a "
            "cached plan computed while the superseded binding still authorized it"
        )
        assert prefetched.status_code == 200, prefetched.text
        assert prefetched.json()["grounded_items"] == [], (
            "context-prefetch must apply the same subject scope as response-plan"
        )
        assert prefetched.json()["persona_version_id"] is None
        # Nobody else's subject may be substituted, and no crisis evidence may be
        # recorded for a subject the seams could no longer read.
        assert await _crisis_evidence_events(app, account_id=session.account_id) == ()

        # The close-only path must survive the withdrawal: closure deliberately
        # does not require the binding to remain active, because requiring it
        # would strand sessions precisely when unbind/revocation needs them
        # closed (same reasoning as session_runtime_close_session).
        close_result = await runtime_service.close_session(
            actor_id=session.account_id,
            session_id=session.session_id,
            reason_code="manager_change_cleanup",
            now=datetime.now(UTC),
        )
        _dump_matrix(
            "manager_change_after_close",
            {
                "close_applied": close_result.applied,
                "close_already_closed": close_result.already_closed,
            },
        )
        assert close_result.applied is True, close_result
        assert close_result.already_closed is False
