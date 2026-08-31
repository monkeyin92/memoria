"""Agent-scoped live-query markers for component-only deploys."""

from __future__ import annotations

from services.agent.src.live_lookup_router import keyword_requires_live_media_lookup

requires_live_media_lookup = keyword_requires_live_media_lookup

__all__ = ["requires_live_media_lookup"]
