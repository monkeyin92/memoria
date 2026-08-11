from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from packages.contracts.generated.python.multi_subject_contracts import PolicyReceiptV2
from services.policy.action_authorizer import (
    ActionAuthorizationError,
    ActionExecutionRequest,
    CompositeActionAuthority,
    PostgresActionAuthorizer,
)
from services.policy.action_fence import (
    build_action_resource_fence,
    build_approval_snapshot_fence,
)
from services.policy.engine import PolicyEngine
from services.policy.postgres_receipt_repository import (
    POLICY_RECEIPTS_V2_SCHEMA_SQL,
    ConnectionBoundPolicyReceiptRepository,
)
from services.policy.receipts import PolicyReceiptConflictError
from services.policy.session_batch import SessionPolicyBatchAuthorizer
from services.policy.tests.fakes import (
    FakeApprovalEvidence,
    FakeCaptureEvidence,
    FakeMembershipEvidence,
    FakeProposalEvidence,
    make_binding,
    make_consent,
    make_context,
)

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
PROJECTOR_ROLE = "memoria_policy_projector"
PROJECTOR_PASSWORD = "memoria_policy_projector_test"


def _dsn_with(
    dsn: str, *, database: str, user: str | None = None, password: str | None = None
) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    username = quote(user or parsed.username or "postgres")
    secret = quote(password if password is not None else parsed.password or "")
    credentials = f"{username}:{secret}" if secret else username
    return urlunsplit(
        (parsed.scheme, f"{credentials}@{host}", f"/{database}", parsed.query, "")
    )


@dataclass(frozen=True, slots=True)
class _PgEnvironment:
    projector_dsn: str
    admin_dsn: str


@pytest.fixture
async def composite_pg() -> _PgEnvironment:
    root_dsn = os.getenv("MEMORIA_TEST_POSTGRES_DSN")
    if not root_dsn:
        pytest.skip(
            "set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL action-authorizer contract"
        )
    database = f"memoria_policy_action_{uuid.uuid4().hex[:10]}"
    root = await asyncpg.connect(root_dsn)
    try:
        await root.execute(f"CREATE DATABASE {database}")
    finally:
        await root.close()
    admin_dsn = _dsn_with(root_dsn, database=database)
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(POLICY_RECEIPTS_V2_SCHEMA_SQL)
        await admin.execute(
            f"ALTER ROLE {PROJECTOR_ROLE} PASSWORD '{PROJECTOR_PASSWORD}'"
        )
        await admin.execute(
            """
            CREATE TABLE policy_test_authority_head (
                stage TEXT NOT NULL,
                authority_key TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                canonical_hash TEXT NOT NULL,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                PRIMARY KEY (stage, authority_key)
            );
            CREATE TABLE policy_test_business_write (
                write_id TEXT PRIMARY KEY,
                backend_pid INTEGER NOT NULL
            );
            GRANT SELECT, UPDATE ON policy_test_authority_head
                TO memoria_policy_projector;
            GRANT SELECT, INSERT ON policy_test_business_write
                TO memoria_policy_projector;
            """
        )
    finally:
        await admin.close()
    projector_dsn = _dsn_with(
        admin_dsn,
        database=database,
        user=PROJECTOR_ROLE,
        password=PROJECTOR_PASSWORD,
    )
    try:
        yield _PgEnvironment(projector_dsn=projector_dsn, admin_dsn=admin_dsn)
    finally:
        root = await asyncpg.connect(root_dsn)
        try:
            await root.execute(f"DROP DATABASE IF EXISTS {database}")
        finally:
            await root.close()


async def _put_head(
    connection: asyncpg.Connection,
    stage: str,
    key: str,
    *,
    status: str = "active",
    revision: int = 1,
    canonical_hash: str = "a" * 64,
    payload: dict[str, object] | None = None,
) -> None:
    await connection.execute(
        """
        INSERT INTO policy_test_authority_head (
            stage, authority_key, status, revision, canonical_hash, payload
        ) VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        ON CONFLICT (stage, authority_key) DO UPDATE SET
            status = EXCLUDED.status,
            revision = EXCLUDED.revision,
            canonical_hash = EXCLUDED.canonical_hash,
            payload = EXCLUDED.payload
        """,
        stage,
        key,
        status,
        revision,
        canonical_hash,
        json.dumps(payload or {}),
    )


