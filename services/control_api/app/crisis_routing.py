from __future__ import annotations

from fastapi import Request

from services.common.crisis_policy import (
    CrisisRoute,
    crisis_semantic_candidate,
    prior_self_crisis_hits,
    route_crisis,
)


async def route_crisis_with_bounded_evidence(
    request: Request,
    query: str,
    *,
    recall_context: list[str] | None = None,
) -> CrisisRoute:
    prior_hits = prior_self_crisis_hits(query, recall_context or [])
    deterministic = route_crisis(query, prior_self_crisis_hits=prior_hits)
    classifier = getattr(request.app.state, "crisis_semantic_classifier", None)
    if deterministic.action != "none" or not crisis_semantic_candidate(query):
        return deterministic
    if classifier is None:
        return deterministic
    semantic_evidence = await classifier.classify(current_text=query)
    return route_crisis(
        query, semantic_evidence=semantic_evidence, prior_self_crisis_hits=prior_hits
    )
