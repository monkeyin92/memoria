"""Conservative local extractor; richer Qwen extraction can implement the same port."""

from __future__ import annotations

import re
from dataclasses import replace

from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import (
    DomainCategory,
    ExtractedClaim,
    ExtractedKnowledge,
    ExtractedPerson,
    ExtractedRelationship,
    ExtractedTimeline,
    MemoryExtraction,
)
from services.archive.memory_write_policy import (
    explicit_remember_content,
    low_risk_self_fact_predicate,
)

_RELATION_ALIASES = {
    "妈妈": ("mother", ("妈妈", "母亲")),
    "母亲": ("mother", ("妈妈", "母亲")),
    "爸爸": ("father", ("爸爸", "父亲")),
    "父亲": ("father", ("爸爸", "父亲")),
    "妻子": ("wife", ("妻子", "爱人")),
    "丈夫": ("husband", ("丈夫", "爱人")),
    "儿子": ("son", ("儿子",)),
    "女儿": ("daughter", ("女儿",)),
    "同事": ("colleague", ("同事",)),
    "朋友": ("friend", ("朋友",)),
}
_PERSON = re.compile(
    r"我(?:的)?(?P<relation>妈妈|母亲|爸爸|父亲|妻子|丈夫|儿子|女儿|同事|朋友)"
    r"(?:叫|名叫|是)?(?P<name>[\u4e00-\u9fff]{2,4}?)(?=今年|负责|[，。,.]|$)"
)
_AGE = re.compile(r"(?:今年)?(?P<age>\d{1,3})岁")
#: Third-party alias in the owner's own words: "家里人也叫她阿梅".
#: Only the alias in this exact clause shape is taken, so a sentence that
#: merely contains a name elsewhere cannot silently become an alias.
_THIRD_PARTY_ALIAS = re.compile(
    r"(?:家里人|家人|大家|别人|我们|村里人|同事)"
    r"(?:都|也)?"
    r"叫(?:她|他|它|ta|TA)"
    r"(?P<alias>[\u4e00-\u9fff]{2,4}?)"
    r"(?=的|说|，|。|,|\?|？|$)"
)
#: Words that describe a role rather than name a person.
_ROLE_WORDS = frozenset({"妈妈", "母亲", "爸爸", "父亲", "妻子", "丈夫", "儿子", "女儿", "同事", "朋友"})


def _category(text: str) -> DomainCategory:
    if any(
        phrase in text
        for phrase in (
            "学习时我喜欢",
            "我喜欢先学",
            "我喜欢边学",
            "我更喜欢跟读",
            "我习惯先复习",
            "讲慢一点我更容易",
        )
    ):
        return "learning_preference"
    if any(
        word in text
        for word in (
            "英语口语",
            "发音",
            "过去式",
            "单词",
            "作业",
            "练习了",
            "掌握了",
            "薄弱点",
            "这道题卡",
        )
    ):
        return "study_progress"
    if any(word in text for word in ("家训", "家风", "我们家", "做人要")):
        return "family_principle"
    if any(word in text for word in ("育儿", "孩子", "教育孩子", "当父母")):
        return "parenting_principle"
    if any(word in text for word in ("工作", "项目", "客户", "复盘", "公司", "职业")):
        return "work_experience"
    if any(word in text for word in ("我认为", "我觉得", "原则", "重大决定", "处世")):
        return "life_wisdom"
    if any(word in text for word in ("小时候", "曾经", "毕业", "出生", "结婚", "那一年")):
        return "life_story"
    return "daily_life"


class RuleBasedMemoryExtractor:
    version = "rules-zh-v2"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        text = str(event.payload.get("text") or "").strip()
        if not text:
            return MemoryExtraction(extractor_version=self.version)
        explicit_content = explicit_remember_content(text)
        if explicit_content is not None:
            text = explicit_content
        category = _category(text)
        explicit_predicate = (
            low_risk_self_fact_predicate(text) if explicit_content is not None else None
        )
        people: list[ExtractedPerson] = []
        relationships: list[ExtractedRelationship] = []
        claims: list[ExtractedClaim] = [
            ExtractedClaim(
                domain_category=category,
                subject_key="self",
                predicate=explicit_predicate or category,
                value=text,
                confidence=0.95 if explicit_content is not None else 0.62,
                valid_from=event.occurred_at,
            )
        ]
        for match in _PERSON.finditer(text):
            relation_raw = match.group("relation")
            relationship, relation_aliases = _RELATION_ALIASES[relation_raw]
            name = match.group("name")
            canonical_key = f"{relationship}:{name.casefold()}"
            people.append(
                ExtractedPerson(
                    display_name=name,
                    relationship_to_owner=relationship,
                    canonical_key=canonical_key,
                    aliases=tuple(dict.fromkeys((*relation_aliases, name))),
                )
            )
            relationships.append(
                ExtractedRelationship(
                    person_key=canonical_key,
                    relationship_type=relationship,
                )
            )
            age = _AGE.search(text[match.end() :])
            if age is not None:
                claims.append(
                    ExtractedClaim(
                        domain_category="life_story",
                        subject_key=canonical_key,
                        predicate="age",
                        value=age.group("age"),
                        confidence=0.9,
                        entity_keys=(canonical_key,),
                        valid_from=event.occurred_at,
                        salience=0.7,
                    )
                )
        if len(people) == 1:
            known = {alias.casefold() for alias in people[0].aliases}
            for match in _THIRD_PARTY_ALIAS.finditer(text):
                alias = match.group("alias").strip()
                if (
                    len(alias) < 2
                    or alias in _ROLE_WORDS
                    or alias.casefold() in known
                ):
                    continue
                known.add(alias.casefold())
                people[0] = replace(
                    people[0],
                    aliases=tuple((*people[0].aliases, alias)),
                )
        knowledge = (
            (
                ExtractedKnowledge(
                    domain_category=category,
                    question="这段经历或原则是什么？",
                    answer=text,
                    entity_keys=tuple(person.canonical_key for person in people),
                ),
            )
            if category != "daily_life"
            else ()
        )
        return MemoryExtraction(
            claims=tuple(claims),
            people=tuple(people),
            relationships=tuple(relationships),
            timeline=(
                ExtractedTimeline(
                    title=text[:60],
                    domain_category=category,
                    event_start=event.occurred_at,
                    participant_keys=tuple(person.canonical_key for person in people),
                ),
            ),
            knowledge=knowledge,
            extractor_version=self.version,
        )
