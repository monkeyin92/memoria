"""Install the optional conversation-close semantic resolver on a DuplexRuntime."""

from __future__ import annotations

from typing import Any

from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.providers.close_intent_semantic_classifier import (
    CloseIntentSemanticClassifier,
    CloseIntentSemanticClassifierConfig,
    CloseIntentSemanticVerdict,
)


def build_close_intent_semantic_classifier(
    settings: Any,
) -> CloseIntentSemanticClassifier | None:
    if not bool(getattr(settings, "conversation_close_semantic_enabled", True)):
        return None
    api_key = str(getattr(settings, "dashscope_api_key", "") or "").strip()
    if not api_key:
        return None
    return CloseIntentSemanticClassifier(
        CloseIntentSemanticClassifierConfig(
            api_key=api_key,
            base_url=str(
                getattr(
                    settings,
                    "dashscope_compatible_base_url",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                )
            ),
            model=str(
                getattr(settings, "conversation_close_semantic_model", "qwen3-flash")
            ),
            timeout_s=float(getattr(settings, "conversation_close_semantic_timeout_s", 0.8)),
        )
    )


def install_conversation_close_semantic_resolver(
    runtime: DuplexRuntime,
    settings: Any,
    *,
    classifier: CloseIntentSemanticClassifier | None = None,
) -> CloseIntentSemanticClassifier | None:
    owned = (
        classifier
        if classifier is not None
        else build_close_intent_semantic_classifier(settings)
    )
    if owned is None:
        runtime.set_conversation_close_semantic_resolver(None)
        return None

    async def _resolve(text: str) -> bool:
        verdict = await owned.classify(current_text=text)
        return verdict is CloseIntentSemanticVerdict.END_SESSION

    runtime.set_conversation_close_semantic_resolver(_resolve)
    return owned
