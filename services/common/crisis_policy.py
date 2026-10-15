"""Deterministic execution policy for crisis and support-request evidence."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from services.common.companion_response_safety import (
    SafetyDecision,
    companion_safety_decision,
    is_story_retell_context,
)
from services.common.fixed_safety_scripts import FixedSafetyScript, script_for_decision

CrisisAction = Literal["crisis_support", "support_victim", "support_relay", "none"]


class CrisisSemanticVerdict(StrEnum):
    """The semantic classifier's verdict; the policy below is its only reader."""

    SELF_CRISIS = "SELF_CRISIS"
    SUPPORT_FOR_OTHER = "SUPPORT_FOR_OTHER"
    NO_CRISIS = "NO_CRISIS"
    UNSURE = "UNSURE"

_FIRST_PERSON_RISK_ANCHOR = re.compile(
    r"(?:我|本人).{0,16}(?:不想活|想死|自伤|自残|轻生|结束生命|"
    r"活下去的理由|撑不下去|准备行动|已经行动)"
)


@dataclass(frozen=True, slots=True)
class CrisisRoute:
    action: CrisisAction
    notify_guardian: bool
    attempt_push: bool
    script: FixedSafetyScript | None
    evidence_source: Literal["deterministic", "semantic_evidence", "none"]
    possibly_story_retell: bool = False


@dataclass(frozen=True, slots=True)
class _RouteSpec:
    action: CrisisAction
    notify_guardian: bool
    attempt_push: bool


_DETERMINISTIC_ROUTES: Final[dict[SafetyDecision, _RouteSpec]] = {
    "crisis_self": _RouteSpec("crisis_support", notify_guardian=True, attempt_push=True),
    "support_victim": _RouteSpec("support_victim", notify_guardian=True, attempt_push=True),
    "support_relay": _RouteSpec("support_relay", notify_guardian=True, attempt_push=False),
}


def _route(
    decision: SafetyDecision,
    *,
    evidence_source: Literal["deterministic", "semantic_evidence"],
    possibly_story_retell: bool = False,
) -> CrisisRoute:
    spec = _DETERMINISTIC_ROUTES[decision]
    return CrisisRoute(
        action=spec.action,
        notify_guardian=spec.notify_guardian,
        attempt_push=spec.attempt_push,
        script=script_for_decision(decision),
        evidence_source=evidence_source,
        possibly_story_retell=possibly_story_retell,
    )


def route_crisis(
    query: str,
    *,
    semantic_evidence: CrisisSemanticVerdict | None = None,
) -> CrisisRoute:
    """Resolve evidence without allowing a model verdict to execute an action alone."""

    deterministic = companion_safety_decision(query)
    # Annotation only: cue word required, and never set on a none route.
    story = is_story_retell_context(query)
    if deterministic in _DETERMINISTIC_ROUTES:
        return _route(
            deterministic,
            evidence_source="deterministic",
            possibly_story_retell=story,
        )
    if (
        semantic_evidence is CrisisSemanticVerdict.SELF_CRISIS
        and _FIRST_PERSON_RISK_ANCHOR.search(query)
    ):
        return _route(
            "crisis_self",
            evidence_source="semantic_evidence",
            possibly_story_retell=story,
        )
    return CrisisRoute(
        action="none",
        notify_guardian=False,
        attempt_push=False,
        script=None,
        evidence_source="none",
        possibly_story_retell=False,
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


def fixed_safety_reply_log_fields(route: CrisisRoute) -> dict[str, str] | None:
    """Ids and route for a spoken fixed safety reply. Never includes child text."""

    if route.script is None:
        return None
    return {
        "route": route.action,
        "reply_template_id": route.script.template_id,
        "script_version": route.script.template_id,
    }


def log_fixed_safety_reply(logger: logging.Logger, route: CrisisRoute) -> None:
    """Emit one structured server log when a fixed safety reply will be spoken."""

    fields = fixed_safety_reply_log_fields(route)
    if fields is None:
        return
    logger.info(
        "fixed_safety_reply route=%s reply_template_id=%s script_version=%s",
        fields["route"],
        fields["reply_template_id"],
        fields["script_version"],
    )
