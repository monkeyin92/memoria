"""Conservative promotion policy for explicit owner memory requests."""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import MemoryExtraction
from services.common.evidence_policy import contribution_for
from services.common.redaction import redact_pii

EXPLICIT_MEMORY_POLICY_VERSION = "explicit-memory-v2"
EXPLICIT_MEMORY_CONFIRM_REASON = "explicit-memory-low-risk"
EXPLICIT_MEMORY_INTENT = {
    "kind": "explicit_remember",
    "policy_version": EXPLICIT_MEMORY_POLICY_VERSION,
}
POLICY_CONFIRMATION_SOURCE = "system.memory_write_policy"
SINGLE_VALUE_PREDICATES = frozenset({"age", "birth_date", "birth_place"})
LOW_RISK_AUTO_CONFIRM_PREDICATES = frozenset({"preference", "habit"})

_EXPLICIT_REMEMBER = re.compile(
    r"^(?:请帮我|请|帮我)记住(?:一下)?(?:这件事)?(?:[：:,，]\s*|\s+)?(?P<content>.+)$"
)
_SENSITIVE_TERMS = (
    "身份证",
    "银行卡",
    "手机号",
    "家庭住址",
    "邮箱",
    "密码",
    "验证码",
    "病史",
    "诊断",
    "过敏",
    "血压",
    "血糖",
    "手术",
    "抑郁",
    "焦虑",
    "收入",
    "工资",
    "资产",
    "负债",
    "贷款",
    "诉讼",
    "案件",
    "犯罪",
    "判决",
    "声纹",
    "指纹",
    "人脸",
    "虹膜",
    "妈妈",
    "爸爸",
    "父母",
    "妻子",
    "丈夫",
    "伴侣",
    "朋友",
    "同事",
    "家人",
    "离婚",
    "婚外",
)
_LOW_RISK_TEMPLATES = (
    (
        "preference",
        re.compile(r"^我(?:最|很|非常|比较|更)?(?:喜欢|不喜欢|偏好)(?P<value>.+)$"),
    ),
    (
        "habit",
        re.compile(r"^我(?:平时|通常|一直)?习惯(?P<value>.+)$"),
    ),
)
_LOW_RISK_VALUE_TOKENS = (
    "散步",
    "跑步",
    "运动",
    "阅读",
    "看书",
    "音乐",
    "电影",
    "旅行",
    "咖啡",
    "茶",
    "烹饪",
    "做饭",
    "每天",
    "早起",
    "早睡",
    "日记",
    "写作",
    "绘画",
    "摄影",
    "园艺",
    "植物",
    "宠物",
    "游戏",
    "晴天",
    "雨天",
    "颜色",
    "蓝色",
    "绿色",
    "红色",
    "清淡",
    "甜食",
)
_LOW_RISK_VALUE_SEQUENCE = re.compile(
    "(?:"
    + "|".join(re.escape(token) for token in sorted(_LOW_RISK_VALUE_TOKENS, key=len, reverse=True))
    + ")+"
)
_SENSITIVE_SELF_FACT = re.compile(
    r"(?:出生|生日|年龄|年纪|\d{1,3}\s*岁|"
    r"\d{4}\s*(?:年|[-/.])\s*\d{1,2}|"
    r"\d{1,2}\s*月\s*\d{1,2}\s*日)"
)


def explicit_remember_content(text: object) -> str | None:
    """Return the requested fact for a strict sentence-initial remember command."""

    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not value or value.endswith(("?", "？")):
        return None
    match = _EXPLICIT_REMEMBER.fullmatch(value)
    if match is None:
        return None
    content = match.group("content").strip()
    question_probe = content.rstrip("。.!！").strip()
    if not content.strip(" \t\r\n:：,，。.!！\"'“”‘’") or question_probe.endswith("吗"):
        return None
    return content


def has_exact_explicit_memory_intent(payload: Mapping[str, object]) -> bool:
    raw = payload.get("memory_write_intent")
    return isinstance(raw, Mapping) and dict(raw) == EXPLICIT_MEMORY_INTENT


def is_policy_confirmation_event(event: EvidenceEvent) -> bool:
    payload = event.payload
    return bool(
        event.event_type == "memory.claim_reviewed"
        and event.source == POLICY_CONFIRMATION_SOURCE
        and payload.get("action") == "confirm"
        and payload.get("policy_version") == EXPLICIT_MEMORY_POLICY_VERSION
        and payload.get("reason") == EXPLICIT_MEMORY_CONFIRM_REASON
        and str(payload.get("target_id") or "").strip()
        and str(payload.get("source_event_id") or "").strip()
    )


