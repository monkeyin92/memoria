from __future__ import annotations

from pathlib import Path

import pytest
from scripts.evaluate_memory_distractors import MemoizedEmbedder, _sizes
from services.archive.memory_domain import MemoryEmbeddingUnavailableError


class _CountingEmbedder:
    model = "stub"
    dimensions = 2

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        return (float(len(text)), 1.0)


async def test_memoized_embedder_requests_each_distinct_text_once() -> None:
    inner = _CountingEmbedder()
    embedder = MemoizedEmbedder(inner)

    first = await embedder.embed("我妈妈的生日")
    again = await embedder.embed("我妈妈的生日")
    other = await embedder.embed("母亲是哪天出生的？")

    assert first == again != other
    assert inner.calls == ["我妈妈的生日", "母亲是哪天出生的？"]
    assert embedder.requests == 2
    assert (embedder.model, embedder.dimensions) == ("stub", 2)


def test_background_sizes_are_sorted_deduplicated_and_clamped_to_the_dataset() -> None:
    assert _sizes("100, 0,10000,100", 302) == [0, 100, 302]
    assert _sizes("500,600", 302) == [302]
    with pytest.raises(SystemExit):
        _sizes("-1", 302)


class _FlakyEmbedder:
    model = "stub"
    dimensions = 2

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    async def embed(self, text: str) -> tuple[float, ...]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise MemoryEmbeddingUnavailableError("transient")
        return (1.0, 2.0)


async def test_memoized_embedder_retries_a_transient_error_but_not_a_persistent_one() -> None:
    flaky = _FlakyEmbedder(failures=2)
    embedder = MemoizedEmbedder(flaky, retries=2, backoff_s=0.0)

    assert await embedder.embed("句子") == (1.0, 2.0)
    assert (flaky.attempts, embedder.requests, embedder.retried) == (3, 1, 2)

    broken = _FlakyEmbedder(failures=99)
    with pytest.raises(MemoryEmbeddingUnavailableError):
        await MemoizedEmbedder(broken, retries=2, backoff_s=0.0).embed("句子")
    assert broken.attempts == 3


async def test_embedding_cache_survives_between_runs_and_ignores_another_model(
    tmp_path: Path,
) -> None:
    path = tmp_path / "vectors.json"
    first = MemoizedEmbedder(_CountingEmbedder(), cache_path=path)
    await first.embed("句子")
    first.save()

    inner = _CountingEmbedder()
    second = MemoizedEmbedder(inner, cache_path=path)
    assert await second.embed("句子") == (float(len("句子")), 1.0)
    assert inner.calls == [] and second.cache_hits == 1 and second.requests == 0

    other = _CountingEmbedder()
    other.model = "another"
    third = MemoizedEmbedder(other, cache_path=path)
    await third.embed("句子")
    assert other.calls == ["句子"]
