"""Deterministic execution policy for crisis evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from services.agent.src.providers.crisis_semantic_classifier import CrisisSemanticVerdict
from services.common.companion_response_safety import (
    CRISIS_SUPPORT_REPLY,
    companion_safety_decision,
)

CrisisAction = Literal["crisis_support", "support_request", "none"]
CRISIS_SCRIPT_VERSION = "crisis-transfer-draft-v1"

_FIRST_PERSON_RISK_ANCHOR = re.compile(
    r"(?:我|本人).{0,16}(?:不想活|想死|自伤|自残|轻生|结束生命|"
    r"活下去的理由|撑不下去|准备行动|已经行动)"
)


@dataclass(frozen=True, slots=True)
class CrisisRoute:
    action: CrisisAction
    notify_guardian: bool
    direct_text: str | None
    evidence_source: Literal["deterministic", "semantic_evidence", "none"]
    script_version: str | None = None


def route_crisis(
    query: str,
    *,
    semantic_evidence: CrisisSemanticVerdict | None = None,
) -> CrisisRoute:
    """Resolve evidence without allowing a model verdict to execute an action alone."""

    deterministic = companion_safety_decision(query)
    if deterministic == "crisis_self":
        return CrisisRoute(
            action="crisis_support",
            notify_guardian=True,
            direct_text=CRISIS_SUPPORT_REPLY,
            evidence_source="deterministic",
            script_version=CRISIS_SCRIPT_VERSION,
        )
    if deterministic == "support_request":
        return CrisisRoute(
            action="support_request",
            notify_guardian=False,
            direct_text=None,
            evidence_source="deterministic",
        )
    if (
        semantic_evidence is CrisisSemanticVerdict.SELF_CRISIS
        and _FIRST_PERSON_RISK_ANCHOR.search(query)
    ):
        return CrisisRoute(
            action="crisis_support",
            notify_guardian=True,
            direct_text=CRISIS_SUPPORT_REPLY,
            evidence_source="semantic_evidence",
            script_version=CRISIS_SCRIPT_VERSION,
        )
    return CrisisRoute(
        action="none",
        notify_guardian=False,
        direct_text=None,
        evidence_source="none",
    )


def crisis_semantic_candidate(query: str) -> bool:
    """Bound semantic calls to first-person risk language not already resolved.

    The model is never asked to scan history or to discover arbitrary risk.  It
    may only disambiguate one current utterance that already contains the
    deterministic first-person anchor required by :func:`route_crisis`.
    """

    return (
        companion_safety_decision(query) == "none"
        and _FIRST_PERSON_RISK_ANCHOR.search(query) is not None
    )
