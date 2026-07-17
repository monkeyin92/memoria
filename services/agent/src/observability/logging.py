"""Structured logging helpers (no secrets / reasoning content)."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "voice-agent") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def safe_event(**fields: Any) -> dict[str, Any]:
    """Drop forbidden fields before logging."""
    blocked = {
        "api_key",
        "authorization",
        "reasoning_content",
        "livekit_api_secret",
        "dashscope_api_key",
        "deepseek_api_key",
    }
    return {k: v for k, v in fields.items() if k.lower() not in blocked}
