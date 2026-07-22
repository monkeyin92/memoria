from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.persona.domain import (
    ObservationResult,
    PersonaCounterexampleRequiredError,
    PersonaEvidence,
    PersonaRequest,
    PersonaReview,
    PersonaTraitCategory,
)
from services.persona.engine import PersonaEngine
from services.persona.rules import PersonaCandidate, RuleBasedPersonaExtractor


async def _record(
    archive: LifeArchive,
    *,
    event_id: str,
    text: str,
    speaker_class: str = "owner",
    event_type: str = "speech.utterance_finalized",
    minute: int = 0,
    session_id: str | None = "persona-session",
    profile_id: str = "persona-shadow-profile",
    persona_eligible: bool | None = True,
    owner_projection_eligible: bool | None = True,
    prompt_kind: str = "spontaneous",
) -> None:
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id="persona-account",
            session_id=session_id,
            turn_id=minute + 1,
            event_type=event_type,
            occurred_at=datetime(2026, 7, 19, 13, minute, tzinfo=UTC),
            speaker_class=speaker_class,  # type: ignore[arg-type]
            source="test",
            payload={
                "text": text,
                **(
                    {"persona_eligible": persona_eligible}
                    if persona_eligible is not None
                    else {}
                ),
                **(
                    {"owner_projection_eligible": owner_projection_eligible}
                    if owner_projection_eligible is not None
                    else {}
                ),
                "interaction_mode": "companion",
                "prompt_kind": prompt_kind,
                **({"actual_heard": True} if speaker_class == "assistant" else {}),
                **(
                    {
                        "speaker_reason_code": "shadow_owner_candidate",
                        "speaker_profile_id": profile_id,
                        "speaker_quality_score": 0.9,
                        "speaker_model_version": "campplus-test",
                        "speaker_template_version": 1,
                    }
                    if speaker_class == "uncertain"
                    else {}
                ),
            },
        )
    )


class _PausedExtractor:
    version = "paused-test-extractor"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(
        self,
        text: str,
        evidence: PersonaEvidence,
    ) -> tuple[PersonaCandidate, ...]:
        self.started.set()
        await self.release.wait()
        return await RuleBasedPersonaExtractor().extract(text, evidence)


class _ExclusiveBucketExtractor:
    version = "exclusive-bucket-test-extractor"

    def __init__(self, category: PersonaTraitCategory) -> None:
        self._category = category

    async def extract(
        self,
        text: str,
        _evidence: PersonaEvidence,
    ) -> tuple[PersonaCandidate, ...]:
        return (
            PersonaCandidate(
                category=self._category,
                normalized_key=text,
                description=f"{self._category}:{text}",
                context="conversation",
            ),
        )


async def _observe_bucket(
    archive: LifeArchive,
    engine: PersonaEngine,
    *,
    event_id: str,
    bucket: str,
    minute: int,
    speaker_class: str = "owner",
    session_id: str | None = "persona-session",
    profile_id: str = "persona-shadow-profile",
) -> ObservationResult:
    await _record(
        archive,
        event_id=event_id,
        text=bucket,
        speaker_class=speaker_class,
        minute=minute,
        session_id=session_id,
        profile_id=profile_id,
    )
    return await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id=event_id,
            learning_allowed=True,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("speaker_class", "persona_eligible"),
    (
        pytest.param("owner", None, id="owner-missing"),
        pytest.param("owner", False, id="owner-false"),
        pytest.param("uncertain", None, id="uncertain-missing"),
        pytest.param("uncertain", False, id="uncertain-false"),
    ),
)
async def test_persona_learning_fails_closed_without_explicit_turn_eligibility(
    tmp_path: Path,
    speaker_class: str,
    persona_eligible: bool | None,
) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )
    await _record(
        archive,
        event_id="ineligible-persona-turn",
        text="我觉得先把事实弄清楚。",
        speaker_class=speaker_class,
        persona_eligible=persona_eligible,
    )

    observed = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="ineligible-persona-turn",
            learning_allowed=True,
        )
    )

    assert observed.accepted is False
    assert observed.reason == "persona_ineligible_turn"
    assert (
        await engine.capsule(PersonaRequest(account_id="persona-account", speaker_class="owner"))
    ).entries == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt_kind", "expected_status"),
    (
        pytest.param("leading", "candidate", id="leading-remains-candidate"),
        pytest.param("spontaneous", "confirmed", id="spontaneous-can-confirm"),
    ),
)
async def test_prompt_kind_weights_owner_persona_promotion(
    tmp_path: Path,
    prompt_kind: str,
    expected_status: str,
) -> None:
    path = tmp_path / f"prompt-weight-{prompt_kind}.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    for index in range(3):
        event_id = f"{prompt_kind}-{index}"
        await _record(
            archive,
            event_id=event_id,
            text="我觉得先确认事实，再做决定。",
            minute=index,
            prompt_kind=prompt_kind,
        )
        await engine.observe(
            PersonaEvidence(
                account_id="persona-account",
                source_event_id=event_id,
                learning_allowed=True,
            )
        )

    verbal_tic = next(
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == "verbal_tic"
    )
    assert verbal_tic.status == expected_status