def _normalized_fact(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def _traceable_to(value: str, source: str) -> bool:
    fact = _normalized_fact(value)
    original = _normalized_fact(source)
    return len(fact) >= 2 and bool(original) and (fact in original or original in fact)


def _contains_sensitive_text(value: str) -> bool:
    return redact_pii(value) != value or any(term in value for term in _SENSITIVE_TERMS)


def _is_closed_low_risk_value(value: str) -> bool:
    segments = re.split(r"[、，,和与或]", re.sub(r"\s+", "", value))
    return bool(segments) and all(
        segment and _LOW_RISK_VALUE_SEQUENCE.fullmatch(segment) for segment in segments
    )


def low_risk_self_fact_predicate(value: object) -> str | None:
    """Return the only server-verifiable facts eligible for auto-confirmation."""

    content = unicodedata.normalize("NFKC", str(value or "")).strip().rstrip("。.!！?")
    if not content or _contains_sensitive_text(content) or _SENSITIVE_SELF_FACT.search(content):
        return None
    for predicate, template in _LOW_RISK_TEMPLATES:
        match = template.fullmatch(content)
        if match is None:
            continue
        fact_value = match.group("value").strip()
        if _is_closed_low_risk_value(fact_value):
            return predicate
    return None


def _complete_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True, slots=True)
class MemoryWriteDecision:
    confirmed: bool
    reason: str
    content: str | None = None

    def confirmation_event(
        self,
        *,
        source: EvidenceEvent,
        claim_id: str,
        claim_value: str,
    ) -> EvidenceEvent:
        if not self.confirmed:
            raise ValueError("candidate memory decisions cannot create confirmation evidence")
        event_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    "memoria:memory-write-policy:"
                    f"{EXPLICIT_MEMORY_POLICY_VERSION}:{source.event_id}:{claim_id}"
                ),
            )
        )
        return EvidenceEvent(
            event_id=event_id,
            account_id=source.account_id,
            session_id=source.session_id,
            turn_id=source.turn_id,
            generation_id=source.generation_id,
            event_type="memory.claim_reviewed",
            occurred_at=source.occurred_at,
            speaker_class="system",
            source=POLICY_CONFIRMATION_SOURCE,
            payload={
                "target_id": claim_id,
                "action": "confirm",
                "previous_value": claim_value,
                "source_event_id": source.event_id,
                "policy_version": EXPLICIT_MEMORY_POLICY_VERSION,
                "reason": self.reason,
                "tool_epoch": source.payload.get("tool_epoch"),
            },
        )


class MemoryWritePolicy:
    """Promote only explicit, attributable, low-risk single-owner facts."""

    def decide(
        self,
        event: EvidenceEvent,
        extraction: MemoryExtraction,
        *,
        existing_values: Collection[str] = (),
    ) -> MemoryWriteDecision:
        if not has_exact_explicit_memory_intent(event.payload):
            return MemoryWriteDecision(False, "explicit_intent_missing")
        contribution = contribution_for(event)
        if (
            not contribution.accepted
            or event.event_type != "speech.utterance_finalized"
            or not (event.session_id or "").strip()
            or not _complete_nonnegative_int(event.turn_id)
            or not _complete_nonnegative_int(event.generation_id)
            or not _complete_nonnegative_int(event.payload.get("tool_epoch"))
        ):
            return MemoryWriteDecision(False, "untrusted_owner_fence")
        content = explicit_remember_content(event.payload.get("text"))
        if content is None:
            return MemoryWriteDecision(False, "invalid_explicit_command")
        low_risk_predicate = low_risk_self_fact_predicate(content)
        if low_risk_predicate is None:
            return MemoryWriteDecision(False, "auto_confirm_not_allowlisted", content)
        if len(extraction.claims) != 1:
            return MemoryWriteDecision(False, "single_claim_required", content)
        claim = extraction.claims[0]
        if (
            claim.subject_key != "self"
            or claim.entity_keys
            or extraction.people
            or extraction.relationships
            or any(item.participant_keys for item in extraction.timeline)
            or any(item.entity_keys for item in extraction.knowledge)
        ):
            return MemoryWriteDecision(False, "self_claim_required", content)
        if (
            claim.predicate not in LOW_RISK_AUTO_CONFIRM_PREDICATES
            or claim.predicate != low_risk_predicate
        ):
            return MemoryWriteDecision(False, "auto_confirm_predicate_mismatch", content)
        if claim.confidence * contribution.factor < 0.9:
            return MemoryWriteDecision(False, "insufficient_confidence", content)
        if not _traceable_to(claim.value, content):
            return MemoryWriteDecision(False, "claim_not_traceable", content)
        sensitivity_values = (
            claim.sensitive_domain,
            *(item.sensitivity for item in extraction.timeline),
            *(item.sensitivity for item in extraction.knowledge),
        )
        text_values = (
            str(event.payload.get("text") or ""),
            content,
            claim.value,
            *(item.title for item in extraction.timeline),
            *(item.question for item in extraction.knowledge),
            *(item.answer for item in extraction.knowledge),
        )
        if any(
            str(value).strip().casefold() not in {"public", "personal"}
            for value in sensitivity_values
        ):
            return MemoryWriteDecision(False, "sensitive_or_unknown_domain", content)
        if any(_contains_sensitive_text(value) for value in text_values):
            return MemoryWriteDecision(False, "sensitive_content", content)
        if claim.predicate in SINGLE_VALUE_PREDICATES and any(
            str(value) != claim.value for value in existing_values
        ):
            return MemoryWriteDecision(False, "conflicting_value", content)
        return MemoryWriteDecision(True, EXPLICIT_MEMORY_CONFIRM_REASON, content)