async def _lock_head(
    connection: object, stage: str, key: str
) -> asyncpg.Record:
    row = await cast(asyncpg.Connection, connection).fetchrow(
        """
        SELECT status, revision, canonical_hash, payload
        FROM policy_test_authority_head
        WHERE stage = $1 AND authority_key = $2
        FOR UPDATE
        """,
        stage,
        key,
    )
    if row is None:
        raise ActionAuthorizationError(f"missing current {stage} authority head")
    return row


def _payload(row: asyncpg.Record) -> dict[str, object]:
    value = row["payload"]
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise ActionAuthorizationError("authority payload must be an object")
    return cast(dict[str, object], decoded)


class _PgPrincipalAdapter:
    async def lock_current(
        self, connection: object, receipt: PolicyReceiptV2, request: object
    ) -> str:
        del request
        row = await _lock_head(connection, "principal", receipt.actor_id)
        if row["status"] != "active":
            raise ActionAuthorizationError("principal is not active")
        return str(_payload(row).get("actor_id", receipt.actor_id))


class _PgActionAdapter:
    async def lock_current(
        self, connection: object, receipt: PolicyReceiptV2, request: object
    ) -> object:
        del request
        fence = receipt.action_resource_fence
        row = await _lock_head(connection, "action", fence.action_resource_id)
        if (
            row["status"] != "active"
            or row["revision"] != fence.action_revision
            or row["canonical_hash"] != fence.canonical_hash
        ):
            raise ActionAuthorizationError("current action head mismatch")
        return fence


class _PgConsentAdapter:
    async def lock_current(
        self, connection: object, receipt: PolicyReceiptV2, request: ActionExecutionRequest
    ) -> tuple[object, ...]:
        current = {
            item.snapshot_id: item for item in request.context.consent_evidence
        }
        result: list[object] = []
        for snapshot_id in sorted(receipt.consent_snapshot_ids):
            row = await _lock_head(connection, "consent", snapshot_id)
            item = current.get(snapshot_id)
            if item is None:
                raise ActionAuthorizationError("consent snapshot is absent")
            result.append(
                replace(
                    item,
                    status=row["status"],
                    version=row["revision"],
                    canonical_hash=row["canonical_hash"],
                )
            )
        return tuple(result)


class _PgBindingAdapter:
    async def lock_current(
        self, connection: object, receipt: PolicyReceiptV2, request: ActionExecutionRequest
    ) -> object:
        binding = request.context.binding_evidence
        if binding is None:
            raise ActionAuthorizationError("binding evidence is absent")
        row = await _lock_head(connection, "binding", receipt.binding_id)
        return replace(
            binding,
            status=row["status"],
            version=row["revision"],
            canonical_hash=row["canonical_hash"],
        )


class _PgMembershipAdapter:
    async def lock_current(
        self, connection: object, receipt: PolicyReceiptV2, request: ActionExecutionRequest
    ) -> object:
        current = request.context.membership_evidence
        if current is None:
            raise ActionAuthorizationError("membership evidence is absent")
        snapshot_id = receipt.action_resource_fence.membership_snapshot_id
        if snapshot_id is None:
            raise ActionAuthorizationError("membership fence is absent")
        row = await _lock_head(connection, "membership", snapshot_id)
        data = _payload(row)
        return replace(
            current,
            status=row["status"],
            revision=row["revision"],
            canonical_hash=row["canonical_hash"],
            family_space_id=str(data.get("family_space_id", current.family_space_id)),
            family_owner_subject_id=str(
                data.get("family_owner_subject_id", current.family_owner_subject_id)
            ),
        )


class _PgProposalAdapter:
    async def lock_current(
        self, connection: object, receipt: PolicyReceiptV2, request: ActionExecutionRequest
    ) -> object:
        current = request.context.proposal_evidence
        proposal_id = receipt.action_resource_fence.proposal_id
        if current is None or proposal_id is None:
            raise ActionAuthorizationError("proposal evidence is absent")
        row = await _lock_head(connection, "proposal", proposal_id)
        data = _payload(row)
        return replace(
            current,
            status=row["status"],
            revision=row["revision"],
            canonical_hash=row["canonical_hash"],
            family_space_id=str(data.get("family_space_id", current.family_space_id)),
        )


