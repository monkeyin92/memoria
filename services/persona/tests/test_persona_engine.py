from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.persona.domain import (
    PersonaCounterexampleRequiredError,
    PersonaEvidence,
    PersonaRequest,
    PersonaReview,
)
from services.persona.engine import PersonaEngine


async def _record(
    archive: LifeArchive,
    *,
    event_id: str,
    text: str,
    speaker_class: str = "owner",
    event_type: str = "speech.utterance_finalized",
    minute: int = 0,
) -> None:
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id="persona-account",
            session_id="persona-session",
            turn_id=minute + 1,
            event_type=event_type,
            occurred_at=datetime(2026, 7, 19, 13, minute, tzinfo=UTC),
            speaker_class=speaker_class,  # type: ignore[arg-type]
            source="test",
            payload={"text": text, **({"actual_heard": True} if speaker_class == "assistant" else {})},
        )
    )


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
