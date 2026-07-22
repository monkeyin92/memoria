"""SQLite contract tests for the evidence-backed self model."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.self_model.domain import (
    InvalidSelfModelTransitionError,
    SelfModelIdempotencyConflictError,
    SelfModelNotFoundError,
    SelfModelVersionConflictError,
    SourceInput,
    UntrustedSelfModelSourceError,
)
from services.self_model.policy import activation_decision
from services.self_model.registry import SelfModelRegistry


async def _evidence(
    path: Path,
    *,
    account_id: str,
    event_id: str,
    speaker_class: str = "owner",
    simulated: bool = False,
) -> None:
    await LifeArchive.sqlite(path).record(
        EvidenceEvent(
            event_id=event_id,
            account_id=account_id,
            event_type=(
                "assistant.actual_heard"
                if speaker_class == "assistant"
                else "speech.utterance_finalized"
            ),
            occurred_at=datetime.now(UTC),
            speaker_class=speaker_class,  # type: ignore[arg-type]
            source="self-model-test",
            payload={
                "text": event_id,
                "interaction_mode": "self_preview" if simulated else "companion",
                "simulated_output": simulated,
                "owner_projection_eligible": speaker_class == "owner" and not simulated,
            },
        )
    )


def _seed_relationship(
    path: Path,
    *,
    account_id: str,
    source_event_id: str,
    person_id: str,
    relationship_id: str,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO person_entities (
                person_id, account_id, canonical_key, display_name,
                relationship_to_owner, status, source_event_id, created_at
            ) VALUES (?, ?, ?, '李梅', 'friend', 'confirmed', ?, ?)
            """,
            (
                person_id,
                account_id,
                f"friend:{person_id}",
                source_event_id,
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO relationships (
                relationship_id, account_id, person_id, relationship_type,
                status, source_event_id, valid_at
            ) VALUES (?, ?, ?, 'friend', 'confirmed', ?, ?)
            """,
            (
                relationship_id,
                account_id,
                person_id,
                source_event_id,
                datetime.now(UTC).isoformat(),
            ),
        )


@pytest.mark.asyncio
async def test_candidate_never_activates_and_low_sensitivity_needs_owner_adoption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "self-model.sqlite3"
    registry = SelfModelRegistry.sqlite(path)
    await _evidence(path, account_id="owner-a", event_id="owner-support")

    claim = await registry.create_cognitive_claim(
        account_id="owner-a",
        claim_type="belief",
        statement="我相信承诺应当兑现。",
        confidence=0.9,
        idempotency_key="create-belief",
    )
    assert activation_decision(claim).reasons == (
        "not_approved",
        "missing_owner_adopted_source",
    )

    sourced = await registry.add_source(
        account_id="owner-a",
        item_kind="cognitive_claim",
        item_id=claim.claim_id,
        source_event_id="owner-support",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=claim.version,
        idempotency_key="source-belief",
    )
    confirmed = await registry.review_cognitive_claim(
        account_id="owner-a",
        claim_id=claim.claim_id,
        status="confirmed",
        expected_version=sourced.version,
        step_up_verified=False,
        idempotency_key="confirm-belief",
    )

    assert activation_decision(confirmed).effective is True
    assert await registry.cognitive_claims(account_id="owner-a", effective_only=True) == (
        confirmed,
    )


@pytest.mark.asyncio
async def test_guest_assistant_and_simulated_owner_sources_are_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "self-model.sqlite3"
    registry = SelfModelRegistry.sqlite(path)
    for event_id, speaker_class, simulated in (
        ("guest-source", "guest", False),
        ("assistant-source", "assistant", False),
        ("simulated-owner", "owner", True),
    ):
        await _evidence(
            path,
            account_id="owner-a",
            event_id=event_id,
            speaker_class=speaker_class,
            simulated=simulated,
        )
    claim = await registry.create_cognitive_claim(
        account_id="owner-a",
        claim_type="preference",
        statement="我更喜欢安静的环境。",
        confidence=0.8,
        idempotency_key="create-preference",
    )

    for index, event_id in enumerate(("guest-source", "assistant-source", "simulated-owner")):
        with pytest.raises(UntrustedSelfModelSourceError):
            await registry.add_source(
                account_id="owner-a",
                item_kind="cognitive_claim",
                item_id=claim.claim_id,
                source_event_id=event_id,
                relation="support",
                adopted=True,
                negative=False,
                expected_version=claim.version,
                idempotency_key=f"reject-source-{index}",
            )


@pytest.mark.asyncio
async def test_create_commands_attach_all_sources_with_original_version_semantics(
    tmp_path: Path,
) -> None:
    path = tmp_path / "self-model.sqlite3"
    registry = SelfModelRegistry.sqlite(path)
    registry.initialize()
    await _evidence(path, account_id="owner-a", event_id="atomic-support")
    await _evidence(path, account_id="owner-a", event_id="atomic-counterexample")
    _seed_relationship(
        path,
        account_id="owner-a",
        source_event_id="atomic-support",
        person_id="atomic-person",
        relationship_id="atomic-relationship",
    )
    sources = (
        SourceInput(source_event_id="atomic-support"),
        SourceInput(
            source_event_id="atomic-counterexample",
            relation="counterexample",
            adopted=False,
        ),
    )

    claim = await registry.create_cognitive_claim(
        account_id="owner-a",
        claim_type="belief",
        statement="原子创建声明。",
        confidence=0.8,
        idempotency_key="atomic-claim",
        sources=sources,
    )
    decision = await registry.create_decision_case(
        account_id="owner-a",
        kind="real",
        context="原子创建决策",
        options=("A", "B"),
        constraints=(),
        chosen_option="A",
        rejected_options=("B",),
        outcome="完成",
        reflection="符合约束",
        still_endorsed=True,
        idempotency_key="atomic-decision",
        sources=sources,
    )
    profile = await registry.create_relationship_profile(
        account_id="owner-a",
        person_id="atomic-person",
        relationship_id="atomic-relationship",
        salutation="朋友",
        tone="坦诚",
        advice_style="先听再建议",
        boundaries=(),
        idempotency_key="atomic-profile",
        sources=sources,
    )

    assert claim.version == 3
    assert decision.version == 3
    assert profile.version_number == 1
    assert [source.source_event_id for source in claim.sources] == [
        "atomic-support",
        "atomic-counterexample",
    ]
    assert decision.sources == claim.sources
    assert profile.sources == claim.sources
    duplicate = await registry.create_cognitive_claim(
        account_id="owner-a",
        claim_type="belief",
        statement="原子创建声明。",
        confidence=0.8,
        idempotency_key="atomic-claim",
        sources=sources,
    )
    assert duplicate == claim
    with pytest.raises(SelfModelIdempotencyConflictError):
        await registry.create_cognitive_claim(
            account_id="owner-a",
            claim_type="belief",
            statement="原子创建声明。",
            confidence=0.8,
            idempotency_key="atomic-claim",
            sources=sources[:1],
        )
    receipts = {
        row["idempotency_key"]: row
        for row in (await registry.export_account("owner-a"))[
            "self_model_command_receipts"
        ]
    }
    assert receipts["atomic-claim"]["result_version"] == 3
    assert receipts["atomic-decision"]["result_version"] == 3
    assert receipts["atomic-profile"]["result_version"] == 1


@pytest.mark.asyncio
async def test_create_commands_roll_back_everything_when_any_source_fails(
    tmp_path: Path,
) -> None:
    path = tmp_path / "self-model.sqlite3"
    registry = SelfModelRegistry.sqlite(path)
    registry.initialize()
    await _evidence(path, account_id="owner-a", event_id="valid-source")
    await _evidence(
        path,
        account_id="owner-a",
        event_id="guest-source",
        speaker_class="guest",
    )
    _seed_relationship(
        path,
        account_id="owner-a",
        source_event_id="valid-source",
        person_id="rollback-person",
        relationship_id="rollback-relationship",
    )

    with pytest.raises(UntrustedSelfModelSourceError):
        await registry.create_cognitive_claim(
            account_id="owner-a",
            claim_type="belief",
            statement="第二个来源不存在。",
            confidence=0.8,
            idempotency_key="rollback-claim",
            sources=(
                SourceInput(source_event_id="valid-source"),
                SourceInput(source_event_id="missing-source"),
            ),
        )
    with pytest.raises(UntrustedSelfModelSourceError):
        await registry.create_decision_case(
            account_id="owner-a",
            kind="real",
            context="第二个来源不合格",
            options=("A",),
            constraints=(),
            chosen_option="A",
            rejected_options=(),
            outcome="",
            reflection="",
            still_endorsed=True,
            idempotency_key="rollback-decision",
            sources=(
                SourceInput(source_event_id="valid-source"),
                SourceInput(source_event_id="guest-source"),
            ),
        )
    with pytest.raises(ValueError, match="duplicate source"):
        await registry.create_relationship_profile(
            account_id="owner-a",
            person_id="rollback-person",
            relationship_id="rollback-relationship",
            salutation="朋友",
            tone="坦诚",
            advice_style="先听再建议",
            boundaries=(),
            idempotency_key="rollback-profile",
            sources=(
                SourceInput(source_event_id="valid-source"),
                SourceInput(source_event_id="valid-source"),
            ),
        )

    exported = await registry.export_account("owner-a")
    assert exported["self_model_cognitive_claims"] == []
    assert exported["self_model_decision_cases"] == []
    assert exported["self_model_relationship_profiles"] == []
    assert exported["self_model_sources"] == []
    assert exported["self_model_audit_events"] == []
    assert exported["self_model_command_receipts"] == []


@pytest.mark.asyncio
async def test_negative_evidence_and_unresolved_conflict_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "self-model.sqlite3"
    registry = SelfModelRegistry.sqlite(path)
    await _evidence(path, account_id="owner-a", event_id="positive")
    await _evidence(path, account_id="owner-a", event_id="negative")
    claim = await registry.create_cognitive_claim(
        account_id="owner-a",
        claim_type="belief",
        statement="我总会优先选稳定方案。",
        confidence=0.8,
        idempotency_key="create-negative-case",
        unresolved_conflict=True,
    )
    claim = await registry.add_source(
        account_id="owner-a",
        item_kind="cognitive_claim",
        item_id=claim.claim_id,
        source_event_id="positive",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=claim.version,
        idempotency_key="positive-source",
    )
    claim = await registry.add_source(
        account_id="owner-a",
        item_kind="cognitive_claim",
        item_id=claim.claim_id,
        source_event_id="negative",
        relation="counterexample",
        adopted=False,
        negative=True,
        expected_version=claim.version,
        idempotency_key="negative-source",
    )
    claim = await registry.review_cognitive_claim(
        account_id="owner-a",
        claim_id=claim.claim_id,
        status="confirmed",
        expected_version=claim.version,
        step_up_verified=False,
        idempotency_key="confirm-negative-case",
    )

    decision = activation_decision(claim)
    assert decision.effective is False
    assert decision.reasons == ("unresolved_conflict", "negative_evidence")


@pytest.mark.asyncio
async def test_high_sensitivity_requires_step_up_and_owner_counterexample(
    tmp_path: Path,
) -> None:
    path = tmp_path / "self-model.sqlite3"
    registry = SelfModelRegistry.sqlite(path)
    await _evidence(path, account_id="owner-a", event_id="value-support")
    await _evidence(path, account_id="owner-a", event_id="value-boundary")
    claim = await registry.create_cognitive_claim(
        account_id="owner-a",
        claim_type="value",
        statement="家庭安全高于短期收益。",
        confidence=0.95,
        idempotency_key="create-value",
    )
    claim = await registry.add_source(
        account_id="owner-a",
        item_kind="cognitive_claim",
        item_id=claim.claim_id,
        source_event_id="value-support",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=claim.version,
        idempotency_key="value-support-source",
    )
    with pytest.raises(InvalidSelfModelTransitionError, match="step-up"):
        await registry.review_cognitive_claim(
            account_id="owner-a",
            claim_id=claim.claim_id,
            status="confirmed",
            expected_version=claim.version,
            step_up_verified=False,
            idempotency_key="value-no-step-up",
        )
    with pytest.raises(InvalidSelfModelTransitionError, match="counterexample"):
        await registry.review_cognitive_claim(
            account_id="owner-a",
            claim_id=claim.claim_id,
            status="confirmed",
            expected_version=claim.version,
            step_up_verified=True,
            idempotency_key="value-no-boundary",
        )

    claim = await registry.add_source(
        account_id="owner-a",
        item_kind="cognitive_claim",
        item_id=claim.claim_id,
        source_event_id="value-boundary",
        relation="counterexample",
        adopted=False,
        negative=False,
        expected_version=claim.version,
        idempotency_key="value-boundary-source",
    )
    confirmed = await registry.review_cognitive_claim(
        account_id="owner-a",
        claim_id=claim.claim_id,
        status="confirmed",
        expected_version=claim.version,
        step_up_verified=True,
        idempotency_key="confirm-value",
    )
    assert activation_decision(confirmed).effective is True


@pytest.mark.asyncio
async def test_only_real_still_endorsed_decisions_activate(tmp_path: Path) -> None:
    path = tmp_path / "self-model.sqlite3"
    registry = SelfModelRegistry.sqlite(path)
    await _evidence(path, account_id="owner-a", event_id="decision-source")

    results = []
    for kind, still_endorsed in (
        ("real", True),
        ("hypothetical", True),
        ("real", False),
    ):
        decision = await registry.create_decision_case(
            account_id="owner-a",
            kind=kind,  # type: ignore[arg-type]
            context=f"{kind}-{still_endorsed}",
            options=("A", "B"),
            constraints=("预算",),
            chosen_option="A",
            rejected_options=("B",),
            outcome="已完成",
            reflection="选择符合当时约束",
            still_endorsed=still_endorsed,
            idempotency_key=f"create-{kind}-{still_endorsed}",
        )
        decision = await registry.add_source(
            account_id="owner-a",
            item_kind="decision_case",
            item_id=decision.case_id,
            source_event_id="decision-source",
            relation="support",
            adopted=True,
            negative=False,
            expected_version=decision.version,
            idempotency_key=f"source-{kind}-{still_endorsed}",
        )
        decision = await registry.review_decision_case(
            account_id="owner-a",
            case_id=decision.case_id,
            status="confirmed",
            expected_version=decision.version,
            step_up_verified=False,
            idempotency_key=f"confirm-{kind}-{still_endorsed}",
        )
        results.append(decision)

    assert [activation_decision(item).effective for item in results] == [
        True,
        False,
        False,
    ]
    assert activation_decision(results[1]).reasons == ("hypothetical_decision",)
    assert activation_decision(results[2]).reasons == ("no_longer_endorsed",)


@pytest.mark.asyncio
async def test_relationship_profiles_are_versioned_immutable_and_never_acl(
    tmp_path: Path,
) -> None:
    path = tmp_path / "self-model.sqlite3"
    registry = SelfModelRegistry.sqlite(path)
    registry.initialize()
    await _evidence(path, account_id="owner-a", event_id="relationship-source")
    _seed_relationship(
        path,
        account_id="owner-a",
        source_event_id="relationship-source",
        person_id="person-a",
        relationship_id="relationship-a",
    )
    profile = await registry.create_relationship_profile(
        account_id="owner-a",
        person_id="person-a",
        relationship_id="relationship-a",
        salutation="梅姐",
        tone="坦诚",
        advice_style="先听再建议",
        boundaries=("不谈财务细节",),
        sharing_scope="family",
        idempotency_key="create-profile",
    )
    profile = await registry.add_source(
        account_id="owner-a",
        item_kind="relationship_profile",
        item_id=profile.profile_id,
        source_event_id="relationship-source",
        relation="support",
        adopted=True,
        negative=False,
        expected_version=profile.version_number,
        idempotency_key="profile-source",
    )
    with pytest.raises(InvalidSelfModelTransitionError, match="step-up"):
        await registry.review_relationship_profile(
            account_id="owner-a",
            profile_id=profile.profile_id,
            version_number=1,
            status="approved",
            expected_status="candidate",
            step_up_verified=False,
            idempotency_key="profile-no-step-up",
        )
    approved = await registry.review_relationship_profile(
        account_id="owner-a",
        profile_id=profile.profile_id,
        version_number=1,
        status="approved",
        expected_status="candidate",
        step_up_verified=True,
        idempotency_key="approve-profile",
    )
    assert activation_decision(approved).effective is True
    assert not hasattr(approved, "acl")
    assert not hasattr(approved, "grant")

    revised = await registry.revise_relationship_profile(
        account_id="owner-a",
        profile_id=profile.profile_id,
        expected_version=1,
        salutation="李梅",
        tone="温和",
        advice_style="只在被询问时建议",
        boundaries=("不分享私人记忆",),
        sharing_scope="private",
        idempotency_key="revise-profile",
    )
    old = await registry.get_relationship_profile(
        account_id="owner-a", profile_id=profile.profile_id, version_number=1
    )
    assert old.status == "superseded"
    assert old.salutation == "梅姐"
    assert revised.version_number == 2
    assert revised.status == "candidate"

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE self_model_relationship_profiles SET tone = '越权修改'
                WHERE profile_id = ? AND version_number = 1
                """,
                (profile.profile_id,),
            )


