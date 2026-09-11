"""Strict Qwen boundary for the custom-persona structuring stage.

The Control API turns one free-text description into the eleven controlled
style fields (design increment-02 §1.1 難点 4).  The LLM is a *structurer*, not
an authority: an out-of-domain value is rejected (``PersonaStructuringError``)
and mapped to ``422 persona_structuring_invalid`` -- it is never silently
clamped, because a broken model response must not become persisted user data.

The constructor mirrors ``services/persona/qwen_extractor.py`` (DASH-
compatible ``/chat/completions``, ``response_format=json_object``,
``temperature=0``, injected ``httpx`` transport for tests).
"""

from __future__ import annotations

import json
from typing import Final

import httpx
from pydantic import ValidationError

from services.persona.custom_persona_fields import (
    StructuredPersona,
    invalid_structured_field,
)

MAX_FREE_TEXT: Final = 1000


class PersonaStructuringError(RuntimeError):
    """A structuring failure the caller may retry or replace by hand-filling.

    ``field`` names the offending controlled field (when known) so the Control
    API can surface ``{"code": "persona_structuring_invalid", "field": ...}``
    without leaking the raw model output.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def _prompt(free_text: str) -> str:
    return f"""
把账户主人对理想陪伴机器人的自由描述，整理成受控的陪伴风格字段。只输出 JSON 对象，不要 Markdown。
不得补写描述里没有的信息；不确定的维度选择最保守的取值。所有长文本都要精简、面向语音表达。

严格结构：
{{
  "style_description": "一句话角色说明（<=60字）",
  "warmth": "warm|bright|soft|calm|reserved",
  "directness": "gentle|direct",
  "response_length": "brief|balanced",
  "question_frequency": "rare|occasional|frequent",
  "interview_depth": "light|structured|on_explicit_invitation",
  "welcome_text": "开场白（<=60字）",
  "conversation_instruction": "表达规则（<=200字）",
  "voice_instruction": "声音表达规则（<=120字）",
  "default_voice_emotion": "neutral|happy",
  "default_voice_rate": 1.0
}}
只能使用上面列出的枚举值；default_voice_rate 必须是 0.90 到 1.10 之间的数字。

账户主人原话：{free_text[:MAX_FREE_TEXT]}
""".strip()


class QwenCustomPersonaStructurer:
    """Structured-output Qwen client for one free-text persona description."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = "qwen-flash",
        timeout_s: float = 8.0,
        workspace_id: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("custom persona structuring requires an API key")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("custom persona structuring requires an HTTP base URL")
        if not model.strip() or not 1 <= timeout_s <= 120:
            raise ValueError(
                "custom persona structuring requires a model and timeout 1..120s"
            )
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model.strip()
        self._timeout_s = timeout_s
        self._workspace_id = workspace_id.strip()
        self._transport = transport
        self.version = f"qwen-persona-structuring:{self._model}:v1"

    async def structure(self, free_text: str) -> StructuredPersona:
        """Return the validated controlled fields or raise ``PersonaStructuringError``."""
        text = free_text.strip()
        if not text:
            raise PersonaStructuringError("free_text must not be blank", field="free_text")
        if len(text) > MAX_FREE_TEXT:
            raise PersonaStructuringError("free_text is too long", field="free_text")
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if self._workspace_id:
            headers["X-DashScope-WorkSpace"] = self._workspace_id
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是保守的中文陪伴人格结构化器，只返回严格 JSON。",
                },
                {"role": "user", "content": _prompt(text)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 800,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._endpoint, headers=headers, json=payload
                )
                response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("Qwen returned non-text persona structuring content")
        except (
            httpx.HTTPError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            raise PersonaStructuringError(
                "Qwen returned an invalid persona structuring"
            ) from exc
        try:
            return StructuredPersona.model_validate_json(content)
        except ValidationError as exc:
            raise PersonaStructuringError(
                "Qwen persona structuring payload is out of domain",
                field=invalid_structured_field(exc),
            ) from exc


__all__ = [
    "MAX_FREE_TEXT",
    "PersonaStructuringError",
    "QwenCustomPersonaStructurer",
]
