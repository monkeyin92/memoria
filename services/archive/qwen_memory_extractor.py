"""Strict Qwen JSON extraction boundary with a deterministic local fallback."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import (
    DomainCategory,
    ExtractedClaim,
    ExtractedKnowledge,
    ExtractedPerson,
    ExtractedRelationship,
    ExtractedTimeline,
    ExtractionUsage,
    MemoryExtraction,
    MemoryExtractor,
    MemorySensitivity,
)
from services.archive.memory_write_policy import (
    explicit_remember_content,
    low_risk_self_fact_predicate,
)


class MemoryExtractionError(RuntimeError):
    pass


class _ClaimPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    domain_category: DomainCategory = Field(
        validation_alias=AliasChoices("domain_category", "category")
    )
    subject_key: str = Field(min_length=1, max_length=128)
    predicate: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=8000)
    confidence: float = Field(ge=0, le=1)
    sensitive_domain: str = Field(default="personal", min_length=1, max_length=64)
    entity_keys: list[str] = Field(default_factory=list, max_length=20)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    salience: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> _ClaimPayload:
        for value in (self.valid_from, self.valid_to):
            if value is not None and value.tzinfo is None:
                raise ValueError("claim timestamps must include timezone")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("claim valid_to must not precede valid_from")
        return self


class _PersonPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=128)
    relationship_to_owner: str = Field(min_length=1, max_length=64)
    canonical_key: str = Field(min_length=1, max_length=160)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class _RelationshipPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    person_key: str = Field(min_length=1, max_length=160)
    relationship_type: str = Field(min_length=1, max_length=64)


class _TimelinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=500)
    domain_category: DomainCategory = Field(
        validation_alias=AliasChoices("domain_category", "category")
    )
    event_start: datetime | None = None
    event_end: datetime | None = None
    time_precision: Literal[
        "exact",
        "day",
        "month",
        "year",
        "approximate",
        "conversation_time",
    ] = "conversation_time"
    canonical_key: str = Field(default="", max_length=200)
    participant_keys: list[str] = Field(default_factory=list, max_length=20)
    salience: float = Field(default=0.6, ge=0, le=1)
    sensitivity: MemorySensitivity = "personal"

    @model_validator(mode="after")
    def validate_time_range(self) -> _TimelinePayload:
        for value in (self.event_start, self.event_end):
            if value is not None and value.tzinfo is None:
                raise ValueError("timeline timestamps must include timezone")
        if self.event_start and self.event_end and self.event_end < self.event_start:
            raise ValueError("timeline event_end must not precede event_start")
        return self


class _KnowledgePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    domain_category: DomainCategory = Field(
        validation_alias=AliasChoices("domain_category", "category")
    )
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=8000)
    applicability: str = Field(default="", max_length=2000)
    counterexample: str = Field(default="", max_length=2000)
    entity_keys: list[str] = Field(default_factory=list, max_length=20)
    salience: float = Field(default=0.55, ge=0, le=1)
    sensitivity: MemorySensitivity = "personal"


class _ExtractionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[_ClaimPayload] = Field(default_factory=list, max_length=30)
    people: list[_PersonPayload] = Field(default_factory=list, max_length=20)
    relationships: list[_RelationshipPayload] = Field(default_factory=list, max_length=30)
    timeline: list[_TimelinePayload] = Field(default_factory=list, max_length=10)
    knowledge: list[_KnowledgePayload] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_entity_references(self) -> _ExtractionPayload:
        person_keys = {person.canonical_key for person in self.people}
        if len(person_keys) != len(self.people):
            raise ValueError("person canonical_key values must be unique per extraction")
        if any(item.person_key not in person_keys for item in self.relationships):
            raise ValueError("relationship references an unknown person_key")
        valid_subjects = {"self", *person_keys}
        if any(claim.subject_key not in valid_subjects for claim in self.claims):
            raise ValueError("claim references an unknown subject_key")
        referenced_keys = (
            {key for claim in self.claims for key in claim.entity_keys}
            | {key for timeline in self.timeline for key in timeline.participant_keys}
            | {key for knowledge in self.knowledge for key in knowledge.entity_keys}
        )
        if not referenced_keys <= person_keys:
            raise ValueError("memory projection references an unknown person_key")
        return self


def _prompt(text: str, occurred_at: datetime) -> str:
    return f"""
从下面的账户主人原话中提取可审核的长期记忆候选。只输出 JSON 对象，不要 Markdown。
不得补充原话没有的信息；不确定时少提取或返回空数组。人物 canonical_key 使用
"关系英文:姓名"，主人本人使用 subject_key="self"。所有结论只是 candidate。

domain_category 只能是：life_story、work_experience、family_principle、
parenting_principle、life_wisdom、daily_life。它只表示主题，不表示记忆种类。
时间不明确时 event_start 设为 null，time_precision 使用 conversation_time。
同一现实事件跨会话再次出现时使用同一个简短 canonical_key；不能确认时留空。
entity_keys/participant_keys 只能引用本次 people 的 canonical_key。
适用条件和反例没有证据时必须为空字符串。
对于句首“请/帮我记住……”后的低风险偏好或习惯，且只有原文明确为非敏感日常偏好/习惯时，
claim predicate 使用 preference 或 habit；出生日期、年龄、地点、关系、健康、金融、法律、
生物特征或不确定内容不能使用这两个 predicate。

