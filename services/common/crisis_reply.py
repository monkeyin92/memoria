"""Guarded crisis reply. The three scripts are the fallback, not the first line."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from services.common.fixed_safety_scripts import FixedSafetyScript, speak_hotline_digits

CrisisReplyKind = Literal["dynamic", "fallback"]
CRISIS_GUARD_PASSED = "crisis_guard_passed"
CRISIS_REPLY_BUDGET_S = 6.0

_TIER_BY_TEMPLATE = {
    "crisis-self-v1": "crisis_self",
    "victim-support-v1": "support_victim",
    "support-other-v1": "support_relay",
}
_ADULT = ("大人", "爸爸", "妈妈", "老师")
_SECRET = (
    "保密",
    "不要告诉",
    "别告诉",
    "不告诉",
    "帮你瞒",
    "替你瞒",
    "不会告诉",
    "这是我们的秘密",
    "不跟爸爸妈妈说",
)
_METHODS = (
    "割腕",
    "割脉",
    "上吊",
    "跳楼",
    "烧炭",
    "服毒",
    "怎么自杀",
    "如何自杀",
    "自杀方法",
    "自残方法",
    "吃药自杀",
)
_SENTENCE_END = re.compile(r"[。！？]")
_MAX_SENTENCES = 3
_MAX_CHARS = 240


@dataclass(frozen=True, slots=True)
class CrisisSpoken:
    text: str
    kind: CrisisReplyKind
    template_id: str


class CrisisReplyModel(Protocol):
    async def generate(
        self,
        *,
        query: str,
        tier: str,
        failures: tuple[str, ...],
    ) -> str | None: ...

    async def accept(self, *, query: str, tier: str, reply: str) -> bool: ...


def rule_failures(tier: str, reply: str) -> tuple[str, ...]:
    """Hard constraints a crisis line must already satisfy before anyone hears it."""

    text = reply.strip()
    failures: list[str] = []
    if not text:
        return ("empty",)
    if len(text) > _MAX_CHARS or _sentence_count(text) > _MAX_SENTENCES:
        failures.append("too_long")
    if not any(word in text for word in _ADULT):
        failures.append("adult")
    if tier == "crisis_self":
        if "幺二三五六" not in text:
            failures.append("hotline_12356")
        if "幺二零" not in text:
            failures.append("hotline_120")
    elif tier == "support_victim":
        if "幺幺零" not in text:
            failures.append("hotline_110")
    if any(phrase in text for phrase in _SECRET):
        failures.append("confidentiality")
    if any(phrase in text for phrase in _METHODS):
        failures.append("method")
    return tuple(failures)


def _sentence_count(text: str) -> int:
    return sum(1 for part in _SENTENCE_END.split(text) if part.strip())


async def resolve_crisis_reply(
    *,
    query: str,
    script: FixedSafetyScript,
    model: CrisisReplyModel | None,
    budget_s: float = CRISIS_REPLY_BUDGET_S,
) -> CrisisSpoken:
    """One generation, one correction, then the tier's short script."""

    fallback = CrisisSpoken(script.display, "fallback", script.template_id)
    if model is None:
        return fallback
    try:
        spoken = await asyncio.wait_for(
            _resolve(query, script, model),
            timeout=budget_s,
        )
    except TimeoutError:
        return fallback
    return spoken


async def _resolve(
    query: str,
    script: FixedSafetyScript,
    model: CrisisReplyModel,
) -> CrisisSpoken:
    fallback = CrisisSpoken(script.display, "fallback", script.template_id)
    tier = _TIER_BY_TEMPLATE[script.template_id]
    draft = await _draft(model, query, tier, ())
    if draft is None:
        return fallback
    failures = await _review(model, query, tier, draft)
    if not failures:
        return _spoken(draft, script)
    if failures == ("semantic_unavailable",):
        return fallback
    corrected = await _draft(model, query, tier, failures)
    if corrected is None:
        return fallback
    if await _review(model, query, tier, corrected):
        return fallback
    return _spoken(corrected, script)


def _spoken(text: str, script: FixedSafetyScript) -> CrisisSpoken:
    if text == script.display or text == script.spoken:
        return CrisisSpoken(script.display, "fallback", script.template_id)
    return CrisisSpoken(text, "dynamic", script.template_id)


async def _draft(
    model: CrisisReplyModel,
    query: str,
    tier: str,
    failures: tuple[str, ...],
) -> str | None:
    raw = await model.generate(query=query, tier=tier, failures=failures)
    if raw is None:
        return None
    text = speak_hotline_digits(raw).strip()
    return text or None


async def _review(
    model: CrisisReplyModel,
    query: str,
    tier: str,
    reply: str,
) -> tuple[str, ...]:
    failures = rule_failures(tier, reply)
    if failures:
        return failures
    try:
        accepted = await model.accept(query=query, tier=tier, reply=reply)
    except Exception:
        return ("semantic_unavailable",)
    if accepted:
        return ()
    return ("semantic_reject",)
