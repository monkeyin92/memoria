"""Install the optional live-lookup semantic resolver on a DuplexRuntime."""

from __future__ import annotations

from typing import Any

from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.providers.live_lookup_semantic_classifier import (
    LiveLookupSemanticClassifier,
    LiveLookupSemanticClassifierConfig,
    LiveLookupSemanticVerdict,
)
from services.agent.src.semantic_endpoint import semantic_endpoint


def build_live_lookup_semantic_classifier(
    settings: Any,
) -> LiveLookupSemanticClassifier | None:
    if not bool(getattr(settings, "live_lookup_semantic_enabled", True)):
        return None
    endpoint = semantic_endpoint(
        settings,
        dashscope_model=str(getattr(settings, "live_lookup_semantic_model", "qwen-flash")),
    )
    if endpoint is None:
        return None
    return LiveLookupSemanticClassifier(
        LiveLookupSemanticClassifierConfig(
            api_key=endpoint.api_key,
            base_url=endpoint.base_url,
            model=endpoint.model,
            timeout_s=float(getattr(settings, "live_lookup_semantic_timeout_s", 1.2)),
            thinking_mode=endpoint.thinking_mode,
        )
    )


def install_live_lookup_semantic_resolver(
    runtime: DuplexRuntime,
    settings: Any,
    *,
    classifier: LiveLookupSemanticClassifier | None = None,
) -> LiveLookupSemanticClassifier | None:
    owned = classifier if classifier is not None else build_live_lookup_semantic_classifier(settings)
    if owned is None:
        runtime.set_live_lookup_semantic_resolver(None)
        return None

    async def _resolve(query: str) -> bool:
        verdict = await owned.classify(current_text=query)
        return verdict is LiveLookupSemanticVerdict.NEEDS_LIVE_LOOKUP

    runtime.set_live_lookup_semantic_resolver(_resolve)
    return owned
