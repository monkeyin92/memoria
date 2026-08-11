"""Persistent message, daily-summary, and profile APIs for the H5 client."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, cast
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.common.companions import COMPANION_IDS
from services.common.redaction import redact_pii
from services.control_api.app.account_gate import require_writable_account
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import (
    MemoryStore,
    MessageIdempotencyConflictError,
)
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
    require_matching_user,
)

router = APIRouter(prefix="/v1/memory", tags=["memory"])
logger = logging.getLogger(__name__)

UserId = str
SummarySource = Literal["qwen", "deepseek", "fallback"]
_LEGACY_MEMORY_WRITE_UNAVAILABLE_DETAIL = (
    "legacy memory writes are unavailable in production"
)


def _clean_user_id(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 128:
        raise ValueError("user_id length must be between 1 and 128 characters")
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class MessageCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    user_id: UserId
    client_message_id: str = Field(min_length=36, max_length=36)
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=8000)
    emotion: str | None = Field(default=None, max_length=32)

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str) -> str:
        return _clean_user_id(value)

    @field_validator("client_message_id")
    @classmethod
    def validate_client_message_id(cls, value: str) -> str:
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("client_message_id must be a UUID") from exc


class MessageRecord(BaseModel):
    id: int
    user_id: str
    client_message_id: str
    role: Literal["user", "assistant"]
    text: str
    emotion: str | None
    local_date: date
    created_at: datetime


class DailySummaryContent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=80)
    overview: str = Field(min_length=1, max_length=800)
    highlights: list[str] = Field(default_factory=list, max_length=6)
    mood: Literal["positive", "calm", "mixed", "low", "tense"]
    suggestion: str = Field(min_length=1, max_length=300)

    @field_validator("highlights")
    @classmethod
    def validate_highlights(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 200 for value in cleaned):
            raise ValueError("each highlight must contain 1 to 200 characters")
        return cleaned


class DailyItem(BaseModel):
    day: date
    message_count: int
    summary: DailySummaryContent | None
    source: SummarySource | None
    generated_at: datetime | None


class DailyListResponse(BaseModel):
    items: list[DailyItem]


class GenerateSummaryRequest(BaseModel):
    user_id: UserId

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str) -> str:
        return _clean_user_id(value)


class ProfileRecord(BaseModel):
    user_id: str
    display_name: str
    bio: str
    avatar_url: str
    phone_number_masked: str
    companion_id: str | None
    timezone: str
    auto_summary: bool
    voice_reply: bool
    gentle_reminders: bool
    reject_non_owner_voice: bool
    subject_category: Literal["unknown", "adult", "minor"]
    birth_year_band: Literal["unknown", "under_14", "14_17", "adult"]
    age_evidence_status: Literal["unverified", "verified", "disputed"]
    subject_revision: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    bio: str | None = Field(default=None, max_length=500)
    avatar_url: str | None = Field(default=None, max_length=2048)
    companion_id: str | None = Field(default=None, min_length=1, max_length=32)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    auto_summary: bool | None = None
    voice_reply: bool | None = None
    gentle_reminders: bool | None = None
    reject_non_owner_voice: bool | None = None

    @field_validator("avatar_url")
    @classmethod
    def reject_sensitive_avatar_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        sensitive_names = {"token", "access_token", "api_key", "apikey", "key", "secret", "sig", "signature"}
        query_names = {name.lower() for name, _ in parse_qsl(urlsplit(value).query)}
        if query_names & sensitive_names or redact_pii(value) != value:
            raise ValueError("avatar_url must not contain credentials or personal data")
        return value

    @field_validator("companion_id")
    @classmethod
    def validate_companion_id(cls, value: str | None) -> str | None:
        if value is not None and value not in COMPANION_IDS:
            raise ValueError("companion_id is not supported")
        return value

    @model_validator(mode="after")
    def require_one_field(self) -> ProfileUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one profile field is required")
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("timezone must be a valid IANA timezone") from exc
        return self


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _settings(request: Request) -> ControlSettings:
    return cast(ControlSettings, request.app.state.settings)


def _reject_legacy_memory_write_in_production(request: Request) -> None:
    settings = _settings(request)
    if settings.environment == "production":
        raise HTTPException(
            status_code=503,
            detail=_LEGACY_MEMORY_WRITE_UNAVAILABLE_DETAIL,
        )


def _message_request_fingerprint(
    settings: ControlSettings,
    *,
    user_id: str,
    body: MessageCreate,
) -> str:
    canonical = json.dumps(
        {
            "emotion": body.emotion,
            "role": body.role,
            "text": body.text,
            "user_id": user_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(
        settings.memoria_message_idempotency_secret.get_secret_value().encode("utf-8"),
        b"memoria-message-idempotency-v1\0" + canonical,
        hashlib.sha256,
    ).hexdigest()


def _fallback_summary(messages: list[dict[str, Any]], summary_date: date) -> DailySummaryContent:
    user_texts = [str(item["text"]) for item in messages if item["role"] == "user"]
    source_texts = user_texts or [str(item["text"]) for item in messages]
    excerpts: list[str] = []
    for raw in source_texts:
        text = re.sub(r"\s+", " ", raw).strip()
        excerpt = text if len(text) <= 80 else f"{text[:79]}…"
        if excerpt and excerpt not in excerpts:
            excerpts.append(excerpt)
        if len(excerpts) == 3:
            break

    joined = "；".join(excerpts)
    overview = f"本日共记录 {len(messages)} 条聊天。"
    if joined:
        overview += f"主要提到：{joined}。"

    positive_words = ("开心", "高兴", "喜欢", "谢谢", "哈哈", "真棒", "顺利")
    negative_words = ("难过", "生气", "焦虑", "烦", "疲惫", "失败", "担心")
    positive = sum(text.count(word) for text in source_texts for word in positive_words)
    negative = sum(text.count(word) for text in source_texts for word in negative_words)
    if positive and negative:
        mood: Literal["positive", "calm", "mixed", "low", "tense"] = "mixed"
    elif positive:
        mood = "positive"
    elif negative:
        mood = "low"
    else:
        mood = "calm"

    return DailySummaryContent(
        title=f"{summary_date:%m月%d日}聊天速记",
        overview=overview,
        highlights=excerpts,
        mood=mood,
        suggestion="当前使用本地规则整理；大模型服务恢复后可获得更完整的回顾。",
    )


def _summary_prompt(messages: list[dict[str, Any]], summary_date: date) -> str:
    lines: list[str] = []
    remaining = 40_000
    for item in messages:
        role = "用户" if item["role"] == "user" else "助手"
        normalized = re.sub(r"\s+", " ", str(item["text"])).strip()[:1200]
        line = f"{role}: {normalized}"
        if len(line) > remaining:
            break
        lines.append(line)
        remaining -= len(line)
    transcript = "\n".join(lines)
    return f"""
