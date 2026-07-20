"""Strict Qwen boundary for evidence-grounded persona candidates."""

from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.persona.domain import PersonaEvidence
from services.persona.rules import (
    PersonaCandidate,
    PersonaExtractor,
    extract_candidates,
)


class PersonaExtractionError(RuntimeError):
    pass


class _TraitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: Literal[
        "emphasis_style",
        "emotional_expression",
        "discourse_style",
        "narrative_style",
        "decision_habit",
        "value_priority",
    ]
    normalized_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    description: str = Field(min_length=2, max_length=2000)
    context: str = Field(min_length=2, max_length=2000)
    counterexample: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def require_decision_boundaries(self) -> _TraitPayload:
        if self.category in {"decision_habit", "value_priority"} and not self.counterexample:
            raise ValueError("decision and value traits require an evidenced counterexample")
        return self


class _ExtractionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    traits: list[_TraitPayload] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def reject_duplicate_traits(self) -> _ExtractionPayload:
        keys = {(trait.category, trait.normalized_key) for trait in self.traits}
        if len(keys) != len(self.traits):
            raise ValueError("persona trait keys must be unique per extraction")
        return self


def _prompt(text: str, evidence: PersonaEvidence) -> str:
    return f"""
从下面的账户主人原话中提取可审核的人格候选。只输出 JSON 对象，不要 Markdown。
不得补充原话没有的信息；不要把一次情绪压成永久性格；证据不足时少提取或返回空数组。
    只提取原话有明确证据的重音方式、情绪表达、表达组织、叙事方式、决策习惯和价值排序。
    不得把一次性情绪推断为稳定风格。决策习惯与价值排序只有在原话同时给出适用情境和
    例外/反例时才可提取；否则不要生成。normalized_key 使用简短稳定的英文 snake_case。
    所有结论只是 candidate。

严格结构：
{{
  "traits": [{{
    "category": "emphasis_style|emotional_expression|narrative_style|discourse_style|decision_habit|value_priority",
    "normalized_key": "...",
    "description": "...",
    "context": "原话中的具体适用情境",
    "counterexample": "原话中的例外或反例；非决策/价值类可为空"
  }}]
}}

当前场景：{evidence.scene}
账户主人原话：{text[:12000]}
""".strip()


class QwenPersonaExtractor:
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
            raise ValueError("Qwen persona extraction requires an API key")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Qwen persona extraction requires an HTTP base URL")
        if not model.strip() or not 1 <= timeout_s <= 120:
            raise ValueError("Qwen persona extraction requires a model and timeout 1..120s")
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model.strip()
        self._timeout_s = timeout_s
        self._workspace_id = workspace_id.strip()
        self._transport = transport
        self.version = f"qwen-persona-json:{self._model}:v2"

    async def extract(
        self,
        text: str,
        evidence: PersonaEvidence,
    ) -> tuple[PersonaCandidate, ...]:
        text = text.strip()
        if not text:
            return ()
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if self._workspace_id:
            headers["X-DashScope-WorkSpace"] = self._workspace_id
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是保守的中文人格证据抽取器，只返回严格 JSON。",
                },
                {"role": "user", "content": _prompt(text, evidence)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 1600,
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
                raise TypeError("Qwen returned non-text persona extraction content")
            parsed = _ExtractionPayload.model_validate_json(content)
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise PersonaExtractionError("Qwen returned an invalid persona extraction") from exc

        semantic = tuple(
            PersonaCandidate(
                category=trait.category,
                normalized_key=trait.normalized_key,
                description=trait.description,
                context=trait.context,
                counterexample=trait.counterexample,
            )
            for trait in parsed.traits
        )
        semantic_categories = {candidate.category for candidate in semantic}
        deterministic = tuple(
            candidate
            for candidate in extract_candidates(text, evidence)
            if candidate.category not in semantic_categories
        )
        return (*semantic, *deterministic)


class FallbackPersonaExtractor:
    def __init__(self, primary: PersonaExtractor, fallback: PersonaExtractor) -> None:
        self._primary = primary
        self._fallback = fallback
        self.version = f"{primary.version}|fallback:{fallback.version}"

    async def extract(
        self,
        text: str,
        evidence: PersonaEvidence,
    ) -> tuple[PersonaCandidate, ...]:
        try:
            return await self._primary.extract(text, evidence)
        except PersonaExtractionError:
            return await self._fallback.extract(text, evidence)