严格结构：
{{
  "claims": [{{"domain_category":"daily_life","subject_key":"self","predicate":"...",
    "value":"...","confidence":0.0,"sensitive_domain":"personal",
    "entity_keys":[],"valid_from":null,"valid_to":null,"salience":0.5}}],
  "people": [{{"display_name":"...","relationship_to_owner":"mother",
    "canonical_key":"mother:姓名","aliases":["..."]}}],
  "relationships": [{{"person_key":"mother:姓名","relationship_type":"mother"}}],
  "timeline": [{{"title":"...","domain_category":"daily_life","event_start":null,
    "event_end":null,"time_precision":"conversation_time","canonical_key":"",
    "participant_keys":[],"salience":0.6,"sensitivity":"personal"}}],
  "knowledge": [{{"domain_category":"life_wisdom","question":"...","answer":"...",
    "applicability":"","counterexample":"","entity_keys":[],"salience":0.55,
    "sensitivity":"personal"}}]
}}

对话发生时间：{occurred_at.isoformat()}
账户主人原话：{text[:12000]}
""".strip()


class QwenMemoryExtractor:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_s: float = 20.0,
        workspace_id: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Qwen memory extraction requires an API key")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Qwen memory extraction requires an HTTP base URL")
        if not model.strip() or not 1 <= timeout_s <= 120:
            raise ValueError("Qwen memory extraction requires a model and timeout 1..120s")
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model.strip()
        self._timeout_s = timeout_s
        self._workspace_id = workspace_id.strip()
        self._transport = transport
        self.version = f"qwen-json:{self._model}:v1"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        text = str(event.payload.get("text") or "").strip()
        if not text:
            return MemoryExtraction(extractor_version=self.version)
        explicit_content = explicit_remember_content(text)
        text = explicit_content or text
        explicit_predicate = (
            low_risk_self_fact_predicate(text) if explicit_content is not None else None
        )
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if self._workspace_id:
            headers["X-DashScope-WorkSpace"] = self._workspace_id
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是保守的中文人生档案抽取器，只返回严格 JSON。",
                },
                {"role": "user", "content": _prompt(text, event.occurred_at)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 2400,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s,
                transport=self._transport,
            ) as client:
                response = await client.post(self._endpoint, headers=headers, json=payload)
                response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("Qwen returned non-text extraction content")
            parsed = _ExtractionPayload.model_validate_json(content)
            raw_usage = body.get("usage") or {}
            usage = ExtractionUsage(
                input_tokens=max(0, int(raw_usage.get("prompt_tokens") or 0)),
                output_tokens=max(0, int(raw_usage.get("completion_tokens") or 0)),
            )
        except (
            httpx.HTTPError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            raise MemoryExtractionError("Qwen returned an invalid memory extraction") from exc

        return MemoryExtraction(
            claims=tuple(
                ExtractedClaim(
                    domain_category=item.domain_category,
                    subject_key=item.subject_key,
                    predicate=(
                        explicit_predicate
                        if explicit_predicate is not None and item.subject_key == "self"
                        else item.predicate
                    ),
                    value=item.value,
                    confidence=item.confidence,
                    sensitive_domain=item.sensitive_domain,
                    entity_keys=tuple(item.entity_keys),
                    valid_from=item.valid_from,
                    valid_to=item.valid_to,
                    salience=item.salience,
                )
                for item in parsed.claims
            ),
            people=tuple(
                ExtractedPerson(
                    display_name=item.display_name,
                    relationship_to_owner=item.relationship_to_owner,
                    canonical_key=item.canonical_key,
                    aliases=tuple(dict.fromkeys((item.display_name, *item.aliases))),
                )
                for item in parsed.people
            ),
            relationships=tuple(
                ExtractedRelationship(
                    person_key=item.person_key,
                    relationship_type=item.relationship_type,
                )
                for item in parsed.relationships
            ),
            timeline=tuple(
                ExtractedTimeline(
                    title=item.title,
                    domain_category=item.domain_category,
                    event_start=item.event_start or event.occurred_at,
                    event_end=item.event_end,
                    time_precision=item.time_precision,
                    canonical_key=item.canonical_key,
                    participant_keys=tuple(item.participant_keys),
                    salience=item.salience,
                    sensitivity=item.sensitivity,
                )
                for item in parsed.timeline
            ),
            knowledge=tuple(
                ExtractedKnowledge(
                    domain_category=item.domain_category,
                    question=item.question,
                    answer=item.answer,
                    applicability=item.applicability,
                    counterexample=item.counterexample,
                    entity_keys=tuple(item.entity_keys),
                    salience=item.salience,
                    sensitivity=item.sensitivity,
                )
                for item in parsed.knowledge
            ),
            extractor_version=self.version,
            usage=usage,
        )


class FallbackMemoryExtractor:
    def __init__(self, primary: MemoryExtractor, fallback: MemoryExtractor) -> None:
        self._primary = primary
        self._fallback = fallback
        self.version = f"{primary.version}|fallback:{fallback.version}"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        try:
            return await self._primary.extract(event)
        except MemoryExtractionError:
            return await self._fallback.extract(event)
