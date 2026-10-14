"""Deterministic execution policy for crisis and support-request evidence."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from services.common.companion_response_safety import (
    CRISIS_SUPPORT_REPLY,
    CRISIS_SUPPORT_REPLY_TEMPLATE_ID,
    SUPPORT_RELAY_REPLY,
    SUPPORT_RELAY_REPLY_TEMPLATE_ID,
    SUPPORT_VICTIM_REPLY,
    SUPPORT_VICTIM_REPLY_TEMPLATE_ID,
    SafetyDecision,
    companion_safety_decision,
)

CrisisAction = Literal["crisis_support", "support_victim", "support_relay", "none"]


class CrisisSemanticVerdict(StrEnum):
    """The semantic classifier's verdict; the policy below is its only reader."""

    SELF_CRISIS = "SELF_CRISIS"
    SUPPORT_FOR_OTHER = "SUPPORT_FOR_OTHER"
    NO_CRISIS = "NO_CRISIS"
    UNSURE = "UNSURE"

# Parent/child-facing copy changed with the support-request tier split; bump ids.
# Names keep ``draft`` until product and compliance sign off the final wording.
CRISIS_SCRIPT_VERSION = "crisis-transfer-draft-v3"
SUPPORT_VICTIM_SCRIPT_VERSION = "support-victim-notify-v1"
SUPPORT_RELAY_SCRIPT_VERSION = "support-relay-pin-v1"

_FIRST_PERSON_RISK_ANCHOR = re.compile(
    r"(?:我|本人).{0,16}(?:不想活|想死|自伤|自残|轻生|结束生命|"
    r"活下去的理由|撑不下去|准备行动|已经行动)"
)


@dataclass(frozen=True, slots=True)
class CrisisRoute:
    action: CrisisAction
    notify_guardian: bool
    attempt_push: bool
    direct_text: str | None
    evidence_source: Literal["deterministic", "semantic_evidence", "none"]
    script_version: str | None = None
    reply_template_id: str | None = None


@dataclass(frozen=True, slots=True)
class _RouteSpec:
    action: CrisisAction
    notify_guardian: bool
    attempt_push: bool
    direct_text: str | None
    script_version: str | None
    reply_template_id: str | None


# One table owns reply / pin / push / version for every deterministic decision.
_DETERMINISTIC_ROUTES: Final[dict[SafetyDecision, _RouteSpec]] = {
    "crisis_self": _RouteSpec(
        action="crisis_support",
        notify_guardian=True,
        attempt_push=True,
        direct_text=CRISIS_SUPPORT_REPLY,
        script_version=CRISIS_SCRIPT_VERSION,
        reply_template_id=CRISIS_SUPPORT_REPLY_TEMPLATE_ID,
    ),
    "support_victim": _RouteSpec(
        action="support_victim",
        notify_guardian=True,
        attempt_push=True,
        direct_text=SUPPORT_VICTIM_REPLY,
        script_version=SUPPORT_VICTIM_SCRIPT_VERSION,
        reply_template_id=SUPPORT_VICTIM_REPLY_TEMPLATE_ID,
    ),
    "support_relay": _RouteSpec(
        action="support_relay",
        notify_guardian=True,
        attempt_push=False,
        direct_text=SUPPORT_RELAY_REPLY,
        script_version=SUPPORT_RELAY_SCRIPT_VERSION,
        reply_template_id=SUPPORT_RELAY_REPLY_TEMPLATE_ID,
    ),
}


def _route_from_spec(
    spec: _RouteSpec,
    *,
    evidence_source: Literal["deterministic", "semantic_evidence"],
) -> CrisisRoute:
    return CrisisRoute(
        action=spec.action,
        notify_guardian=spec.notify_guardian,
        attempt_push=spec.attempt_push,
        direct_text=spec.direct_text,
        evidence_source=evidence_source,
        script_version=spec.script_version,
        reply_template_id=spec.reply_template_id,
    )


def route_crisis(
    query: str,
    *,
    semantic_evidence: CrisisSemanticVerdict | None = None,
) -> CrisisRoute:
    """Resolve evidence without allowing a model verdict to execute an action alone."""

    deterministic = companion_safety_decision(query)
    spec = _DETERMINISTIC_ROUTES.get(deterministic)
    if spec is not None:
        return _route_from_spec(spec, evidence_source="deterministic")
    if (
        semantic_evidence is CrisisSemanticVerdict.SELF_CRISIS
        and _FIRST_PERSON_RISK_ANCHOR.search(query)
    ):
        return _route_from_spec(
            _DETERMINISTIC_ROUTES["crisis_self"],
            evidence_source="semantic_evidence",
        )
    return CrisisRoute(
        action="none",
        notify_guardian=False,
        attempt_push=False,
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


def fixed_safety_reply_log_fields(route: CrisisRoute) -> dict[str, str] | None:
    """Ids and route for a spoken fixed safety reply. Never includes child text."""

    if route.reply_template_id is None or route.script_version is None:
        return None
    if route.action == "none":
        return None
    return {
        "route": route.action,
        "reply_template_id": route.reply_template_id,
        "script_version": route.script_version,
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
