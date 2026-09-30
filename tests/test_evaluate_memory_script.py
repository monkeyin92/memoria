from __future__ import annotations

import pytest
from scripts.evaluate_memory import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_URL,
    _build_adapter,
    _parser,
)
from services.archive.postgres_memory_catalog import QwenMemoryEmbedder


def test_embedding_flag_defaults_to_the_requested_bailian_model() -> None:
    args = _parser().parse_args(["--dsn", "postgresql://db/test", "--embedding-model"])

    assert args.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert DEFAULT_EMBEDDING_MODEL == "qwen3.7-text-embedding-flash"


def test_embedding_adapter_uses_dashscope_key_and_requires_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")

    adapter = _build_adapter(
        "rules",
        "postgresql://db/test",
        None,
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        embedding_url=DEFAULT_EMBEDDING_URL,
        embedding_dimensions=1024,
        embedding_timeout_s=5.0,
    )

    assert isinstance(adapter._embedder, QwenMemoryEmbedder)
    assert adapter._embedder.model == DEFAULT_EMBEDDING_MODEL
    assert adapter._embedder.dimensions == 1024
    assert adapter._require_vector is True
    assert adapter.name == f"memoria-postgres-vector-{DEFAULT_EMBEDDING_MODEL}"


def test_embedding_adapter_never_accepts_a_missing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMORIA_MEMORY_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    with pytest.raises(SystemExit, match="requires MEMORIA_MEMORY_EMBEDDING_API_KEY"):
        _build_adapter(
            "rules",
            "postgresql://db/test",
            None,
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            embedding_url=DEFAULT_EMBEDDING_URL,
            embedding_dimensions=1024,
            embedding_timeout_s=5.0,
        )
