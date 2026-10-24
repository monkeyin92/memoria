"""Deterministic execution policy for crisis and support-request evidence."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Literal

from services.common.companion_response_safety import (
    SafetyDecision,
    companion_safety_decision,
    is_story_retell_context,
)
from services.common.fixed_safety_scripts import (
    FixedSafetyScript,
    FixedSafetyTemplateId,
    script_for_decision,
)

CrisisAction = Literal["crisis_support", "support_victim", "support_relay", "none"]


class CrisisSemanticVerdict(StrEnum):
    """The semantic classifier's verdict; the policy below is its only reader."""

    SELF_CRISIS = "SELF_CRISIS"
    SUPPORT_FOR_OTHER = "SUPPORT_FOR_OTHER"
    NO_CRISIS = "NO_CRISIS"
    UNSURE = "UNSURE"

# Bare self-absence, not a location: 「不在家」「不在学校」 must not reach the
# marker, so only 了/也/都 may sit between 不在 and 没关系.
_FIRST_PERSON_RISK_ANCHOR = re.compile(
    r"(?:我|本人).{0,16}(?:不想活|想死|自伤|自残|轻生|结束生命|"
    r"活下去的理由|撑不下去|准备行动|已经行动|"
    r"消失(?:了)?(?:就)?好|(?:没有|没)(?:被)?(?:生出来|生下来|出生)|"
    r"睡着.{0,4}不醒|不在(?:了)?(?:也|都)?(?:没关系|没事|无所谓|不要紧)|"
    r"活着.{0,6}(?:没意思|没有意思|没意义))"
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


_PROTECTION_RANK: Final[dict[FixedSafetyTemplateId, int]] = {
    "support-other-v1": 1,
    "victim-support-v1": 2,
    "crisis-self-v1": 3,
}


def more_protective_local_script(
    query: str, planned: FixedSafetyScript | None
) -> FixedSafetyScript | None:
    """The local deterministic script when it protects more than ``planned``.

    The planner (control-api) and the media bridge ship as separate images, so
    a partial rollout leaves two versions of these rules live at once.  The
    child hears the more protective of the two; ``None`` keeps ``planned``.
    """

    local = route_crisis(query).script
    if local is None:
        return None
    if planned is not None and (
        _PROTECTION_RANK[planned.template_id] >= _PROTECTION_RANK[local.template_id]
    ):
        return None
    return local


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
    """Template id and UTC time of a spoken fixed safety reply. Never child text."""

    if route.script is None:
        return None
    return {
        "reply_template_id": route.script.template_id,
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def log_fixed_safety_reply(logger: logging.Logger, route: CrisisRoute) -> None:
    """Emit one structured server log when a fixed safety reply will be spoken."""

    fields = fixed_safety_reply_log_fields(route)
    if fields is None:
        return
    logger.info(
        "fixed_safety_reply reply_template_id=%s at=%s",
        fields["reply_template_id"],
        fields["at"],
    )