@pytest.mark.asyncio
async def test_guided_prompts_do_not_pollute_spontaneous_style_stats(
    tmp_path: Path,
) -> None:
    path = tmp_path / "prompt-style-stats.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    for index, prompt_kind in enumerate(("leading", "open", "spontaneous")):
        event_id = f"style-stat-{prompt_kind}"
        await _record(
            archive,
            event_id=event_id,
            text="我觉得这件事要慢慢说。",
            minute=index,
            prompt_kind=prompt_kind,
        )
        await engine.observe(
            PersonaEvidence(
                account_id="persona-account",
                source_event_id=event_id,
                learning_allowed=True,
            )
        )

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT utterance_count, char_count
            FROM speech_style_stats
            WHERE account_id = ? AND scene = ?
            """,
            ("persona-account", "conversation"),
        ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] == len("我觉得这件事要慢慢说。")


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["sentence_length", "speech_rate", "pause_style"])
async def test_mixed_exclusive_owner_buckets_do_not_auto_promote(
    tmp_path: Path,
    category: PersonaTraitCategory,
) -> None:
    path = tmp_path / f"mixed-{category}.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(path, extractor=_ExclusiveBucketExtractor(category))
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    results = []
    for index, bucket in enumerate(("left", "right") * 3):
        results.append(
            await _observe_bucket(
                archive,
                engine,
                event_id=f"mixed-{category}-{index}",
                bucket=bucket,
                minute=index,
            )
        )

    traits = [
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == category
    ]

    assert all(result.published_version_id is None for result in results)
    assert {trait.status for trait in traits} == {"candidate"}
    assert await engine.versions(account_id="persona-account") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["sentence_length", "speech_rate", "pause_style"])
async def test_stable_exclusive_owner_bucket_auto_promotes(
    tmp_path: Path,
    category: PersonaTraitCategory,
) -> None:
    path = tmp_path / f"stable-{category}.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(path, extractor=_ExclusiveBucketExtractor(category))
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    results = [
        await _observe_bucket(
            archive,
            engine,
            event_id=f"stable-{category}-{index}",
            bucket="stable",
            minute=index,
        )
        for index in range(3)
    ]
    traits = [
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == category
    ]

    assert all(result.published_version_id is None for result in results[:2])
    assert results[-1].published_version_id is not None
    assert [(trait.description, trait.status) for trait in traits] == [
        (f"{category}:stable", "confirmed")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["sentence_length", "speech_rate", "pause_style"])
async def test_exclusive_owner_dominance_change_never_leaves_two_confirmed(
    tmp_path: Path,
    category: PersonaTraitCategory,
) -> None:
    path = tmp_path / f"changed-{category}.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(path, extractor=_ExclusiveBucketExtractor(category))
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    for index in range(3):
        await _observe_bucket(
            archive,
            engine,
            event_id=f"changed-{category}-old-{index}",
            bucket="old",
            minute=index,
        )
    for index in range(6):
        await _observe_bucket(
            archive,
            engine,
            event_id=f"changed-{category}-new-{index}",
            bucket="new",
            minute=index + 3,
        )
        current = [
            trait
            for trait in await engine.traits(account_id="persona-account")
            if trait.category == category and trait.status == "confirmed"
        ]
        assert len(current) <= 1

    traits = [
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == category
    ]
    assert {(trait.description, trait.status) for trait in traits} == {
        (f"{category}:old", "candidate"),
        (f"{category}:new", "confirmed"),
    }


@pytest.mark.asyncio
async def test_owner_lane_replaces_historical_uncertain_bucket_and_owns_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "owner-lane.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(
        path,
        extractor=_ExclusiveBucketExtractor("sentence_length"),
    )
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    for index in range(6):
        await _observe_bucket(
            archive,
            engine,
            event_id=f"uncertain-history-{index}",
            bucket="shadow",
            minute=index,
            speaker_class="uncertain",
            session_id=f"shadow-session-{index // 2}",
        )
    for index in range(3):
        await _observe_bucket(
            archive,
            engine,
            event_id=f"owner-current-{index}",
            bucket="owner",
            minute=index + 6,
        )

    traits = [
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == "sentence_length"
    ]
    capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="owner",
            max_chars=500,
        )
    )
    sentence_entry = next(entry for entry in capsule.entries if entry.category == "sentence_length")

    assert {(trait.description, trait.status) for trait in traits} == {
        ("sentence_length:shadow", "candidate"),
        ("sentence_length:owner", "confirmed"),
    }
    assert sentence_entry.description == "sentence_length:owner"
    assert sentence_entry.source_event_ids == tuple(f"owner-current-{index}" for index in range(3))


@pytest.mark.asyncio
async def test_owner_snapshot_excludes_prior_uncertain_evidence_for_same_bucket(
    tmp_path: Path,
) -> None:
    path = tmp_path / "owner-snapshot.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(
        path,
        extractor=_ExclusiveBucketExtractor("sentence_length"),
    )
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    for index in range(6):
        await _observe_bucket(
            archive,
            engine,
            event_id=f"shared-uncertain-{index}",
            bucket="shared",
            minute=index,
            speaker_class="uncertain",
            session_id=f"shared-shadow-session-{index // 2}",
        )
    for index in range(3):
        await _observe_bucket(
            archive,
            engine,
            event_id=f"shared-owner-{index}",
            bucket="shared",
            minute=index + 6,
        )

    capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="owner",
            max_chars=500,
        )
    )
    sentence_entry = next(entry for entry in capsule.entries if entry.category == "sentence_length")

    assert sentence_entry.source_event_ids == tuple(f"shared-owner-{index}" for index in range(3))


@pytest.mark.asyncio
async def test_uncertain_exclusive_buckets_require_one_dominant_profile_lane(
    tmp_path: Path,
) -> None:
    path = tmp_path / "uncertain-lane.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(
        path,
        extractor=_ExclusiveBucketExtractor("sentence_length"),
    )
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    results = []
    for index in range(12):
        results.append(
            await _observe_bucket(
                archive,
                engine,
                event_id=f"uncertain-lane-{index}",
                bucket="left" if index % 2 == 0 else "right",
                minute=index,
                speaker_class="uncertain",
                session_id=f"uncertain-lane-session-{index // 2}",
            )
        )

    traits = [
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == "sentence_length"
    ]
    assert all(result.published_version_id is None for result in results)
    assert {trait.status for trait in traits} == {"candidate"}


@pytest.mark.asyncio
async def test_uncertain_exclusive_bucket_does_not_merge_multiple_profiles(
    tmp_path: Path,
) -> None:
    path = tmp_path / "uncertain-profiles.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(
        path,
        extractor=_ExclusiveBucketExtractor("sentence_length"),
    )
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    results = []
    for index in range(6):
        profile_index = index % 2
        results.append(
            await _observe_bucket(
                archive,
                engine,
                event_id=f"uncertain-profile-{index}",
                bucket="shared",
                minute=index,
                speaker_class="uncertain",
                session_id=f"uncertain-profile-session-{index // 2}",
                profile_id=f"shadow-profile-{profile_index}",
            )
        )

    traits = [
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == "sentence_length"
    ]
    assert all(result.published_version_id is None for result in results)
    assert [(trait.description, trait.status) for trait in traits] == [
        ("sentence_length:shared", "candidate")
    ]


@pytest.mark.asyncio
async def test_manual_confirmation_keeps_exclusive_category_single_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manual-exclusive.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(
        path,
        extractor=_ExclusiveBucketExtractor("sentence_length"),
    )
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    for index, bucket in enumerate(("left", "right")):
        await _observe_bucket(
            archive,
            engine,
            event_id=f"manual-exclusive-{index}",
            bucket=bucket,
            minute=index,
        )
    traits = await engine.traits(account_id="persona-account")
    by_description = {trait.description: trait for trait in traits}
    await engine.review(
        PersonaReview(
            account_id="persona-account",
            trait_id=by_description["sentence_length:left"].trait_id,
            action="confirm",
        )
    )
    await engine.review(
        PersonaReview(
            account_id="persona-account",
            trait_id=by_description["sentence_length:right"].trait_id,
            action="confirm",
        )
    )

    confirmed = [
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == "sentence_length" and trait.status == "confirmed"
    ]
    assert [trait.description for trait in confirmed] == ["sentence_length:right"]


@pytest.mark.asyncio
async def test_observation_rechecks_consent_after_extraction_before_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="consent-race-observation",
        text="我觉得先把事实弄清楚。",
    )
    extractor = _PausedExtractor()
    engine = PersonaEngine.sqlite(path, extractor=extractor)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    observation = asyncio.create_task(
        engine.observe(
            PersonaEvidence(
                account_id="persona-account",
                source_event_id="consent-race-observation",
                learning_allowed=True,
            )
        )
    )
    await extractor.started.wait()
    await engine.revoke_consent(account_id="persona-account")
    extractor.release.set()
    result = await observation

    assert result.accepted is False
    assert result.reason == "learning_not_authorized"
    assert await engine.traits(account_id="persona-account") == ()


@pytest.mark.asyncio
async def test_engine_capsule_cannot_bypass_revoked_consent(tmp_path: Path) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )
    for index in range(3):
        await _record(
            archive,
            event_id=f"revoked-capsule-{index}",
            text="我觉得先把事实弄清楚。",
            minute=index,
        )
        await engine.observe(
            PersonaEvidence(
                account_id="persona-account",
                source_event_id=f"revoked-capsule-{index}",
                learning_allowed=True,
            )
        )
    await engine.revoke_consent(account_id="persona-account")

    capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="owner",
            topic="表达看法",
        )
    )

    assert capsule.entries == ()
    assert capsule.prompt_fragment == ""


@pytest.mark.asyncio
async def test_repeated_owner_style_promotes_with_traceability_but_pollution_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    texts = (
        "我觉得先把事实弄清楚，再讨论责任。",
        "我觉得这件事可以先听听孩子怎么说。",
        "我觉得答应别人的事就应该做到。",
    )
    for index, text in enumerate(texts):
        await _record(archive, event_id=f"style-{index}", text=text, minute=index)
    await _record(
        archive,
        event_id="style-guest",
        text="我觉得这句访客话不能学。",
        speaker_class="guest",
        minute=4,
    )
    await _record(
        archive,
        event_id="style-assistant",
        text="我觉得这句合成回复不能学。",
        speaker_class="assistant",
        event_type="assistant.playout_stopped",
        minute=5,
    )
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    results = [
        await engine.observe(
            PersonaEvidence(
                account_id="persona-account",
                source_event_id=f"style-{index}",
                learning_allowed=True,
                scene="conversation",
            )
        )
        for index in range(3)
    ]
    guest = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="style-guest",
            learning_allowed=True,
        )
    )
    assistant = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="style-assistant",
            learning_allowed=True,
        )
    )
    contaminated = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="style-2",
            learning_allowed=True,
            contamination_flags=("echo",),
        )
    )
    capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="owner",
            topic="怎么表达我的看法",
            max_chars=500,
        )
    )
    guest_capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="guest",
            topic="怎么表达我的看法",
        )
    )

    assert all(result.accepted for result in results)
    assert results[-1].published_version_id is not None
    assert (guest.reason, assistant.reason, contaminated.reason) == (
        "speaker_not_owner",
        "assistant_or_synthetic_evidence",
        "contaminated_evidence",
    )
    assert "我觉得" in capsule.prompt_fragment
    assert capsule.entries[0].source_event_ids == ("style-0", "style-1", "style-2")
    assert guest_capsule.entries == ()


@pytest.mark.asyncio
async def test_uncertain_style_auto_promotes_after_repeated_cross_session_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    for index in range(6):
        await _record(
            archive,
            event_id=f"uncertain-style-{index}",
            text="我觉得先把事实弄清楚，再讨论责任。",
            speaker_class="uncertain",
            minute=index,
            session_id=f"persona-shadow-{index // 2}",
        )
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    results = [
        await engine.observe(
            PersonaEvidence(
                account_id="persona-account",
                source_event_id=f"uncertain-style-{index}",
                learning_allowed=True,
                speech_duration_ms=8_000,
                pause_ratio=0.55,
            )
        )
        for index in range(6)
    ]
    traits = await engine.traits(account_id="persona-account")
    versions_after_learning = await engine.versions(account_id="persona-account")
    verbal_tic = next(trait for trait in traits if trait.category == "verbal_tic")
    baseline_uncertain_capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="uncertain",
            topic="表达看法",
        )
    )
    confirmed_style_capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="uncertain",
            topic="表达看法",
            confirmed_style_only=True,
        )
    )

    assert all(result.accepted for result in results)
    assert all(result.published_version_id is None for result in results[:5])
    assert results[-1].published_version_id is not None
    assert verbal_tic.status == "confirmed"
    assert verbal_tic.source_event_ids == tuple(f"uncertain-style-{index}" for index in range(6))
    assert not {"speech_rate", "pause_style"} & {trait.category for trait in traits}
    assert versions_after_learning[0].version_number == 1
    assert baseline_uncertain_capsule.entries == ()
    assert {entry.category for entry in confirmed_style_capsule.entries} == {
        "verbal_tic",
        "sentence_length",
        "discourse_style",
    }
    assert "已确认表达风格 v1" in confirmed_style_capsule.prompt_fragment
    assert confirmed_style_capsule.entries[0].context == ""
    assert confirmed_style_capsule.entries[0].counterexample == ""
    assert confirmed_style_capsule.entries[0].source_event_ids == ()

    await engine.review(
        PersonaReview(
            account_id="persona-account",
            trait_id=verbal_tic.trait_id,
            action="correct",
            corrected_description="我姐姐住在上海，手机号是 13800138000。",
        )
    )
    private_text_capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="uncertain",
            topic="表达看法",
            confirmed_style_only=True,
        )
    )
    assert all(entry.category != "verbal_tic" for entry in private_text_capsule.entries)
    assert "上海" not in private_text_capsule.prompt_fragment


@pytest.mark.asyncio
async def test_uncertain_style_repetition_in_one_session_does_not_auto_promote(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    for index in range(8):
        await _record(
            archive,
            event_id=f"same-session-style-{index}",
            text="我觉得先把事实弄清楚，再讨论责任。",
            speaker_class="uncertain",
            minute=index,
            session_id="one-shadow-session",
        )
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    results = [
        await engine.observe(
            PersonaEvidence(
                account_id="persona-account",
                source_event_id=f"same-session-style-{index}",
                learning_allowed=True,
            )
        )
        for index in range(8)
    ]
    verbal_tic = next(
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == "verbal_tic"
    )

    assert all(result.published_version_id is None for result in results)
    assert verbal_tic.status == "candidate"
    assert await engine.versions(account_id="persona-account") == ()


@pytest.mark.asyncio
async def test_uncertain_style_without_session_identity_does_not_auto_promote(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    for index in range(8):
        await _record(
            archive,
            event_id=f"missing-session-style-{index}",
            text="我觉得先把事实弄清楚，再讨论责任。",
            speaker_class="uncertain",
            minute=index,
            session_id=None,
        )
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    results = [
        await engine.observe(
            PersonaEvidence(
                account_id="persona-account",
                source_event_id=f"missing-session-style-{index}",
                learning_allowed=True,
            )
        )
        for index in range(8)
    ]
    verbal_tic = next(
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == "verbal_tic"
    )

    assert all(result.published_version_id is None for result in results)
    assert verbal_tic.status == "candidate"
    assert await engine.versions(account_id="persona-account") == ()


@pytest.mark.asyncio
async def test_disabled_style_is_not_reactivated_by_later_observations(tmp_path: Path) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    for index in range(4):
        await _record(
            archive,
            event_id=f"disabled-style-{index}",
            text="我觉得先把事实弄清楚。",
            minute=index,
        )
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )
    first_results = [
        await engine.observe(
            PersonaEvidence(
                account_id="persona-account",
                source_event_id=f"disabled-style-{index}",
                learning_allowed=True,
            )
        )
        for index in range(3)
    ]
    verbal_tic = next(
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == "verbal_tic"
    )
    disabled = await engine.review(
        PersonaReview(
            account_id="persona-account",
            trait_id=verbal_tic.trait_id,
            action="disable",
        )
    )

    observed_after_disable = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="disabled-style-3",
            learning_allowed=True,
        )
    )
    updated = next(
        trait
        for trait in await engine.traits(account_id="persona-account")
        if trait.category == "verbal_tic"
    )

    assert first_results[-1].published_version_id is not None
    assert disabled.status == "disabled"
    assert observed_after_disable.published_version_id is None
    assert updated.status == "disabled"
    assert updated.observation_count == 3


@pytest.mark.asyncio
async def test_decision_trait_requires_review_and_versions_can_be_corrected_and_rolled_back(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="decision-001",
        text="做重大决定时，我习惯先列事实，再睡一晚。",
    )
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    observed = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="decision-001",
            learning_allowed=True,
            scene="major_decision",
        )
    )
    before_review = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="owner",
            topic="重大决定",
        )
    )
    trait_id = observed.candidate_trait_ids[0]
    with pytest.raises(PersonaCounterexampleRequiredError):
        await engine.review(
            PersonaReview(
                account_id="persona-account",
                trait_id=trait_id,
                action="confirm",
            )
        )
    confirmed = await engine.review(
        PersonaReview(
            account_id="persona-account",
            trait_id=trait_id,
            action="confirm",
            counterexample="紧急安全风险出现时会立即行动。",
        )
    )
    first_version_id = confirmed.version_id
    first_capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="owner",
            topic="重大决定",
        )
    )
    uncertain_style_capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="uncertain",
            topic="重大决定",
            confirmed_style_only=True,
        )
    )
    corrected = await engine.review(
        PersonaReview(
            account_id="persona-account",
            trait_id=trait_id,
            action="correct",
            corrected_description="重大决定前先核对事实，并至少留一晚冷静期。",
        )
    )
    corrected_capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="owner",
            topic="重大决定",
        )
    )
    rolled_back = await engine.rollback(
        account_id="persona-account",
        version_id=first_version_id or "",
    )
    rollback_capsule = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="owner",
            topic="重大决定",
        )
    )

    assert observed.accepted is True
    assert observed.published_version_id is None
    assert before_review.entries == ()
    assert confirmed.status == "confirmed"
    assert first_version_id
    assert first_capsule.entries[0].source_event_ids == ("decision-001",)
    assert uncertain_style_capsule.entries == ()
    assert corrected.version_id != first_version_id
    assert "冷静期" in corrected_capsule.prompt_fragment
    assert rolled_back.version_id == first_version_id
    assert "先列事实，再睡一晚" in rollback_capsule.prompt_fragment
    assert "冷静期" not in rollback_capsule.prompt_fragment


@pytest.mark.asyncio
async def test_capsule_respects_disable_switch_and_character_budget(tmp_path: Path) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="budget-001",
        text="做重大决定时，我习惯先收集事实，再把不同方案逐项比较。",
    )
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )
    observed = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="budget-001",
            learning_allowed=True,
        )
    )
    await engine.review(
        PersonaReview(
            account_id="persona-account",
            trait_id=observed.candidate_trait_ids[0],
            action="confirm",
            counterexample="紧急安全风险出现时会立即行动。",
        )
    )

    disabled = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="owner",
            topic="方案",
            enabled=False,
        )
    )
    bounded = await engine.capsule(
        PersonaRequest(
            account_id="persona-account",
            speaker_class="owner",
            topic="方案",
            max_chars=180,
        )
    )

    assert disabled.entries == ()
    assert disabled.prompt_fragment == ""
    assert bounded.entries
    assert len(bounded.prompt_fragment) <= 180