请总结 {summary_date.isoformat()} 的聊天记录。只输出一个 JSON 对象，不要 Markdown、解释或额外字段。
JSON 必须严格使用以下结构：
{{
  "title": "不超过80字的标题",
  "overview": "不超过800字的客观概述",
  "highlights": ["1至6条，每条不超过200字"],
  "mood": "positive|calm|mixed|low|tense 五选一",
  "suggestion": "不超过300字、温和且可执行的下一步建议"
}}
不得编造聊天中未出现的事实，也不要输出敏感信息之外的推断。

聊天记录：
{transcript}
""".strip()


async def _openai_compatible_summary(
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout_s: float,
    messages: list[dict[str, Any]],
    summary_date: date,
) -> DailySummaryContent:
    if not api_key:
        raise RuntimeError("LLM summary provider is not configured")
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是严谨的中文聊天记录整理助手，必须返回严格 JSON。",
            },
            {"role": "user", "content": _summary_prompt(messages, summary_date)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 900,
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        response.raise_for_status()
    body = response.json()
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("LLM provider returned an unexpected response") from exc
    if not isinstance(content, str):
        raise ValueError("LLM provider returned non-text summary content")
    return DailySummaryContent.model_validate_json(content)


async def _dashscope_summary(
    settings: ControlSettings,
    messages: list[dict[str, Any]],
    summary_date: date,
) -> DailySummaryContent:
    return await _openai_compatible_summary(
        api_key=settings.dashscope_api_key.get_secret_value(),
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_summary_model,
        timeout_s=settings.dashscope_summary_timeout_s,
        messages=messages,
        summary_date=summary_date,
    )


async def _deepseek_summary(
    settings: ControlSettings,
    messages: list[dict[str, Any]],
    summary_date: date,
) -> DailySummaryContent:
    return await _openai_compatible_summary(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_summary_model,
        timeout_s=settings.deepseek_summary_timeout_s,
        messages=messages,
        summary_date=summary_date,
    )


@router.post("/messages", response_model=MessageRecord, status_code=201)
def create_message(
    body: MessageCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    response: Response,
) -> dict[str, Any]:
    _reject_legacy_memory_write_in_production(request)
    user_id = require_matching_user(body.user_id, user)
    settings = _settings(request)
    client_message_id = body.client_message_id
    try:
        local_timezone = ZoneInfo(settings.memoria_timezone)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=500, detail="server timezone is invalid") from exc
    now = datetime.now(UTC)
    try:
        record, duplicate = _store(request).add_message(
            user_id=user_id,
            client_message_id=client_message_id,
            request_fingerprint=_message_request_fingerprint(
                settings,
                user_id=user_id,
                body=body,
            ),
            role=body.role,
            text=redact_pii(body.text),
            emotion=redact_pii(body.emotion) if body.emotion else None,
            local_date=now.astimezone(local_timezone).date().isoformat(),
            created_at=now.isoformat().replace("+00:00", "Z"),
        )
    except MessageIdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail="client_message_id payload conflict") from exc
    if duplicate:
        response.status_code = 200
    return record


@router.get("/days", response_model=DailyListResponse)
def list_days(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    user_id: str = Query(min_length=1, max_length=128),
    limit: int = Query(default=30, ge=1, le=90),
    before: date | None = None,
) -> dict[str, Any]:
    try:
        clean_user_id = _clean_user_id(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    items = _store(request).list_days(
        user_id=require_matching_user(clean_user_id, user),
        limit=limit,
        before=before.isoformat() if before else None,
    )
    return {"items": items}


@router.post("/days/{summary_date}/summary", response_model=DailyItem)
async def generate_daily_summary(
    summary_date: date,
    body: GenerateSummaryRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    _reject_legacy_memory_write_in_production(request)
    user_id = require_matching_user(body.user_id, user)
    store = _store(request)
    messages = store.list_messages(user_id=user_id, summary_date=summary_date.isoformat())
    if not messages:
        raise HTTPException(status_code=404, detail="no messages found for this date")

    settings = _settings(request)
    source: SummarySource = "fallback"
    summary = _fallback_summary(messages, summary_date)
    if settings.llm_provider == "qwen" and settings.dashscope_api_key.get_secret_value():
        try:
            summary = await _dashscope_summary(settings, messages, summary_date)
            source = "qwen"
        except Exception:
            logger.warning("Qwen summary failed; using deterministic fallback")
    elif (
        settings.llm_provider == "bailian_deepseek"
        and settings.dashscope_api_key.get_secret_value()
    ):
        try:
            summary = await _dashscope_summary(settings, messages, summary_date)
            source = "deepseek"
        except Exception:
            logger.warning("Bailian DeepSeek summary failed; using deterministic fallback")
    elif settings.llm_provider == "deepseek" and settings.deepseek_api_key.get_secret_value():
        try:
            summary = await _deepseek_summary(settings, messages, summary_date)
            source = "deepseek"
        except Exception:
            logger.warning("DeepSeek summary failed; using deterministic fallback")

    return store.upsert_summary(
        user_id=user_id,
        summary_date=summary_date.isoformat(),
        content=summary.model_dump(),
        source=source,
        message_count=len(messages),
        generated_at=_utc_now(),
    )


@router.get("/profile/{user_id}", response_model=ProfileRecord)
def get_profile(
    user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    try:
        clean_user_id = _clean_user_id(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _store(request).get_profile(
        user_id=require_matching_user(clean_user_id, user),
        now=_utc_now(),
    )


@router.put("/profile/{user_id}", response_model=ProfileRecord)
def update_profile(
    user_id: str,
    body: ProfileUpdate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    try:
        clean_user_id = _clean_user_id(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    values = body.model_dump(exclude_none=True)
    for field in ("display_name", "bio"):
        if field in values:
            values[field] = redact_pii(str(values[field]))
    return _store(request).update_profile(
        user_id=require_matching_user(clean_user_id, user),
        values=values,
        now=_utc_now(),
    )
