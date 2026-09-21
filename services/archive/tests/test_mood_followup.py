"""The deterministic mood follow-up: measured positives always claimed, negatives never."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import ExtractedClaim, ExtractionUsage, MemoryExtraction
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.mood_followup import (
    MoodFollowupEnsuringExtractor,
    mood_followup_claim,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.memory_components import build_memory_extractor

OCCURRED_AT = datetime(2026, 9, 1, 17, 30, tzinfo=UTC)

# The measured 2026-09-21 emotion-stability set (HANDOFF): the two positives were
# dropped by qwen-flash in 3 of 6 invocations under the shipped prompt; the four
# negatives must stay unclaimed under every prompt variant.
POSITIVE_UTTERANCES = (
    "我今天被老师批评了，好难过。",
    "我今天特别开心，因为我跑步拿了第一名。",
)
NEGATIVE_UTTERANCES = (
    "我今天有点烦",
    "有点无聊",
    "今天吃了个冰淇淋，很开心",
    "公交晚点了，有点烦",
)


def _event(text: str) -> EvidenceEvent:
    return EvidenceEvent(
        event_id="mood-followup-001",
        account_id="account-mood-followup",
        event_type="speech.utterance_finalized",
        occurred_at=OCCURRED_AT,
        speaker_class="owner",
        source="test",
        payload={"text": text},
    )


class _ScriptedExtractor:
    def __init__(self, extraction: MemoryExtraction) -> None:
        self._extraction = extraction
        self.version = "scripted:v1"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        return self._extraction


@pytest.mark.parametrize("text", POSITIVE_UTTERANCES)
async def test_felt_moment_with_careworthy_reason_is_claimed(text: str) -> None:
    claim = mood_followup_claim(text, occurred_at=OCCURRED_AT)

    assert claim is not None
    assert claim.domain_category == "daily_life"
    assert claim.subject_key == "self"
    assert claim.predicate == "mood"
    assert claim.value == text
    assert claim.valid_from == OCCURRED_AT


@pytest.mark.parametrize("text", NEGATIVE_UTTERANCES)
async def test_trivial_and_reasonless_moods_stay_unclaimed(text: str) -> None:
    assert mood_followup_claim(text, occurred_at=OCCURRED_AT) is None


async def test_daily_fact_without_a_feeling_word_is_untouched() -> None:
    assert mood_followup_claim("我今天去公园散步了。", occurred_at=OCCURRED_AT) is None


async def test_wrapper_appends_the_claim_the_model_dropped() -> None:
    dropped = MemoryExtraction(extractor_version="qwen-json:qwen-flash:v2")
    extractor = MoodFollowupEnsuringExtractor(_ScriptedExtractor(dropped))

    extraction = await extractor.extract(_event(POSITIVE_UTTERANCES[0]))

    assert extractor.version == "scripted:v1|mood-followup"
    assert extraction.extractor_version == "qwen-json:qwen-flash:v2"
    assert [claim.value for claim in extraction.claims] == [POSITIVE_UTTERANCES[0]]


async def test_wrapper_does_not_duplicate_a_feeling_the_model_kept() -> None:
    kept = MemoryExtraction(
        claims=(
            ExtractedClaim(
                domain_category="daily_life",
                subject_key="self",
                predicate="mood",
                value="被老师批评了，好难过",
                confidence=0.8,
            ),
        ),
        extractor_version="qwen-json:qwen-flash:v2",
    )
    extractor = MoodFollowupEnsuringExtractor(_ScriptedExtractor(kept))

    extraction = await extractor.extract(_event(POSITIVE_UTTERANCES[0]))

    assert [claim.value for claim in extraction.claims] == ["被老师批评了，好难过"]


async def test_wrapper_appends_when_the_model_kept_the_fact_but_lost_the_feeling() -> None:
    fact_only = MemoryExtraction(
        claims=(
            ExtractedClaim(
                domain_category="daily_life",
                subject_key="self",
                predicate="event",
                value="被老师批评了",
                confidence=0.8,
            ),
        ),
        extractor_version="qwen-json:qwen-flash:v2",
    )
    extractor = MoodFollowupEnsuringExtractor(_ScriptedExtractor(fact_only))

    extraction = await extractor.extract(_event(POSITIVE_UTTERANCES[0]))

    assert [claim.value for claim in extraction.claims] == [
        "被老师批评了",
        POSITIVE_UTTERANCES[0],
    ]


async def test_wrapper_appends_when_the_model_answered_in_english() -> None:
    english = MemoryExtraction(
        claims=(
            ExtractedClaim(
                domain_category="daily_life",
                subject_key="self",
                predicate="mood",
                value="sadness",
                confidence=0.8,
            ),
        ),
        extractor_version="qwen-json:qwen-flash:v2",
    )
    extractor = MoodFollowupEnsuringExtractor(_ScriptedExtractor(english))

    extraction = await extractor.extract(_event(POSITIVE_UTTERANCES[0]))

    assert [claim.value for claim in extraction.claims] == [
        "sadness",
        POSITIVE_UTTERANCES[0],
    ]


async def test_rule_fallback_full_sentence_claim_satisfies_the_feeling() -> None:
    extractor = MoodFollowupEnsuringExtractor(RuleBasedMemoryExtractor())

    extraction = await extractor.extract(_event(POSITIVE_UTTERANCES[0]))

    assert len(extraction.claims) == 1
    assert extraction.claims[0].value == POSITIVE_UTTERANCES[0]


async def test_no_feeling_passes_the_delegate_result_through_unchanged() -> None:
    usage = ExtractionUsage(input_tokens=11, output_tokens=7)
    delegate_result = MemoryExtraction(extractor_version="rules-zh-v2", usage=usage)
    extractor = MoodFollowupEnsuringExtractor(_ScriptedExtractor(delegate_result))

    extraction = await extractor.extract(_event(NEGATIVE_UTTERANCES[3]))

    assert extraction is delegate_result
    assert extraction.usage == usage


def test_assembly_wraps_only_the_model_branch() -> None:
    offline = build_memory_extractor(
        ControlSettings(_env_file=None, OFFLINE_MOCK=True, DASHSCOPE_API_KEY="k")
    )
    unkeyed = build_memory_extractor(ControlSettings(_env_file=None))
    assert isinstance(offline, RuleBasedMemoryExtractor)
    assert isinstance(unkeyed, RuleBasedMemoryExtractor)

    keyed = build_memory_extractor(
        ControlSettings(_env_file=None, OFFLINE_MOCK=False, DASHSCOPE_API_KEY="k")
    )

    assert isinstance(keyed, MoodFollowupEnsuringExtractor)
    assert keyed.version.endswith("|mood-followup")
