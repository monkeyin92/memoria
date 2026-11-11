"""Real planner client, HTTP routing and isolated PostgreSQL catalog."""

# ruff: noqa: F811
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import asyncpg
import httpx
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.reply_plan import ReplyPlanMixin
from services.agent.src.response_plan_cache import ResponsePlanCache
from services.agent.src.response_planner_client import (
    ResponsePlannerClient,
    ResponsePlannerClientConfig,
)
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import MemoryClaimReview, MemorySearchQuery
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.archive.tests.test_atomic_claim_unseen_evaluation import AtomicToolExtractor
from services.control_api.app.main import create_app
from services.control_api.tests.policy_chain_test_support import _RESPONSE_PLAN_TOKEN, _configure
from services.control_api.tests.test_policy_chain_subject_matrix import _open_subject_session
from services.session_runtime.commands import SwitchPersistentSubjectCommand
from services.session_runtime.service import build_postgres_session_runtime_service
from services.session_runtime.tests.postgres_store_test_support import (
    _seed_self_consent,
    _seed_verified_device,
    postgres_runtime,  # noqa: F401
    postgres_runtime_with_consent,  # noqa: F401
)
from services.session_runtime.tests.test_postgres_action_authority import _revoke_self_consent
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


async def test_catalog_budget_subject_switch_and_reply_cache(
    postgres_runtime_with_consent, monkeypatch, tmp_path
):
    store, dsn = postgres_runtime_with_consent
    _configure(monkeypatch, tmp_path)
    app = create_app()
    service = build_postgres_session_runtime_service(
        store=store, signing_key=app.state.settings.runtime_profile_signing_key()
    )
    app.state.session_runtime_service = service
    archive = PostgresLifeArchive(dsn)
    catalog = PostgresMemoryCatalog(
        dsn, extractor=AtomicToolExtractor(), evidence_subject_category_resolver=lambda e: "adult"
    )
    await archive.initialize()
    await catalog.initialize()
    app.state.memory_catalog = catalog
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://control"
        ) as http:
            session = await _open_subject_session(
                http,
                app,
                bootstrap_dsn=dsn,
                device_id="r3-device",
                binding_id="r3-binding",
                subject_id="r3-child",
                declared_mode="parent_for_child",
                subject_category="minor",
                age_band="under_14",
                switch_to_subject=False,
            )
            admin = await asyncpg.connect(dsn)
            try:
                await admin.execute(
                    """INSERT INTO identity_relationships (
                    relationship_id, source_person_id, target_person_id, relation_type,
                    status, valid_from, established_evidence_id, confirmed_by_source_at,
                    confirmed_by_target_at, requires_confirmation, can_delegate, created_at, updated_at
                ) VALUES ('r3-guardian', $1, 'r3-child', 'guardian_of', 'active', $2,
                    'synthetic-r3-confirmed', $2, $2, true, false, $2, $2)""",
                    session.account_id,
                    datetime.now(UTC),
                )
            finally:
                await admin.close()
            child_profile = await service.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=session.session_id,
                    actor_id=session.account_id,
                    subject_id="r3-child",
                    now=datetime.now(UTC),
                    requested_capabilities=("chat",),
                )
            )
            assert child_profile.active_subject_id == "r3-child"
            for i in range(12):
                await archive.record(
                    EvidenceEvent(
                        event_id=f"r3-claim-{i}",
                        account_id=session.account_id,
                        subject_id=session.account_id,
                        event_type="speech.utterance_finalized",
                        occurred_at=datetime.now(UTC),
                        speaker_class="owner",
                        source="synthetic-r3",
                        payload={
                            "text": f"I use Qdrant{i}.",
                            "interaction_mode": "companion",
                            "owner_projection_eligible": True,
                            "prompt_kind": "spontaneous",
                        },
                    )
                )
            assert (await catalog.compile_pending()).compiled_events == 12
            for claim in await catalog.review_queue(account_id=session.account_id):
                await catalog.review(
                    MemoryClaimReview(
                        account_id=session.account_id, claim_id=claim.item_id, action="confirm"
                    )
                )
            assert (
                len(
                    (
                        await catalog.context(
                            MemorySearchQuery(
                                account_id=session.account_id,
                                subject_id=session.account_id,
                                speaker_class="owner",
                                text="Qdrant",
                                limit=8,
                            )
                        )
                    ).items
                )
                == 8
            )
            client = ResponsePlannerClient(
                ResponsePlannerClientConfig(
                    endpoint="http://control/v1/interaction/response-plan",
                    internal_token=_RESPONSE_PLAN_TOKEN,
                ),
                client=http,
            )
            speaker = SpeakerDecision(
                classification="owner",
                score=0.9,
                quality_score=0.9,
                reason_code="trusted",
                model_version="campplus-test-v1",
                profile_id="speaker-profile-1",
                template_version=1,
                permissions=permissions_for_speaker("owner"),
            )

            async def fetch(turn, query="Qdrant"):
                return await client.fetch(
                    session_id=session.session_id,
                    query=query,
                    fence=GenerationFence(session.session_id, turn, turn, 0),
                    speaker_decision=speaker,
                )

            child = await fetch(1)
            assert child.available, child.reason
            assert child.plan.grounded_items == ()
            profile = await service.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=session.session_id,
                    actor_id=session.account_id,
                    subject_id=session.account_id,
                    now=datetime.now(UTC),
                    requested_capabilities=("chat",),
                )
            )
            owner = await fetch(2)
            assert owner.available, owner.reason
            assert len(owner.plan.grounded_items) == 4
            assert (
                len({s for item in owner.plan.grounded_items for s in item.source_event_ids}) == 4
            )
            original_context = catalog.context

            async def delayed_context(query):
                await asyncio.sleep(0.5)
                return await original_context(query)

            monkeypatch.setattr(catalog, "context", delayed_context)
            fallback = await fetch(4)
            assert fallback.available and not fallback.plan.grounded_items
            monkeypatch.setattr(catalog, "context", original_context)
            assert len((await fetch(5)).plan.grounded_items) == 4
            consumer = ReplyPlanMixin()
            consumer._response_plans = ResponsePlanCache()
            holder = SimpleNamespace(profile=profile)
            consumer._runtime = SimpleNamespace(
                orchestrator=SimpleNamespace(runtime_profiles=SimpleNamespace(current=holder))
            )
            consumer.cache_response_plan(owner.plan)
            assert consumer.response_plan_authorized(owner.plan)
            holder.profile = await service.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=session.session_id,
                    actor_id=session.account_id,
                    subject_id="r3-child",
                    now=datetime.now(UTC),
                    requested_capabilities=("chat",),
                )
            )
            assert not consumer.response_plan_authorized(owner.plan)
            again = await fetch(3)
            assert again.available and not again.plan.grounded_items
            session = await _open_subject_session(
                http,
                app,
                bootstrap_dsn=dsn,
                device_id="r3-private-device",
                binding_id="r3-private-binding",
                subject_id="r3-private-other",
                declared_mode="self_use",
                subject_category="adult",
                age_band="adult",
                switch_to_account=True,
            )
            await archive.record(
                EvidenceEvent(
                    event_id="r3-private-fact",
                    account_id=session.account_id,
                    subject_id=session.account_id,
                    event_type="speech.utterance_finalized",
                    occurred_at=datetime.now(UTC),
                    speaker_class="owner",
                    source="synthetic-r3",
                    payload={
                        "text": "I use Milvus.",
                        "interaction_mode": "companion",
                        "owner_projection_eligible": True,
                        "prompt_kind": "spontaneous",
                    },
                )
            )
            assert (await catalog.compile_pending()).compiled_events == 13
            for claim in await catalog.review_queue(account_id=session.account_id):
                await catalog.review(
                    MemoryClaimReview(
                        account_id=session.account_id, claim_id=claim.item_id, action="confirm"
                    )
                )
            admin = await asyncpg.connect(dsn)
            try:
                await _seed_verified_device(
                    admin,
                    device_id="r3-private-device",
                    binding_id="r3-private-binding",
                    now=datetime.now(UTC),
                )
                evidence, consent_snapshot = await _seed_self_consent(
                    admin,
                    actor_id=session.account_id,
                    device_id="r3-private-device",
                    binding_id="r3-private-binding",
                    capability="memory_recall_private",
                    purpose="memory_recall",
                    now=datetime.now(UTC),
                )
            finally:
                await admin.close()
            await service.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=session.session_id,
                    actor_id=session.account_id,
                    subject_id=session.account_id,
                    now=datetime.now(UTC),
                    requested_capabilities=("chat",),
                )
            )
            private_profile = await service.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=session.session_id,
                    actor_id=session.account_id,
                    subject_id=session.account_id,
                    now=datetime.now(UTC),
                    requested_capabilities=("chat", "memory_recall_private"),
                )
            )
            assert "memory_recall_private" in private_profile.capabilities
            private_plan = await fetch(6, "Milvus")
            assert private_plan.available and len(private_plan.plan.grounded_items) == 1
            assert not (await fetch(8)).plan.grounded_items
            holder.profile = private_profile
            consumer.cache_response_plan(private_plan.plan)
            assert consumer.response_plan_authorized(private_plan.plan)
            admin = await asyncpg.connect(dsn)
            try:
                await _revoke_self_consent(
                    admin, evidence=evidence, snapshot=consent_snapshot, now=datetime.now(UTC)
                )
            finally:
                await admin.close()
            rejected = await fetch(7, "Milvus")
            assert rejected.available and rejected.reason == "ok"
            assert rejected.plan.grounded_items == ()
            assert rejected.plan.provenance.source_refs == ()
            holder.profile = None
            assert not consumer.response_plan_authorized(private_plan.plan)
    finally:
        await catalog.close()
        await archive.close()
