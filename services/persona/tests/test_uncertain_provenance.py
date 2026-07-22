from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.persona.domain import PersonaEvidence
from services.persona.engine import PersonaEngine


async def _record_uncertain(
    archive: LifeArchive,
    *,
    event_id: str,
    session_id: str,
    profile_id: str,
    reason_code: str = "shadow_owner_candidate",
    quality_score: float = 0.9,
    minute: int = 0,
) -> None:
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id="persona-account",
            session_id=session_id,
            turn_id=minute + 1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 7, 21, 12, minute, tzinfo=UTC),
            speaker_class="uncertain",
            source="funasr.authoritative_final",
            payload={
                "text": "我觉得先把事实弄清楚，再讨论责任。",
                "persona_eligible": True,
                "speaker_reason_code": reason_code,
                "speaker_profile_id": profile_id,
                "speaker_quality_score": quality_score,
                "speaker_model_version": "campplus-test",
                "speaker_template_version": 1,
            },
        )
    )


@pytest.mark.asyncio
async def test_ambiguous_uncertain_speaker_cannot_create_persona_candidate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record_uncertain(
        archive,
        event_id="ambiguous-speaker",
        session_id="session-1",
        profile_id="profile-1",
        reason_code="shadow_ambiguous_candidate",
    )
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    result = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="ambiguous-speaker",
            learning_allowed=True,
        )
    )

    assert result.accepted is False
    assert result.reason == "untrusted_uncertain_speaker"
    assert await engine.traits(account_id="persona-account") == ()


@pytest.mark.asyncio
async def test_uncertain_evidence_from_multiple_shadow_profiles_does_not_auto_promote(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    for index in range(6):
        await _record_uncertain(
            archive,
            event_id=f"mixed-profile-{index}",
            session_id=f"session-{index // 2}",
            profile_id="profile-1" if index < 4 else "profile-2",
            minute=index,
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
                source_event_id=f"mixed-profile-{index}",
                learning_allowed=True,
            )
        )
        for index in range(6)
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
async def test_new_shadow_profile_can_build_a_fresh_auto_promotion_lane(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persona.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record_uncertain(
        archive,
        event_id="retired-profile-evidence",
        session_id="retired-session",
        profile_id="retired-profile",
    )
    for index in range(6):
        await _record_uncertain(
            archive,
            event_id=f"current-profile-{index}",
            session_id=f"current-session-{index // 2}",
            profile_id="current-profile",
            minute=index + 1,
        )
    engine = PersonaEngine.sqlite(path)
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )

    await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="retired-profile-evidence",
            learning_allowed=True,
        )
    )
    results = [
        await engine.observe(
            PersonaEvidence(
                account_id="persona-account",
                source_event_id=f"current-profile-{index}",
                learning_allowed=True,
            )
        )
        for index in range(6)
    ]
    confirmed = {
        trait.category
        for trait in await engine.traits(account_id="persona-account")
        if trait.status == "confirmed"
    }

    assert results[-1].published_version_id is not None
    assert {"verbal_tic", "sentence_length", "discourse_style"} <= confirmed