class _PgApprovalAdapter:
    async def lock_current(
        self, connection: object, receipt: PolicyReceiptV2, request: ActionExecutionRequest
    ) -> tuple[object, ...]:
        current = {
            item.snapshot_id: item for item in request.context.approval_evidence
        }
        result: list[object] = []
        for fence in receipt.action_resource_fence.approval_snapshots:
            row = await _lock_head(connection, "approval", fence.snapshot_id)
            item = current.get(fence.snapshot_id)
            if item is None:
                raise ActionAuthorizationError("approval snapshot is absent")
            data = _payload(row)
            result.append(
                replace(
                    item,
                    status=row["status"],
                    revision=row["revision"],
                    canonical_hash=row["canonical_hash"],
                    subject_id=str(data.get("subject_id", item.subject_id)),
                )
            )
        return tuple(result)


class _PgCaptureAdapter:
    async def lock_current(
        self, connection: object, receipt: PolicyReceiptV2, request: ActionExecutionRequest
    ) -> tuple[object, ...]:
        current = {
            item.evidence_id: item for item in request.context.capture_evidence
        }
        result: list[object] = []
        for evidence_id in receipt.action_resource_fence.capture_evidence_ids:
            row = await _lock_head(connection, "capture", evidence_id)
            item = current.get(evidence_id)
            if item is None:
                raise ActionAuthorizationError("capture evidence is absent")
            result.append(
                replace(
                    item,
                    status=row["status"],
                    revision=row["revision"],
                    canonical_hash=row["canonical_hash"],
                )
            )
        return tuple(result)


class _PgProfileAdapter:
    async def lock_current(
        self, connection: object, receipts: tuple[PolicyReceiptV2, ...]
    ) -> None:
        for profile_id in sorted({item.runtime_profile_id for item in receipts}):
            row = await _lock_head(connection, "profile", profile_id)
            if row["status"] != "active":
                raise ActionAuthorizationError("profile authority is not active")


def _authorizer(**adapters: object) -> PostgresActionAuthorizer:
    return PostgresActionAuthorizer(
        authority=CompositeActionAuthority(
            principal=cast(Any, adapters.get("principal", _PgPrincipalAdapter())),
            consent=cast(Any, adapters.get("consent")),
            membership=cast(Any, adapters.get("membership")),
            binding=cast(Any, adapters.get("binding")),
            proposal=cast(Any, adapters.get("proposal")),
            action=cast(Any, adapters.get("action", _PgActionAdapter())),
            approvals=cast(Any, adapters.get("approvals")),
            capture=cast(Any, adapters.get("capture")),
        )
    )


async def _seed_common_heads(
    connection: asyncpg.Connection, receipts: tuple[PolicyReceiptV2, ...]
) -> None:
    for actor_id in sorted({item.actor_id for item in receipts}):
        await _put_head(
            connection,
            "principal",
            actor_id,
            payload={"actor_id": actor_id},
        )
    for receipt in receipts:
        fence = receipt.action_resource_fence
        await _put_head(
            connection,
            "action",
            fence.action_resource_id,
            revision=fence.action_revision,
            canonical_hash=fence.canonical_hash,
        )


def _chat_batch() -> tuple[
    tuple[PolicyReceiptV2, ...], tuple[ActionExecutionRequest, ...]
]:
    contexts = (
        make_context(evaluated_at=NOW, session_id="session-chat"),
        make_context(
            capability="english_practice",
            evaluated_at=NOW,
            session_id="session-english",
        ),
    )
    receipts: list[PolicyReceiptV2] = []
    requests: list[ActionExecutionRequest] = []
    for index, context in enumerate(contexts, start=1):
        engine = PolicyEngine(receipt_id_factory=lambda index=index: f"receipt-batch-{index}")
        receipt = engine.receipt_for(context, engine.decide(context))
        receipts.append(receipt)
        requests.append(ActionExecutionRequest(receipt.receipt_id, context, NOW))
    return tuple(receipts), tuple(requests)


