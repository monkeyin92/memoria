"""Build the memory components shared by runtime and projection maintenance."""

from __future__ import annotations

from services.archive.memory_domain import MemoryEmbedder, MemoryExtractor
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_memory_catalog import QwenMemoryEmbedder
from services.archive.qwen_memory_extractor import FallbackMemoryExtractor, QwenMemoryExtractor
from services.control_api.app.config import ControlSettings


def build_memory_embedder(settings: ControlSettings) -> MemoryEmbedder | None:
    api_key = settings.memory_embedding_api_key.get_secret_value()
    if not settings.memory_embedding_url or not api_key or not settings.memory_embedding_model:
        return None
    return QwenMemoryEmbedder(
        endpoint=settings.memory_embedding_url,
        api_key=api_key,
        model=settings.memory_embedding_model,
        dimensions=settings.memory_embedding_dimensions,
        timeout_s=settings.memory_embedding_timeout_s,
    )


def build_memory_extractor(settings: ControlSettings) -> MemoryExtractor:
    fallback = RuleBasedMemoryExtractor()
    api_key = settings.dashscope_api_key.get_secret_value()
    if settings.offline_mock or not api_key:
        return fallback
    return FallbackMemoryExtractor(
        QwenMemoryExtractor(
            api_key=api_key,
            base_url=settings.dashscope_base_url,
            model=settings.memory_extraction_model,
            timeout_s=settings.memory_extraction_timeout_s,
            workspace_id=settings.dashscope_workspace_id,
        ),
        fallback,
    )
