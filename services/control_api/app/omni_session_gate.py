"""Refuse Qwen Omni before any DashScope session or SDP request."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def require_omni_api_key(settings: Any) -> str:
    if not bool(getattr(settings, "qwen_omni_session_enabled", False)):
        raise HTTPException(status_code=503, detail="Qwen Omni 会话未开启")
    api_key = settings.dashscope_api_key.get_secret_value()
    if not api_key:
        raise HTTPException(status_code=503, detail="Qwen3.5-Omni 服务端尚未配置")
    return str(api_key)
