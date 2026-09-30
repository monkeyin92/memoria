"""Run the fixed Chinese long-term-memory evaluation dataset on PostgreSQL.

Every case runs in its own throwaway schema, so point ``--dsn`` at a scratch
database where the archive role may CREATE schemas; nothing already stored
there is read or changed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from services.archive.memory_evaluation import (
    CatalogMemoryEvaluationAdapter,
    load_memory_evaluation_dataset,
    report_json,
    run_memory_evaluation,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_memory_catalog import QwenMemoryEmbedder

DEFAULT_DATASET = (
    Path(__file__).parents[1]
    / "services"
    / "archive"
    / "evaluation"
    / "memory_eval_zh_v1.json"
)
DEFAULT_EMBEDDING_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
DEFAULT_EMBEDDING_MODEL = "qwen3.7-text-embedding-flash"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("MEMORIA_MEMORY_EVAL_DATABASE_URL", ""),
        help=(
            "PostgreSQL DSN of the archive role (default: "
            "MEMORIA_MEMORY_EVAL_DATABASE_URL); needs CREATE on the database."
        ),
    )
    parser.add_argument(
        "--compiler-dsn",
        default=os.environ.get("MEMORIA_MEMORY_EVAL_COMPILER_DATABASE_URL") or None,
        help=(
            "PostgreSQL DSN of the outbox compiler role (default: "
            "MEMORIA_MEMORY_EVAL_COMPILER_DATABASE_URL); required when the "
            "archive role cannot claim compile tasks past account RLS."
        ),
    )
    parser.add_argument(
        "--extractor",
        choices=("rules", "configured"),
        default="rules",
        help=(
            "rules: offline rule extractor (structural ceiling: it never emits "
            "a canonical episode key). configured: the production assembly -- "
            "Qwen with rule fallback -- which needs DASHSCOPE_API_KEY and "
            "OFFLINE_MOCK=false; it is the only path that can produce the "
            "cross-session canonical keys some cases expect."
        ),
    )
    parser.add_argument(
        "--embedding-model",
        nargs="?",
        const=DEFAULT_EMBEDDING_MODEL,
        default=os.environ.get("MEMORIA_MEMORY_EVAL_EMBEDDING_MODEL") or None,
        metavar="MODEL",
        help=(
            "Enable the PostgreSQL vector path with this OpenAI-compatible embedding "
            f"model (omit MODEL to use {DEFAULT_EMBEDDING_MODEL}); the extractor "
            "remains the selected offline/configured extractor."
        ),
    )
    parser.add_argument(
        "--embedding-url",
        default=(
            os.environ.get("MEMORIA_MEMORY_EVAL_EMBEDDING_URL")
            or os.environ.get("MEMORIA_MEMORY_EMBEDDING_URL")
            or DEFAULT_EMBEDDING_URL
        ),
        help="Embedding endpoint (default: DashScope compatible-mode endpoint).",
    )
    parser.add_argument(
        "--embedding-dimensions",
        type=int,
        default=int(
            os.environ.get("MEMORIA_MEMORY_EVAL_EMBEDDING_DIMENSIONS")
            or os.environ.get("MEMORIA_MEMORY_EMBEDDING_DIMENSIONS")
            or "1024"
        ),
        help=(
            "Embedding dimensions (default: evaluation/production environment or 1024)."
        ),
    )
    parser.add_argument(
        "--embedding-timeout-s",
        type=float,
        default=float(
            os.environ.get("MEMORIA_MEMORY_EVAL_EMBEDDING_TIMEOUT_S")
            or os.environ.get("MEMORIA_MEMORY_EMBEDDING_TIMEOUT_S")
            or "5"
        ),
        help="Per-request embedding timeout in seconds (default: production default 5).",
    )
    return parser


def build_embedder(
    *,
    embedding_model: str | None,
    embedding_url: str,
    embedding_dimensions: int,
    embedding_timeout_s: float,
) -> QwenMemoryEmbedder | None:
    """The production embedder for ``embedding_model``; None keeps the lexical path."""

    if embedding_model is None:
        return None
    api_key = os.environ.get("MEMORIA_MEMORY_EMBEDDING_API_KEY") or os.environ.get(
        "DASHSCOPE_API_KEY", ""
    )
    if not api_key:
        raise SystemExit(
            "--embedding-model requires MEMORIA_MEMORY_EMBEDDING_API_KEY "
            "or DASHSCOPE_API_KEY; the key is read from the environment only"
        )
    return QwenMemoryEmbedder(
        endpoint=embedding_url,
        api_key=api_key,
        model=embedding_model,
        dimensions=embedding_dimensions,
        timeout_s=embedding_timeout_s,
    )


def _build_adapter(
    kind: str,
    dsn: str,
    compiler_dsn: str | None,
    *,
    embedding_model: str | None,
    embedding_url: str,
    embedding_dimensions: int,
    embedding_timeout_s: float,
) -> CatalogMemoryEvaluationAdapter:
    embedder = build_embedder(
        embedding_model=embedding_model,
        embedding_url=embedding_url,
        embedding_dimensions=embedding_dimensions,
        embedding_timeout_s=embedding_timeout_s,
    )
    if kind == "rules":
        return CatalogMemoryEvaluationAdapter(
            dsn,
            compiler_dsn=compiler_dsn,
            embedder=embedder,
            require_vector=embedder is not None,
        )
    # Production parity: the same assembly the Control API uses, so an offline
    # or unkeyed environment fails loudly instead of quietly scoring rules.
    from services.control_api.app.config import ControlSettings
    from services.control_api.app.memory_components import build_memory_extractor

    extractor = build_memory_extractor(ControlSettings())
    if isinstance(extractor, RuleBasedMemoryExtractor):
        raise SystemExit(
            "--extractor configured needs DASHSCOPE_API_KEY set and "
            "OFFLINE_MOCK=false; without them the production assembly is the "
            "rule extractor, which cannot produce canonical episode keys."
        )
    adapter = CatalogMemoryEvaluationAdapter(
        dsn,
        extractor,
        compiler_dsn=compiler_dsn,
        embedder=embedder,
        require_vector=embedder is not None,
    )
    adapter.name = f"memoria-postgres-{kind}" + ("-vector" if embedder is not None else "")
    return adapter


async def _run(
    dataset_path: Path,
    extractor: str,
    dsn: str,
    compiler_dsn: str | None,
    *,
    embedding_model: str | None,
    embedding_url: str,
    embedding_dimensions: int,
    embedding_timeout_s: float,
) -> str:
    dataset = load_memory_evaluation_dataset(dataset_path)
    report = await run_memory_evaluation(
        dataset,
        _build_adapter(
            extractor,
            dsn,
            compiler_dsn,
            embedding_model=embedding_model,
            embedding_url=embedding_url,
            embedding_dimensions=embedding_dimensions,
            embedding_timeout_s=embedding_timeout_s,
        ),
    )
    return report_json(report)


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn (or MEMORIA_MEMORY_EVAL_DATABASE_URL) is required")
    payload = asyncio.run(
        _run(
            args.dataset,
            args.extractor,
            args.dsn,
            args.compiler_dsn,
            embedding_model=args.embedding_model,
            embedding_url=args.embedding_url,
            embedding_dimensions=args.embedding_dimensions,
            embedding_timeout_s=args.embedding_timeout_s,
        )
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