@pytest.mark.asyncio
async def test_live_pg_session_batch_same_connection_and_all_or_nothing(
    composite_pg: _PgEnvironment,
) -> None:
    receipts, requests = _chat_batch()
    admin = await asyncpg.connect(composite_pg.admin_dsn)
    try:
        await _seed_common_heads(admin, receipts)
        await _put_head(admin, "profile", "profile-1")
    finally:
        await admin.close()

    batch = SessionPolicyBatchAuthorizer(
        repository=ConnectionBoundPolicyReceiptRepository(),
        action_authorizer=_authorizer(),
        profile_authority=_PgProfileAdapter(),
    )
    projector = await asyncpg.connect(composite_pg.projector_dsn)
    try:
        callback_pid = 0

        async def failing_callback(
            connection: object, locked: tuple[PolicyReceiptV2, ...]
        ) -> None:
            del locked
            pid = await cast(asyncpg.Connection, connection).fetchval(
                "SELECT pg_backend_pid()"
            )
            await cast(asyncpg.Connection, connection).execute(
                "INSERT INTO policy_test_business_write VALUES ('rolled-back', $1)",
                pid,
            )
            raise RuntimeError("profile persistence failed")

        with pytest.raises(RuntimeError, match="profile persistence failed"):
            async with projector.transaction():
                await batch.execute_profile_persist(
                    projector,
                    receipts=receipts,
                    requests=requests,
                    write_callback=failing_callback,
                )

        async def callback(
            connection: object, locked: tuple[PolicyReceiptV2, ...]
        ) -> str:
            nonlocal callback_pid
            assert locked == receipts
            callback_pid = await cast(asyncpg.Connection, connection).fetchval(
                "SELECT pg_backend_pid()"
            )
            await cast(asyncpg.Connection, connection).execute(
                "INSERT INTO policy_test_business_write VALUES ('committed', $1)",
                callback_pid,
            )
            return "profile-written"

        async with projector.transaction():
            outer_pid = await projector.fetchval("SELECT pg_backend_pid()")
            result = await batch.execute_profile_persist(
                projector,
                receipts=receipts,
                requests=requests,
                write_callback=callback,
            )
        assert result == "profile-written"
        assert callback_pid == outer_pid

        changed = receipts[1].model_dump(mode="json")
        changed["reason_code"] = "immutable-conflict"
        conflicting = PolicyReceiptV2.model_validate(changed)
        with pytest.raises(PolicyReceiptConflictError):
            async with projector.transaction():
                await batch.execute_profile_persist(
                    projector,
                    receipts=(receipts[0], conflicting),
                    requests=requests,
                    write_callback=callback,
                )

        new_payload = receipts[0].model_dump(mode="json")
        new_payload["receipt_id"] = "receipt-batch-0-savepoint-new"
        new_receipt = PolicyReceiptV2.model_validate(new_payload)
        async with projector.transaction():
            try:
                await ConnectionBoundPolicyReceiptRepository().insert_many(
                    projector, (new_receipt, conflicting)
                )
            except PolicyReceiptConflictError:
                pass
            else:  # pragma: no cover - explicit red/green assertion
                pytest.fail("immutable batch conflict was not raised")
            await projector.execute(
                "INSERT INTO policy_test_business_write VALUES "
                "('outer-transaction-survived', pg_backend_pid())"
            )
    finally:
        await projector.close()

    admin = await asyncpg.connect(composite_pg.admin_dsn)
    try:
        assert await admin.fetchval("SELECT count(*) FROM policy_receipts_v2") == 2
        assert await admin.fetchval("SELECT count(*) FROM policy_test_business_write") == 2
        assert await admin.fetchval(
            "SELECT count(*) FROM policy_receipts_v2 "
            "WHERE receipt_id = 'receipt-batch-0-savepoint-new'"
        ) == 0
    finally:
        await admin.close()


