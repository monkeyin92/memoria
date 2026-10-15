from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import httpx

DECISION_MODEL_NAME: Final = "decision-model-preview"
_MAX_QUESTIONS: Final = 16
_MAX_STATE_CHARS: Final = 8_000

LIVE_LOOKUP_QUESTION: Final[dict[str, Any]] = {
    "type": "choice",
    "instructions": (
        "根据说话人这句话本身的意图，判断它想得到哪一类信息。注意：句子里出现天气、新闻、股票、汇率、交通等词，"
        "不代表需要最新数据；只有明确想知道现在或近期才变化的具体情况，才算需要实时信息。"
    ),
    "criteria": {
        "realtime": (
            "想知道此刻或近期才有的最新具体情况，必须联网查询才能回答，例如今明天的天气气温、最新新闻与比分、"
            "当前股价汇率、路况、车次航班时刻、近期活动与最新发布。"
        ),
        "knowledge": (
            "询问原理、定义、概念、成因、历史或固定常识，答案不随时间变化，即使话题涉及天气、金融、新闻、交通等"
            "也属于这一类，例如询问闪电的成因或基金的含义。"
        ),
        "chat": (
            "打招呼、闲聊、讲故事、唱歌、猜谜、回忆、表达心情、请求陪伴，以及关于说话人自己或家人的个人问题。"
        ),
    },
}

CLOSE_INTENT_QUESTION: Final[dict[str, Any]] = {
    "type": "choice",
    "instructions": (
        "根据说话人这句话本身的意图，判断说话人是否自己想结束这次与助手的对话。"
        "只是提到或询问告别用语、替别人告别、或还想继续聊，都不算结束。"
    ),
    "criteria": {
        "end_now": (
            "说话人自己明确想结束对话：告别、让助手退下或休息、说先不聊了或不用陪了、因为要去做别的事而结束聊天。"
        ),
        "mention_only": (
            "只是在提到、询问或谈论告别用语本身（含义、写法、怎么说、相关的歌曲或游戏），或转述别人的告别，"
            "说话人并没有结束对话的意思。"
        ),
        "continue": (
            "普通聊天、提问、讲故事、问候、换话题、请助手留下或继续陪伴，或只是表示知道了、让助手继续说。"
        ),
    },
}

CRISIS_GRADE_QUESTION: Final[dict[str, Any]] = {
    "type": "choice",
    "instructions": (
        "你只为当前一句话提供危机语义证据，不执行回复、通知或分级。"
        "SELF_CRISIS：说话人本人明确表达自伤、轻生、已采取行动或马上行动。"
        "SUPPORT_FOR_OTHER：替他人求助、引用他人的话或一般性咨询。"
        "NO_CRISIS：没有上述语义。"
        "UNSURE：上下文不足，不能可靠判断。"
    ),
    "criteria": {
        "SELF_CRISIS": "说话人本人明确表达自伤、轻生、已采取行动或马上行动。",
        "SUPPORT_FOR_OTHER": "替他人求助、引用他人的话或一般性咨询。",
        "NO_CRISIS": "没有自伤、轻生或他人危机求助语义。",
        "UNSURE": "上下文不足，不能可靠判断。",
    },
}

TURN_BUNDLE_QUESTIONS: Final[dict[str, Any]] = {
    "lookup_type": LIVE_LOOKUP_QUESTION,
    "close_type": CLOSE_INTENT_QUESTION,
    "crisis_grade": CRISIS_GRADE_QUESTION,
}


class DecisionModelDisabled(RuntimeError):
    pass


class CrisisChoice(StrEnum):
    SELF_CRISIS = "SELF_CRISIS"
    SUPPORT_FOR_OTHER = "SUPPORT_FOR_OTHER"
    NO_CRISIS = "NO_CRISIS"
    UNSURE = "UNSURE"


@dataclass(frozen=True, slots=True)
class DecisionModelConfig:
    api_key: str
    systemone_url: str
    enabled: bool = False
    model: str = DECISION_MODEL_NAME
    timeout_s: float = 1.2
    confidence_floor: float = 0.5
    # API reference uses Bearer; 2026-10-03 bridge scripts used the raw key.
    auth_style: str = "bearer"

    def __post_init__(self) -> None:
        if self.enabled and not self.api_key.strip():
            raise ValueError("decision model requires an API key when enabled")
        if self.enabled and not self.systemone_url.strip():
            raise ValueError("decision model requires a systemone URL when enabled")
        if self.timeout_s <= 0:
            raise ValueError("decision model timeout must be positive")
        if not 0.0 <= self.confidence_floor <= 1.0:
            raise ValueError("confidence_floor must be in [0, 1]")
        if self.auth_style not in {"bearer", "raw"}:
            raise ValueError("auth_style must be 'bearer' or 'raw'")


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    confidence: float | None
    probabilities: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class TurnBundleVerdict:
    live_lookup: ChoiceAnswer
    close_intent: ChoiceAnswer
    crisis_grade: ChoiceAnswer
    latency_ms: float
    server_latency_ms: float | None
    input_tokens: int | None
    raw_answers: Mapping[str, Any]


