"""SQL fragments for the lexical half of memory search.

``whole`` and every entry of ``matches`` are ready-made boolean SQL over one document
(the whole query, and one n-gram, as ILIKE); ``text_index`` is the bind index of the
plain query text used for the full-text match.
"""

from __future__ import annotations

from collections.abc import Sequence

from services.archive.memory_domain import (
    content_query_terms,
    edge_content_query_terms,
    lexical_query_terms,
)


def search_terms(text: str, *, with_embedding: bool) -> tuple[str, ...]:
    terms = lexical_query_terms(text)
    if not with_embedding:  # function-word n-grams would let boilerplate outrank
        return content_query_terms(terms)
    return edge_content_query_terms(terms)  # next to embeddings a keyword bonus must name something


def _phrase(text_index: int, whole: str) -> str:
    return f"(document.search_vector @@ websearch_to_tsquery('simple', ${text_index}) OR {whole})"


def text_match(text_index: int, whole: str, matches: Sequence[str]) -> str:
    return f"({_phrase(text_index, whole)[1:-1]}{''.join(f' OR {match}' for match in matches)})"


def hit_count(matches: Sequence[str]) -> str:
    return " + ".join(f"(CASE WHEN {match} THEN 1 ELSE 0 END)" for match in matches)


def text_score(text_index: int, whole: str, matches: Sequence[str]) -> str:
    """Full-text rank or keyword bonus, whichever is larger.

    The bonus grows with how much of the query a memory repeats, so a fragment shared
    with a long question earns almost nothing while a memory that repeats what was asked
    earns the full 0.5.
    """

    phrase = _phrase(text_index, whole)
    bonus = f"CASE WHEN {phrase} THEN 0.5 ELSE 0.0 END"
    if matches:
        share = f"(({hit_count(matches)})::double precision / {len(matches)})"
        bonus = f"CASE WHEN {phrase} THEN 0.5 ELSE 0.5 * POWER({share}, 2) END"
    return (
        "GREATEST(ts_rank(document.search_vector, "
        f"websearch_to_tsquery('simple', ${text_index})), {bonus})"
    )
