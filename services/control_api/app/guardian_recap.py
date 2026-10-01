"""A transcript-free daily recap of what a bound child talked about, for the guardian.

The guardian sees summaries, trends and alerts -- never the child's own words (product
decision 2026-09-24/25). Until now the only guardian view was the weekly report, which counts
``emotion_observation`` / ``topic.observation`` events that nothing in production emits, and the
回顾 tab read a legacy messages table that production no longer writes. So a parent who bound a
device for a child saw an empty page for every conversation the child had.

This module turns the child's own archived, consented turns into a day list (counts only) and,
on request, a short recap written by the summary model from a prompt that forbids quoting. The
recap is computed per request and never stored: what is derived about a child must not outlive
the child's own data or escape the per-subject deletion. A recap that repeats a stretch of the
child's words, or cannot be produced, is replaced by a counts-only one.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

import httpx

from services.archive.domain import EvidenceEvent
from services.common.llm_thinking import ThinkingMode, thinking_disabled
from services.common.redaction import redact_pii
from services.control_api.app.config import ControlSettings

logger = logging.getLogger(__name__)

RecapSource = Literal["qwen", "deepseek", "fallback"]
MOODS: Final = ("positive", "calm", "mixed", "low", "tense")
#: A recap sharing this many consecutive characters with the child's words is a quote.
VERBATIM_WINDOW: Final = 10
_MAX_TRANSCRIPT_CHARS: Final = 12_000
_MAX_UTTERANCE_CHARS: Final = 300


@dataclass(frozen=True, slots=True)
class ChildUtterance:
    day: date
    text: str


def child_utterances(
    events: Iterable[EvidenceEvent], *, subject_id: str, zone: ZoneInfo
) -> list[ChildUtterance]:
    """The child's own consented turns, oldest first. Nothing else is ever read."""

    seen: set[str] = set()
    found: list[tuple[Any, ChildUtterance]] = []
    for event in events:
        payload = event.payload
        text = payload.get("text")
        if not (
            event.event_type == "speech.utterance_finalized"
            and event.speaker_class == "owner"
            and event.subject_id == subject_id
            and payload.get("history_eligible") is True
            and payload.get("owner_projection_eligible") is True
            and payload.get("memory_retention") != "ephemeral_only"
            and isinstance(text, str)
            and text.strip()
            and event.event_id not in seen
        ):
            continue
        seen.add(event.event_id)
        found.append(
            (
                event.occurred_at,
                ChildUtterance(
                    day=event.occurred_at.astimezone(zone).date(), text=text.strip()
                ),
            )
        )
    found.sort(key=lambda item: item[0])
    return [item for _, item in found]


def day_activity(utterances: Sequence[ChildUtterance], *, limit: int) -> list[dict[str, object]]:
    """``[{day, message_count}]`` newest first. Counts only: no text leaves this function."""

    counts = Counter(item.day for item in utterances)
    return [
        {"day": day.isoformat(), "message_count": counts[day]}
        for day in sorted(counts, reverse=True)[:limit]
    ]


def _prompt(day: date, utterances: Sequence[ChildUtterance]) -> str:
    lines: list[str] = []
    remaining = _MAX_TRANSCRIPT_CHARS
    for item in utterances:
        line = "- " + " ".join(item.text.split())[:_MAX_UTTERANCE_CHARS]
        if len(line) > remaining:
            break
        lines.append(line)
        remaining -= len(line)
    return f"""
请为家长概括 {day.isoformat()} 孩子和陪伴机器人的聊天。只输出一个 JSON 对象，不要 Markdown、解释或额外字段：
{{
  "title": "不超过30字的标题，例如“今天聊了学校和小狗”",
  "overview": "不超过200字，用“孩子”做主语，概括聊了哪些话题、整体心情",
  "highlights": ["1至4条，每条不超过40字，只写话题或情绪，不写原话"],
  "mood": "positive|calm|mixed|low|tense 五选一",
  "suggestion": "不超过100字，给家长的温和、可执行的建议；没有需要特别关注的，就写可以怎样陪伴孩子"
}}
硬性规则：
- 不得引用或复述孩子的原话，不得逐句照搬，用自己的话概括。
- 不要出现孩子的姓名、学校、班级、老师、住址、电话等能识别身份的信息。
- 只写聊天中出现的内容，不编造，不做心理诊断，不推测没说过的原因。
- 聊到被欺负、害怕、孤单、难过、想伤害自己等需要关心的情况时，如实而温和地写进 overview，
  并在 suggestion 里建议家长找个轻松的时间陪孩子聊聊，必要时联系老师或专业人士。

孩子当天说过的话（只供你理解，勿照搬）：
{chr(10).join(lines)}
""".strip()


def _copied_fragments(recap_text: str, utterances: Sequence[ChildUtterance]) -> list[str]:
    """Each run of the child's words that the recap repeats in VERBATIM_WINDOW-long pieces."""

    haystack = "".join(recap_text.split())
    fragments: list[str] = []
    for item in utterances:
        words = "".join(item.text.split())
        covered = [False] * len(words)
        for start in range(len(words) - VERBATIM_WINDOW + 1):
            if words[start : start + VERBATIM_WINDOW] in haystack:
                covered[start : start + VERBATIM_WINDOW] = [True] * VERBATIM_WINDOW
        run = ""
        for char, hit in zip(words, covered, strict=True):
            if hit:
                run += char
            elif run:
                fragments.append(run)
                run = ""
        if run:
            fragments.append(run)
    return fragments