def parse_choice_answer(raw: Mapping[str, Any] | None) -> ChoiceAnswer:
    if not isinstance(raw, Mapping):
        return ChoiceAnswer(choice="UNSURE", confidence=None, probabilities={})
    choice = raw.get("choice")
    probs_raw = raw.get("probabilities")
    probabilities: dict[str, float] = {}
    if isinstance(probs_raw, Mapping):
        for key, value in probs_raw.items():
            try:
                probabilities[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    confidence: float | None
    try:
        confidence = float(raw["confidence"]) if "confidence" in raw else None
    except (TypeError, ValueError, KeyError):
        confidence = None
    if not isinstance(choice, str) or not choice:
        choice = max(probabilities, key=probabilities.get) if probabilities else "UNSURE"
    return ChoiceAnswer(choice=choice, confidence=confidence, probabilities=probabilities)


def needs_live_lookup(answer: ChoiceAnswer, *, confidence_floor: float) -> bool:
    if answer.choice != "realtime":
        return False
    if answer.confidence is None:
        return True
    return answer.confidence >= confidence_floor


def ends_conversation(answer: ChoiceAnswer, *, confidence_floor: float) -> bool:
    if answer.choice != "end_now":
        return False
    if answer.confidence is None:
        return True
    return answer.confidence >= confidence_floor


def crisis_choice(answer: ChoiceAnswer) -> CrisisChoice:
    try:
        return CrisisChoice(answer.choice)
    except ValueError:
        return CrisisChoice.UNSURE


def low_confidence(answer: ChoiceAnswer, *, confidence_floor: float) -> bool:
    if answer.confidence is None:
        return False
    return answer.confidence < confidence_floor


class DecisionModelClient:
    def __init__(
        self,
        config: DecisionModelConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def decide(
        self,
        *,
        state: str,
        questions: Mapping[str, Any],
    ) -> tuple[dict[str, Any], float]:
        if not self._config.enabled:
            raise DecisionModelDisabled(
                "decision-model adapter is disabled (default). "
                "Set enabled=True only for offline eval or an explicit shadow flag."
            )
        if len(questions) > _MAX_QUESTIONS:
            raise ValueError(f"System One accepts at most {_MAX_QUESTIONS} questions")
        body = {
            "model": self._config.model,
            "state": (state or "").strip()[-_MAX_STATE_CHARS:],
            "questions": dict(questions),
        }
        auth = (
            f"Bearer {self._config.api_key}"
            if self._config.auth_style == "bearer"
            else self._config.api_key
        )
        headers = {
            "Authorization": auth,
            "Content-Type": "application/json",
        }
        t0 = time.perf_counter()
        response = await asyncio.wait_for(
            self._client.post(
                self._config.systemone_url,
                headers=headers,
                json=body,
            ),
            timeout=self._config.timeout_s,
        )
        wall_ms = (time.perf_counter() - t0) * 1000.0
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("decision model response is not an object")
        return payload, wall_ms

    async def classify_turn_bundle(self, *, current_text: str) -> TurnBundleVerdict:
        payload, wall_ms = await self.decide(
            state=current_text,
            questions=TURN_BUNDLE_QUESTIONS,
        )
        answers = payload.get("answers")
        if not isinstance(answers, Mapping):
            answers = {}
        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        tokens = usage.get("input_tokens") if isinstance(usage, Mapping) else None
        server_ms = payload.get("latency_ms")
        try:
            server_latency = float(server_ms) if server_ms is not None else None
        except (TypeError, ValueError):
            server_latency = None
        try:
            input_tokens = int(tokens) if tokens is not None else None
        except (TypeError, ValueError):
            input_tokens = None
        return TurnBundleVerdict(
            live_lookup=parse_choice_answer(
                answers.get("lookup_type") if isinstance(answers, Mapping) else None
            ),
            close_intent=parse_choice_answer(
                answers.get("close_type") if isinstance(answers, Mapping) else None
            ),
            crisis_grade=parse_choice_answer(
                answers.get("crisis_grade") if isinstance(answers, Mapping) else None
            ),
            latency_ms=wall_ms,
            server_latency_ms=server_latency,
            input_tokens=input_tokens,
            raw_answers=dict(answers) if isinstance(answers, Mapping) else {},
        )
