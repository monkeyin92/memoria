"""Endpoint selection and builders for the Control API's small text-model calls.

Crisis evidence, memory and persona extraction and persona structuring follow
``LLM_PROVIDER`` like the agent's reply model.  Qwen and Bailian keep the
DashScope key and each task's own model; the official DeepSeek API only serves
DeepSeek models, so there every task uses ``DEEPSEEK_FAST_MODEL``.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.agent.src.providers.crisis_semantic_classifier import (
    CrisisSemanticClassifier,
    CrisisSemanticClassifierConfig,
)
from services.common.llm_thinking import ThinkingMode, thinking_mode_for
from services.control_api.app.config import ControlSettings
from services.persona.custom_persona_structurer import QwenCustomPersonaStructurer
from services.persona.qwen_extractor import FallbackPersonaExtractor, QwenPersonaExtractor
from services.persona.rules import PersonaExtractor, RuleBasedPersonaExtractor


@dataclass(frozen=True, slots=True)
class TextModelEndpoint:
    api_key: str
    base_url: str
    workspace_id: str
    thinking_mode: ThinkingMode
    # Set for the official DeepSeek API, where it replaces every task's Qwen model.
    fixed_model: str | None

    def model(self, configured: str) -> str:
        return self.fixed_model or configured


def text_model_endpoint(settings: ControlSettings) -> TextModelEndpoint | None:
    """The endpoint for these calls, or ``None`` when offline or the provider has no key."""

    if settings.offline_mock:
        return None
    thinking_mode = thinking_mode_for(settings.llm_provider)
    if thinking_mode == "deepseek":
        api_key = settings.deepseek_api_key.get_secret_value()
        endpoint = TextModelEndpoint(
            api_key=api_key,
            base_url=settings.deepseek_base_url,
            workspace_id="",
            thinking_mode=thinking_mode,
            fixed_model=settings.deepseek_fast_model,
        )
    else:
        api_key = settings.dashscope_api_key.get_secret_value()
        endpoint = TextModelEndpoint(
            api_key=api_key,
            base_url=settings.dashscope_base_url,
            workspace_id=settings.dashscope_workspace_id,
            thinking_mode=thinking_mode,
            fixed_model=None,
        )
    return endpoint if api_key else None


def build_persona_extractor(settings: ControlSettings) -> PersonaExtractor:
    fallback = RuleBasedPersonaExtractor()
    endpoint = text_model_endpoint(settings)
    if endpoint is None:
        return fallback
    return FallbackPersonaExtractor(
        QwenPersonaExtractor(
            api_key=endpoint.api_key,
            base_url=endpoint.base_url,
            model=endpoint.model(settings.memory_extraction_model),
            timeout_s=settings.memory_extraction_timeout_s,
            workspace_id=endpoint.workspace_id,
            thinking_mode=endpoint.thinking_mode,
        ),
        fallback,
    )


def build_persona_structurer(
    settings: ControlSettings,
) -> QwenCustomPersonaStructurer | None:
    """Build the custom-persona structurer, or ``None`` when unmocked/offline.

    Without authority the endpoint returns ``503 persona_structuring_
    unavailable`` and the client hand-fills the same controlled fields
    (PRD P1-2), so no free-text ever reaches a prompt.
    """

    endpoint = text_model_endpoint(settings)
    if endpoint is None:
        return None
    return QwenCustomPersonaStructurer(
        api_key=endpoint.api_key,
        base_url=endpoint.base_url,
        model=endpoint.model(settings.persona_structuring_model),
        timeout_s=settings.persona_structuring_timeout_s,
        workspace_id=endpoint.workspace_id,
        thinking_mode=endpoint.thinking_mode,
    )


def build_crisis_semantic_classifier(
    settings: ControlSettings,
) -> CrisisSemanticClassifier | None:
    endpoint = text_model_endpoint(settings)
    if endpoint is None or not settings.crisis_semantic_enabled:
        return None
    return CrisisSemanticClassifier(
        CrisisSemanticClassifierConfig(
            api_key=endpoint.api_key,
            base_url=endpoint.base_url,
            model=endpoint.model(settings.crisis_semantic_model),
            timeout_s=settings.crisis_semantic_timeout_s,
            thinking_mode=endpoint.thinking_mode,
        )
    )