def _clean(value: object, limit: int) -> str:
    return redact_pii(" ".join(str(value).split()))[:limit] if isinstance(value, str) else ""


def parse_recap(raw: object, utterances: Sequence[ChildUtterance]) -> dict[str, object] | None:
    """The model's JSON as the public recap, or ``None`` when it breaks a rule."""

    return _checked(raw, utterances)[0]


def _checked(
    raw: object, utterances: Sequence[ChildUtterance]
) -> tuple[dict[str, object] | None, list[str]]:
    """The public recap (``None`` when it breaks a rule) and the child's words it repeated."""

    if not isinstance(raw, str):
        return None, []
    try:
        body = json.loads(raw)
    except ValueError:
        return None, []
    if not isinstance(body, Mapping):
        return None, []
    title = _clean(body.get("title"), 30)
    overview = _clean(body.get("overview"), 200)
    suggestion = _clean(body.get("suggestion"), 100)
    raw_highlights = body.get("highlights")
    highlights = [
        cleaned
        for item in (raw_highlights if isinstance(raw_highlights, list) else [])[:4]
        if (cleaned := _clean(item, 40))
    ]
    mood = body.get("mood")
    if not title or not overview or mood not in MOODS:
        return None, []
    copied = _copied_fragments("".join((title, overview, suggestion, *highlights)), utterances)
    if copied:
        return None, copied
    return {
        "title": title,
        "overview": overview,
        "highlights": highlights,
        "mood": mood,
        "suggestion": suggestion,
    }, []


def _rewrite_prompt(copied: Sequence[str]) -> str:
    quoted = "、".join(f"“{fragment}”" for fragment in copied)
    return (
        f"这份概括照搬了孩子的原话：{quoted}。请按同样的 JSON 结构重写："
        "不要出现这些原话，也不要照搬孩子的其他原话，用自己的话只写话题和心情。"
    )


def fallback_recap(day: date, count: int) -> dict[str, object]:
    """Counts only: used when the model is unavailable or repeated the child's words."""

    return {
        "title": f"{day:%m月%d日}聊了 {count} 次",
        "overview": f"孩子这一天和机器人聊了 {count} 次。AI 暂时没能整理出概括，稍后可以再试一次。",
        "highlights": [],
        "mood": "calm",
        "suggestion": "",
    }


def _provider(
    settings: ControlSettings,
) -> tuple[str, str, str, ThinkingMode, float, RecapSource] | None:
    if settings.llm_provider in {"qwen", "bailian_deepseek"}:
        key = settings.dashscope_api_key.get_secret_value()
        if key:
            return (
                key,
                settings.dashscope_base_url,
                settings.dashscope_summary_model,
                "dashscope",
                settings.dashscope_summary_timeout_s,
                "qwen",
            )
    elif settings.llm_provider == "deepseek":
        key = settings.deepseek_api_key.get_secret_value()
        if key:
            return (
                key,
                settings.deepseek_base_url,
                settings.deepseek_summary_model,
                "deepseek",
                settings.deepseek_summary_timeout_s,
                "deepseek",
            )
    return None


async def recap_for_day(
    settings: ControlSettings, *, day: date, utterances: Sequence[ChildUtterance]
) -> tuple[dict[str, object], RecapSource]:
    """The day's recap and where it came from. Never raises; never contains a quote."""

    count = len(utterances)
    provider = _provider(settings)
    if provider is None or count == 0:
        return fallback_recap(day, count), "fallback"
    api_key, base_url, model, thinking_mode, timeout_s, source = provider
    messages: list[dict[str, str]] = [
        {"role": "system", "content": "你是严谨的中文助手，只返回严格 JSON。"},
        {"role": "user", "content": _prompt(day, utterances)},
    ]
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:

            async def complete(conversation: list[dict[str, str]]) -> object:
                payload: dict[str, object] = {
                    "model": model,
                    "messages": conversation,
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                    "max_tokens": 600,
                    **thinking_disabled(thinking_mode),
                }
                response = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                content: object = response.json()["choices"][0]["message"]["content"]
                return content

            content = await complete(messages)
            recap, copied = _checked(content, utterances)
            if recap is None and copied:
                # One rewrite that names the repeated words; the guard still decides.
                logger.info("guardian recap repeated the child's words; one rewrite day=%s", day)
                content = await complete(
                    [
                        *messages,
                        {"role": "assistant", "content": str(content)},
                        {"role": "user", "content": _rewrite_prompt(copied)},
                    ]
                )
                recap, _ = _checked(content, utterances)
    except Exception as exc:
        logger.warning("guardian recap model unavailable error=%s", type(exc).__name__)
        return fallback_recap(day, count), "fallback"
    if recap is None:
        logger.warning("guardian recap rejected (shape or quoted words) day=%s", day)
        return fallback_recap(day, count), "fallback"
    return recap, source
