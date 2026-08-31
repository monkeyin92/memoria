"""Install the optional live-lookup semantic resolver on a DuplexRuntime."""

from __future__ import annotations

from typing import Any

from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.providers.live_lookup_semantic_classifier import (
    LiveLookupSemanticClassifier,
    LiveLookupSemanticClassifierConfig,
    LiveLookupSemanticVerdict,
)


def build_live_lookup_semantic_classifier(
    settings: Any,
) -> LiveLookupSemanticClassifier | None:
    if not bool(getattr(settings, "live_lookup_semantic_enabled", True)):
        return None
    api_key = str(getattr(settings, "dashscope_api_key", "") or "").strip()
    if not api_key:
        return None
    return LiveLookupSemanticClassifier(
        LiveLookupSemanticClassifierConfig(
            api_key=api_key,
            base_url=str(
                getattr(
                    settings,
                    "dashscope_compatible_base_url",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                )
            ),
            model=str(getattr(settings, "live_lookup_semantic_model", "deepseek-v4-flash")),
            timeout_s=float(getattr(settings, "live_lookup_semantic_timeout_s", 0.8)),
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