@pytest.mark.asyncio
async def test_account_isolation_idempotency_and_compare_and_set(tmp_path: Path) -> None:
    path = tmp_path / "self-model.sqlite3"
    registry = SelfModelRegistry.sqlite(path)
    await _evidence(path, account_id="owner-a", event_id="concurrent-source-a")
    await _evidence(path, account_id="owner-a", event_id="concurrent-source-b")
    claim, duplicate = await asyncio.gather(
        asyncio.to_thread(
            lambda: asyncio.run(
                registry.create_cognitive_claim(
                    account_id="owner-a",
                    claim_type="belief",
                    statement="同一个命令只创建一次。",
                    confidence=0.8,
                    idempotency_key="same-create",
                )
            )
        ),
        asyncio.to_thread(
            lambda: asyncio.run(
                registry.create_cognitive_claim(
                    account_id="owner-a",
                    claim_type="belief",
                    statement="同一个命令只创建一次。",
                    confidence=0.8,
                    idempotency_key="same-create",
                )
            )
        ),
    )
    assert duplicate.claim_id == claim.claim_id
    with pytest.raises(SelfModelIdempotencyConflictError):
        await registry.create_cognitive_claim(
            account_id="owner-a",
            claim_type="belief",
            statement="复用 key 但改变负载。",
            confidence=0.8,
            idempotency_key="same-create",
        )
    with pytest.raises(SelfModelNotFoundError):
        await registry.get_cognitive_claim(account_id="owner-b", claim_id=claim.claim_id)

    results = await asyncio.gather(
        asyncio.to_thread(
            lambda: asyncio.run(
                registry.add_source(
                    account_id="owner-a",
                    item_kind="cognitive_claim",
                    item_id=claim.claim_id,
                    source_event_id="concurrent-source-a",
                    relation="support",
                    adopted=True,
                    negative=False,
                    expected_version=1,
                    idempotency_key="cas-a",
                )
            )
        ),
        asyncio.to_thread(
            lambda: asyncio.run(
                registry.add_source(
                    account_id="owner-a",
                    item_kind="cognitive_claim",
                    item_id=claim.claim_id,
                    source_event_id="concurrent-source-b",
                    relation="support",
                    adopted=True,
                    negative=False,
                    expected_version=1,
                    idempotency_key="cas-b",
                )
            )
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, SelfModelVersionConflictError) for result in results) == 1
    assert (
        len(
            (
                await registry.get_cognitive_claim(account_id="owner-a", claim_id=claim.claim_id)
            ).sources
        )
        == 1
    )


@pytest.mark.asyncio
async def test_export_delete_seam_and_audit_are_account_scoped(tmp_path: Path) -> None:
    path = tmp_path / "self-model.sqlite3"
    registry = SelfModelRegistry.sqlite(path)
    claim = await registry.create_cognitive_claim(
        account_id="owner-a",
        claim_type="uncertainty",
        statement="我还没有形成稳定看法。",
        confidence=0.4,
        idempotency_key="create-export",
    )
    await registry.create_cognitive_claim(
        account_id="owner-b",
        claim_type="belief",
        statement="另一账户的数据。",
        confidence=0.7,
        idempotency_key="create-other",
    )

    exported = await registry.export_account("owner-a")
    assert [row["claim_id"] for row in exported["self_model_cognitive_claims"]] == [claim.claim_id]
    assert exported["self_model_audit_events"][0]["actor_account_id"] == "owner-a"
    assert exported["self_model_audit_events"][0]["event_type"] == ("cognitive_claim.created")

    deleted = await registry.delete_account("owner-a")
    assert deleted["self_model_cognitive_claims"] == 1
    assert await registry.cognitive_claims(account_id="owner-a") == ()
    assert len(await registry.cognitive_claims(account_id="owner-b")) == 1