def _voice_case() -> tuple[object, PolicyReceiptV2]:
    consent = make_consent(
        snapshot_id="voice-consent-1",
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    binding = make_binding(now=NOW)
    fence = build_action_resource_fence(
        capability="voice_clone_use",
        purpose="voice_clone",
        action_resource_id="voice-action-1",
        action_revision=1,
        consent_snapshot_id=consent.snapshot_id,
        consent_snapshot_revision=consent.version,
        consent_snapshot_hash=consent.canonical_hash,
        generation_id=0,
        turn_id=0,
        tool_epoch=0,
        issued_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        data_classification="biometric",
        consent_evidence=(consent,),
        binding_evidence=binding,
        action_resource_fence=fence,
        evaluated_at=NOW,
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-linearized")
    return context, engine.receipt_for(context, engine.decide(context))


async def _seed_voice_heads(
    connection: asyncpg.Connection, context: Any, receipt: PolicyReceiptV2
) -> None:
    await _seed_common_heads(connection, (receipt,))
    consent = context.consent_evidence[0]
    binding = context.binding_evidence
    await _put_head(
        connection,
        "consent",
        consent.snapshot_id,
        status=consent.status,
        revision=consent.version,
        canonical_hash=consent.canonical_hash,
    )
    await _put_head(
        connection,
        "binding",
        receipt.binding_id,
        status=binding.status,
        revision=binding.version,
        canonical_hash=binding.canonical_hash,
    )


@pytest.mark.asyncio
async def test_live_pg_authority_linearizes_revoke_and_callback(
    composite_pg: _PgEnvironment,
) -> None:
    context, receipt = _voice_case()
    admin = await asyncpg.connect(composite_pg.admin_dsn)
    try:
        await _seed_voice_heads(admin, context, receipt)
    finally:
        await admin.close()
    projector = await asyncpg.connect(composite_pg.projector_dsn)
    async with projector.transaction():
        await ConnectionBoundPolicyReceiptRepository().insert_many(projector, (receipt,))
    await projector.close()

    authorizer = _authorizer(
        consent=_PgConsentAdapter(),
        binding=_PgBindingAdapter(),
    )
    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()

    async def consume_first() -> str:
        connection = await asyncpg.connect(composite_pg.projector_dsn)
        try:
            async with connection.transaction():
                async def callback(active: object, locked: PolicyReceiptV2) -> str:
                    del locked
                    pid = await cast(asyncpg.Connection, active).fetchval(
                        "SELECT pg_backend_pid()"
                    )
                    await cast(asyncpg.Connection, active).execute(
                        "INSERT INTO policy_test_business_write VALUES ('linearized', $1)",
                        pid,
                    )
                    callback_entered.set()
                    await asyncio.wait_for(release_callback.wait(), timeout=5)
                    return "written"

                return await authorizer.execute_authorized(
                    connection,
                    ActionExecutionRequest(receipt.receipt_id, context, NOW),
                    callback,
                )
        finally:
            await connection.close()

    revoker_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def revoke() -> None:
        connection = await asyncpg.connect(composite_pg.admin_dsn)
        try:
            revoker_pid.set_result(await connection.fetchval("SELECT pg_backend_pid()"))
            await connection.execute(
                "UPDATE policy_test_authority_head SET status = 'revoked' "
                "WHERE stage = 'consent' AND authority_key = 'voice-consent-1'"
            )
        finally:
            await connection.close()

    consumer_task = asyncio.create_task(consume_first())
    revoke_task: asyncio.Task[None] | None = None
    callback_wait: asyncio.Task[bool] | None = None
    try:
        callback_wait = asyncio.create_task(callback_entered.wait())
        done, _pending = await asyncio.wait(
            {consumer_task, callback_wait},
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if consumer_task in done:
            consumer_task.result()
            pytest.fail("consumer returned before entering the guarded callback")
        if callback_wait not in done:
            pytest.fail("consumer did not enter callback before timeout")
        await callback_wait

        revoke_task = asyncio.create_task(revoke())
        done, _pending = await asyncio.wait(
            {revoke_task, revoker_pid},
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if revoke_task in done and not revoker_pid.done():
            revoke_task.result()
            pytest.fail("revoker exited before publishing its backend pid")
        if not revoker_pid.done():
            pytest.fail("revoker did not publish backend pid before timeout")
        pid = revoker_pid.result()

        observer = await asyncpg.connect(composite_pg.admin_dsn)
        try:
            for _ in range(100):
                if revoke_task.done():
                    revoke_task.result()
                    pytest.fail("revoker completed before lock linearization")
                wait_type = await asyncio.wait_for(
                    observer.fetchval(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = $1",
                        pid,
                    ),
                    timeout=2,
                )
                if wait_type == "Lock":
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("revoker never blocked on the consumer authority-row lock")
        finally:
            await observer.close()
        assert not revoke_task.done()
        release_callback.set()
        assert await asyncio.wait_for(consumer_task, timeout=5) == "written"
        await asyncio.wait_for(revoke_task, timeout=5)
    finally:
        release_callback.set()
        tasks = [consumer_task]
        if revoke_task is not None:
            tasks.append(revoke_task)
        if callback_wait is not None:
            tasks.append(callback_wait)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    callback_count = 0

    async def forbidden_callback(*_args: object) -> None:
        nonlocal callback_count
        callback_count += 1

    connection = await asyncpg.connect(composite_pg.projector_dsn)
    try:
        with pytest.raises(ActionAuthorizationError):
            async with connection.transaction():
                await authorizer.execute_authorized(
                    connection,
                    ActionExecutionRequest(receipt.receipt_id, context, NOW),
                    forbidden_callback,
                )
    finally:
        await connection.close()
    assert callback_count == 0

    admin = await asyncpg.connect(composite_pg.admin_dsn)
    try:
        assert await admin.fetchval("SELECT count(*) FROM policy_test_business_write") == 1
    finally:
        await admin.close()


def _family_case() -> tuple[object, PolicyReceiptV2]:
    approvals = (
        FakeApprovalEvidence("approval-a", 1, "a" * 64, "subject-a"),
        FakeApprovalEvidence("approval-b", 2, "b" * 64, "subject-b"),
    )
    consent = make_consent(
        snapshot_id="family-consent",
        version=4,
        canonical_hash="c" * 64,
        subject_id="subject-a",
        resource_owner_id="owner-1",
        actor_id="subject-a",
        capability="family_shared_memory_promotion",
        purpose="family_shared_memory_promotion",
        now=NOW,
    )
    membership = FakeMembershipEvidence(
        snapshot_id="family-membership", revision=5, canonical_hash="d" * 64
    )
    proposal = FakeProposalEvidence()
    fence = build_action_resource_fence(
        capability="family_shared_memory_promotion",
        purpose="family_shared_memory_promotion",
        action_resource_id="proposal-1",
        action_revision=1,
        family_space_id="family-1",
        family_owner_subject_id="owner-1",
        proposal_id="proposal-1",
        proposal_revision=1,
        required_approval_subject_ids=("subject-a", "subject-b"),
        approval_snapshots=tuple(
            build_approval_snapshot_fence(
                subject_id=item.subject_id,
                snapshot_id=item.snapshot_id,
                revision=item.revision,
                canonical_hash=item.canonical_hash,
            )
            for item in approvals
        ),
        consent_snapshot_id=consent.snapshot_id,
        consent_snapshot_revision=consent.version,
        consent_snapshot_hash=consent.canonical_hash,
        membership_snapshot_id=membership.snapshot_id,
        membership_snapshot_revision=membership.revision,
        membership_snapshot_hash=membership.canonical_hash,
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        issued_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
    )
    context = make_context(
        actor_id="subject-a",
        subject_id="subject-a",
        resource_owner_id="owner-1",
        capability="family_shared_memory_promotion",
        purpose="family_shared_memory_promotion",
        current_session_mode="family_shared",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        membership_evidence=membership,
        proposal_evidence=proposal,
        approval_evidence=approvals,
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        action_resource_fence=fence,
        evaluated_at=NOW,
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-family-promotion")
    return context, engine.receipt_for(context, engine.decide(context))


@pytest.mark.asyncio
async def test_live_pg_family_old_receipt_rejects_every_changed_current_head(
    composite_pg: _PgEnvironment,
) -> None:
    context, receipt = _family_case()
    admin = await asyncpg.connect(composite_pg.admin_dsn)
    try:
        await _seed_voice_heads(admin, context, receipt)
        membership = context.membership_evidence
        proposal = context.proposal_evidence
        await _put_head(
            admin,
            "membership",
            membership.snapshot_id,
            revision=membership.revision,
            canonical_hash=membership.canonical_hash,
            payload={
                "family_space_id": membership.family_space_id,
                "family_owner_subject_id": membership.family_owner_subject_id,
            },
        )
        await _put_head(
            admin,
            "proposal",
            proposal.proposal_id,
            status=proposal.status,
            revision=proposal.revision,
            canonical_hash=proposal.canonical_hash,
            payload={"family_space_id": proposal.family_space_id},
        )
        for approval in context.approval_evidence:
            await _put_head(
                admin,
                "approval",
                approval.snapshot_id,
                revision=approval.revision,
                canonical_hash=approval.canonical_hash,
                payload={"subject_id": approval.subject_id},
            )
    finally:
        await admin.close()
    connection = await asyncpg.connect(composite_pg.projector_dsn)
    async with connection.transaction():
        await ConnectionBoundPolicyReceiptRepository().insert_many(connection, (receipt,))
    await connection.close()

    authorizer = _authorizer(
        consent=_PgConsentAdapter(),
        membership=_PgMembershipAdapter(),
        binding=_PgBindingAdapter(),
        proposal=_PgProposalAdapter(),
        approvals=_PgApprovalAdapter(),
    )

    async def consume() -> None:
        connection = await asyncpg.connect(composite_pg.projector_dsn)
        try:
            async with connection.transaction():
                await authorizer.execute_authorized(
                    connection,
                    ActionExecutionRequest(receipt.receipt_id, context, NOW),
                    lambda *_args: None,
                )
        finally:
            await connection.close()

    await consume()
    mutations = (
        ("principal", "subject-a", "payload = '{\"actor_id\":\"other\"}'::jsonb"),
        ("action", "proposal-1", "revision = 2"),
        (
            "membership",
            "family-membership",
            "payload = '{\"family_space_id\":\"other-family\"}'::jsonb",
        ),
        ("proposal", "proposal-1", "revision = 2"),
        (
            "approval",
            "approval-a",
            "payload = '{\"subject_id\":\"subject-b\"}'::jsonb",
        ),
    )
    for stage, key, assignment in mutations:
        admin = await asyncpg.connect(composite_pg.admin_dsn)
        try:
            await admin.execute(
                f"UPDATE policy_test_authority_head SET {assignment} "
                "WHERE stage = $1 AND authority_key = $2",
                stage,
                key,
            )
        finally:
            await admin.close()
        with pytest.raises(ActionAuthorizationError):
            await consume()
        admin = await asyncpg.connect(composite_pg.admin_dsn)
        try:
            await admin.execute("DELETE FROM policy_test_authority_head")
            await _seed_voice_heads(admin, context, receipt)
            membership = context.membership_evidence
            proposal = context.proposal_evidence
            await _put_head(
                admin,
                "membership",
                membership.snapshot_id,
                revision=membership.revision,
                canonical_hash=membership.canonical_hash,
                payload={
                    "family_space_id": membership.family_space_id,
                    "family_owner_subject_id": membership.family_owner_subject_id,
                },
            )
            await _put_head(
                admin,
                "proposal",
                proposal.proposal_id,
                status=proposal.status,
                revision=proposal.revision,
                canonical_hash=proposal.canonical_hash,
                payload={"family_space_id": proposal.family_space_id},
            )
            for approval in context.approval_evidence:
                await _put_head(
                    admin,
                    "approval",
                    approval.snapshot_id,
                    revision=approval.revision,
                    canonical_hash=approval.canonical_hash,
                    payload={"subject_id": approval.subject_id},
                )
        finally:
            await admin.close()


def _proposal_or_approval_case(capability: str) -> tuple[object, PolicyReceiptV2]:
    consent = make_consent(
        snapshot_id=f"{capability}-consent",
        version=4,
        canonical_hash="c" * 64,
        subject_id="subject-a",
        resource_owner_id="owner-1",
        actor_id="subject-a",
        capability=capability,  # type: ignore[arg-type]
        purpose=capability,
        now=NOW,
    )
    membership = FakeMembershipEvidence(
        snapshot_id=f"{capability}-membership",
        revision=5,
        canonical_hash="d" * 64,
    )
    capture = FakeCaptureEvidence(
        evidence_id=f"{capability}-capture",
        subject_id="subject-a",
    )
    proposal = FakeProposalEvidence(proposal_id=f"{capability}-proposal")
    fence_values: dict[str, object] = {
        "capability": capability,
        "purpose": capability,
        "action_resource_id": proposal.proposal_id,
        "action_revision": 1,
        "family_space_id": "family-1",
        "family_owner_subject_id": "owner-1",
        "proposal_id": proposal.proposal_id,
        "proposal_revision": proposal.revision,
        "required_approval_subject_ids": ("subject-a", "subject-b"),
        "consent_snapshot_id": consent.snapshot_id,
        "consent_snapshot_revision": consent.version,
        "consent_snapshot_hash": consent.canonical_hash,
        "membership_snapshot_id": membership.snapshot_id,
        "membership_snapshot_revision": membership.revision,
        "membership_snapshot_hash": membership.canonical_hash,
        "generation_id": 7,
        "turn_id": 8,
        "tool_epoch": 9,
        "issued_at": NOW,
        "valid_until": NOW + timedelta(minutes=5),
    }
    if capability == "family_shared_memory_proposal":
        fence_values["capture_evidence_records"] = ((
            capture.evidence_id,
            capture.revision,
            capture.canonical_hash,
        ),)
    else:
        fence_values.update(
            {
                "voter_subject_id": "subject-a",
                "approval_decision": "confirm",
            }
        )
    fence = build_action_resource_fence(**fence_values)  # type: ignore[arg-type]
    context = make_context(
        actor_id="subject-a",
        subject_id="subject-a",
        resource_owner_id="owner-1",
        capability=capability,  # type: ignore[arg-type]
        purpose=capability,
        current_session_mode="family_shared",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        membership_evidence=membership,
        proposal_evidence=(
            None if capability == "family_shared_memory_proposal" else proposal
        ),
        capture_evidence=(
            (capture,) if capability == "family_shared_memory_proposal" else ()
        ),
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        action_resource_fence=fence,
        evaluated_at=NOW,
    )
    engine = PolicyEngine(receipt_id_factory=lambda: f"receipt-{capability}")
    return context, engine.receipt_for(context, engine.decide(context))


@pytest.mark.parametrize(
    "capability",
    ["family_shared_memory_proposal", "family_shared_memory_approval"],
)
@pytest.mark.asyncio
async def test_live_pg_proposal_and_approval_old_fences_reject_current_head_change(
    composite_pg: _PgEnvironment,
    capability: str,
) -> None:
    context, receipt = _proposal_or_approval_case(capability)
    admin = await asyncpg.connect(composite_pg.admin_dsn)
    try:
        await _seed_voice_heads(admin, context, receipt)
        membership = context.membership_evidence
        await _put_head(
            admin,
            "membership",
            membership.snapshot_id,
            revision=membership.revision,
            canonical_hash=membership.canonical_hash,
            payload={
                "family_space_id": membership.family_space_id,
                "family_owner_subject_id": membership.family_owner_subject_id,
            },
        )
        proposal = context.proposal_evidence
        if proposal is not None:
            await _put_head(
                admin,
                "proposal",
                proposal.proposal_id,
                status=proposal.status,
                revision=proposal.revision,
                canonical_hash=proposal.canonical_hash,
                payload={"family_space_id": proposal.family_space_id},
            )
        for capture in context.capture_evidence:
            await _put_head(
                admin,
                "capture",
                capture.evidence_id,
                status=capture.status,
                revision=capture.revision,
                canonical_hash=capture.canonical_hash,
            )
    finally:
        await admin.close()

    connection = await asyncpg.connect(composite_pg.projector_dsn)
    try:
        async with connection.transaction():
            await ConnectionBoundPolicyReceiptRepository().insert_many(
                connection, (receipt,)
            )
    finally:
        await connection.close()
    authorizer = _authorizer(
        consent=_PgConsentAdapter(),
        membership=_PgMembershipAdapter(),
        binding=_PgBindingAdapter(),
        proposal=_PgProposalAdapter(),
        capture=(
            _PgCaptureAdapter()
            if capability == "family_shared_memory_proposal"
            else None
        ),
    )

    async def consume() -> None:
        connection = await asyncpg.connect(composite_pg.projector_dsn)
        try:
            async with connection.transaction():
                await authorizer.execute_authorized(
                    connection,
                    ActionExecutionRequest(receipt.receipt_id, context, NOW),
                    lambda *_args: None,
                )
        finally:
            await connection.close()

    await consume()
    admin = await asyncpg.connect(composite_pg.admin_dsn)
    try:
        if capability == "family_shared_memory_proposal":
            await admin.execute(
                "UPDATE policy_test_authority_head SET revision = revision + 1 "
                "WHERE stage = 'capture'"
            )
        else:
            await admin.execute(
                "UPDATE policy_test_authority_head SET revision = revision + 1 "
                "WHERE stage = 'proposal'"
            )
    finally:
        await admin.close()
    with pytest.raises(ActionAuthorizationError):
        await consume()
