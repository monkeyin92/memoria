"""Run the large-corpus distractor evaluation on PostgreSQL.

One account holds a few hundred memories (topic clusters, facts about other people, daily
trivia); paraphrased questions must find their target among them.  Every corpus size in
``--background-sizes`` is one variant (the labelled core plus that many background
memories) and gets its own throwaway schema.  Without ``--embedding-model`` this is the
lexical fallback; with it, the production hybrid path with that embedding model, in strict
mode (any embedding failure aborts the run instead of falling back to lexical search).

    uv run python scripts/evaluate_memory_distractors.py --dsn postgresql://... \\
        --embedding-model text-embedding-v4 --output docs/....json

Compare two receipts with ``scripts/compare_memory_distractor_receipts.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from services.archive.memory_distractor_evaluation import (
    build_receipt,
    evaluate_variant,
    load_distractor_pool,
    receipt_json,
)
from services.archive.memory_domain import MemoryEmbedder, MemoryEmbeddingUnavailableError
from services.archive.memory_evaluation import CatalogMemoryEvaluationAdapter

from scripts.evaluate_memory import DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_URL, build_embedder

DEFAULT_DATASET = (
    Path(__file__).parents[1]
    / "services"
    / "archive"
    / "evaluation"
    / "memory_eval_zh_v3_distractors.json"
)


class MemoizedEmbedder:
    """Embeds each distinct text once per run, retrying transient service errors.

    The core memories are ingested again for every corpus size, so without the cache the
    same sentences would be sent to the embedding service once per variant.  A run makes
    thousands of requests, so a request that fails is retried ``retries`` times with a
    growing pause; one that still fails aborts the run (the evaluation stays strict).
    ``cache_path`` additionally keeps the vectors between runs (keyed by model and
    dimensions), which makes comparing retrieval changes on the same corpus cheap.
    """

    def __init__(
        self,
        inner: MemoryEmbedder,
        *,
        retries: int = 2,
        backoff_s: float = 1.0,
        cache_path: Path | None = None,
    ) -> None:
        self._inner = inner
        self.model = inner.model
        self.dimensions = inner.dimensions
        self._retries = retries
        self._backoff_s = backoff_s
        self._cache_path = cache_path
        self._cache: dict[str, tuple[float, ...]] = {}
        self.requests = 0
        self.retried = 0
        self.cache_hits = 0
        self._loaded = 0
        if cache_path is not None and cache_path.exists():
            stored = json.loads(cache_path.read_text(encoding="utf-8"))
            if stored.get("model") == self.model and stored.get("dimensions") == self.dimensions:
                self._cache = {
                    text: tuple(float(value) for value in vector)
                    for text, vector in stored["vectors"].items()
                }
                self._loaded = len(self._cache)

    async def embed(self, text: str) -> tuple[float, ...]:
        cached = self._cache.get(text)
        if cached is None:
            cached = await self._request(text)
            self._cache[text] = cached
            self.requests += 1
        else:
            self.cache_hits += 1
        return cached

    def save(self) -> None:
        if self._cache_path is None or len(self._cache) == self._loaded:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": self.model,
            "dimensions": self.dimensions,
            "vectors": {text: list(vector) for text, vector in self._cache.items()},
        }
        self._cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    async def _request(self, text: str) -> tuple[float, ...]:
        attempt = 0
        while True:
            try:
                return await self._inner.embed(text)
            except MemoryEmbeddingUnavailableError:
                if attempt >= self._retries:
                    raise
                attempt += 1
                self.retried += 1
                await asyncio.sleep(self._backoff_s * attempt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("MEMORIA_MEMORY_EVAL_DATABASE_URL", ""),
        help="PostgreSQL DSN (default: MEMORIA_MEMORY_EVAL_DATABASE_URL); needs CREATE on the database.",
    )
    parser.add_argument(
        "--background-sizes",
        default="0,100,10000",
        help="Comma-separated background memory counts, one variant each (clamped to the dataset).",
    )
    parser.add_argument(
        "--check-ingestion",
        action="store_true",
        help="Also ask every memory's own sentence and report memories the catalog failed to store.",
    )
    parser.add_argument(
        "--embedding-model",
        nargs="?",
        const=DEFAULT_EMBEDDING_MODEL,
        default=os.environ.get("MEMORIA_MEMORY_EVAL_EMBEDDING_MODEL") or None,
        metavar="MODEL",
        help=(
            "Enable the vector path with this OpenAI-compatible embedding model "
            f"(omit MODEL for {DEFAULT_EMBEDDING_MODEL}); omit the flag for the lexical fallback."
        ),
    )
    parser.add_argument(
        "--embedding-url",
        default=(
            os.environ.get("MEMORIA_MEMORY_EVAL_EMBEDDING_URL")
            or os.environ.get("MEMORIA_MEMORY_EMBEDDING_URL")
            or DEFAULT_EMBEDDING_URL
        ),
    )
    parser.add_argument(
        "--embedding-dimensions",
        type=int,
        default=int(
            os.environ.get("MEMORIA_MEMORY_EVAL_EMBEDDING_DIMENSIONS")
            or os.environ.get("MEMORIA_MEMORY_EMBEDDING_DIMENSIONS")
            or "1024"
        ),
    )
    parser.add_argument(
        "--embedding-cache",
        type=Path,
        default=None,
        help="Keep embedding vectors in this file between runs (same model and dimensions only).",
    )
    parser.add_argument(
        "--embedding-timeout-s",
        type=float,
        default=float(
            os.environ.get("MEMORIA_MEMORY_EVAL_EMBEDDING_TIMEOUT_S")
            or os.environ.get("MEMORIA_MEMORY_EMBEDDING_TIMEOUT_S")
            or "5"
        ),
    )
    return parser


def _sizes(raw: str, available: int) -> list[int]:
    sizes = sorted({min(int(value), available) for value in raw.split(",") if value.strip()})
    if not sizes or sizes[0] < 0:
        raise SystemExit("--background-sizes needs non-negative integers")
    return sizes


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    pool = load_distractor_pool(args.dataset)
    inner = build_embedder(
        embedding_model=args.embedding_model,
        embedding_url=args.embedding_url,
        embedding_dimensions=args.embedding_dimensions,
        embedding_timeout_s=args.embedding_timeout_s,
    )
    embedder = (
        MemoizedEmbedder(inner, cache_path=args.embedding_cache) if inner is not None else None
    )
    adapter = CatalogMemoryEvaluationAdapter(
        args.dsn, embedder=embedder, require_vector=embedder is not None
    )
    variants = []
    try:
        for size in _sizes(args.background_sizes, len(pool.background_order)):
            variant = await evaluate_variant(
                adapter, pool, background=size, check_ingestion=args.check_ingestion
            )
            variants.append(variant)
            print(_line(variant), flush=True)
    finally:
        if embedder is not None:
            embedder.save()
    config: dict[str, Any] = {
        "embedding_model": None if embedder is None else embedder.model,
        "embedding_dimensions": None if embedder is None else embedder.dimensions,
        "embedding_timeout_s": None if embedder is None else args.embedding_timeout_s,
        "embedding_requests": None if embedder is None else embedder.requests,
        "embedding_retries": None if embedder is None else embedder.retried,
        "embedding_cache_hits": None if embedder is None else embedder.cache_hits,
        "check_ingestion": args.check_ingestion,
    }
    return build_receipt(pool, adapter_name=adapter.name, config=config, variants=variants)


def _line(variant: dict[str, Any]) -> str:
    summary = variant["summary"]
    paraphrase, lexical, exact = summary["paraphrase"], summary["lexical"], summary["exact_term"]
    return (
        f"background={variant['background']:>4d} memories={variant['memories']:>4d}  "
        f"paraphrase top1={paraphrase['top1']:.3f} mrr={paraphrase['mrr']:.3f} "
        f"window={paraphrase['found_in_production_window']:.3f}  "
        f"lexical top1={lexical['top1']:.3f}  exact top1={exact['top1']:.3f}"
    )


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn (or MEMORIA_MEMORY_EVAL_DATABASE_URL) is required")
    receipt = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(receipt_json(receipt), encoding="utf-8")
    failures = [
        (variant["background"], variant["ingestion"]["failures"])
        for variant in receipt["variants"]
        if variant.get("ingestion", {}).get("failures")
    ]
    if failures:
        print(f"INGESTION FAILURES: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
