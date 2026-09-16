from datetime import UTC, datetime

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.memory_write_policy import filter_extraction_for_subject


def _event(text: str) -> EvidenceEvent:
    return EvidenceEvent(
        event_id=f"student-memory-{len(text)}",
        account_id="student-account",
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 8, 9, 10, 0, tzinfo=UTC),
        speaker_class="owner",
        source="test",
        payload={"text": text},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("我今天练习了英语口语，过去式还是薄弱点。", "study_progress"),
        ("学习时我喜欢先跟读，再自己说一遍。", "learning_preference"),
        ("我喜欢喝热牛奶。", "daily_life"),
    ],
)
async def test_student_categories_are_explicit_and_do_not_capture_general_preferences(
    text: str,
    category: str,
) -> None:
    result = await RuleBasedMemoryExtractor().extract(_event(text))

    assert result.claims[0].domain_category == category
    assert result.timeline[0].domain_category == category


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "我和爸爸最近总吵架。",
        "我被诊断为焦虑症。",
        "请记住我的身高是一米六。",
        "我妈妈叫李梅。",
    ],
)
async def test_minor_long_term_filter_drops_family_health_body_and_relationship_facts(
    text: str,
) -> None:
    event = _event(text)
    extraction = await RuleBasedMemoryExtractor().extract(event)

    filtered = filter_extraction_for_subject(
        event,
        extraction,
        subject_category="minor",
    )

    assert filtered.claims == ()
    assert filtered.people == ()
    assert filtered.relationships == ()
    assert filtered.timeline == ()
    assert filtered.knowledge == ()


@pytest.mark.asyncio
async def test_minor_long_term_filter_keeps_only_safe_study_and_learning_preferences() -> None:
    for text, expected in (
        ("我今天练习了英语口语，过去式还是薄弱点。", "study_progress"),
        ("学习时我喜欢先跟读，再自己说一遍。", "learning_preference"),
    ):
        event = _event(text)
        extraction = await RuleBasedMemoryExtractor().extract(event)

        filtered = filter_extraction_for_subject(
            event,
            extraction,
            subject_category="minor",
        )

        assert filtered.claims[0].domain_category == expected
        assert filtered.timeline[0].domain_category == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_aliases", "forbidden"),
    [
        # The owner's own words for how the family refers to someone.
        ("我妈妈叫李梅，家里人也叫她阿梅。", {"妈妈", "母亲", "李梅", "阿梅"}, ()),
        ("我妈妈叫李梅。", {"妈妈", "母亲", "李梅"}, ("阿梅",)),
        # A role word is not an alias, and a self-introduction is not a
        # third-party alias.
        ("我妈妈叫李梅，家里人也叫她妈妈。", {"妈妈", "母亲", "李梅"}, ("阿梅",)),
        ("我妈妈叫李梅，我叫阿梅。", {"妈妈", "母亲", "李梅"}, ("阿梅",)),
    ],
)
async def test_third_party_alias_is_taken_only_from_its_own_clause(
    text: str,
    expected_aliases: set[str],
    forbidden: tuple[str, ...],
) -> None:
    extraction = await RuleBasedMemoryExtractor().extract(_event(text))

    assert len(extraction.people) == 1
    aliases = set(extraction.people[0].aliases)
    assert aliases == expected_aliases
    assert not (aliases & set(forbidden))


@pytest.mark.asyncio
async def test_two_people_in_one_utterance_never_share_an_alias() -> None:
    """An alias clause may not be attached when more than one person matched."""

    extraction = await RuleBasedMemoryExtractor().extract(
        _event("我妈妈叫李梅，家里人也叫她阿梅。")
    )
    assert [person.display_name for person in extraction.people] == ["李梅"]
    assert "阿梅" in extraction.people[0].aliases
