"""What a parent is told about one safety alert.

Every tier-dependent parent string lives here. The tier is the fixed script
the robot already spoke, stored at crisis time as
``guardian_crisis_events.reply_template_id``. Nothing here reads the child's
words, so a summary cannot carry them. Editing a sentence relabels past alerts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from types import MappingProxyType
from typing import Final, cast, get_args

from services.common.fixed_safety_scripts import FixedSafetyTemplateId


@dataclass(frozen=True, slots=True)
class ParentAlertCopy:
    category_label: str
    summary: str
    robot_said: str
    parent_action: str


_ROBOT_SAID_DEFAULT: Final = "机器人已提醒孩子去找身边的大人"

PARENT_ALERT_COPY: Final[Mapping[FixedSafetyTemplateId, ParentAlertCopy]] = MappingProxyType(
    {
        "crisis-self-v1": ParentAlertCopy(
            category_label="情绪和安全",
            summary="孩子说到了和自己情绪、安全有关的内容",
            robot_said=_ROBOT_SAID_DEFAULT,
            parent_action="找个安静的时间陪孩子聊聊，先听孩子说，不急着讲道理。如需帮助可拨打心理援助热线12356",
        ),
        "victim-support-v1": ParentAlertCopy(
            category_label="人身安全",
            summary="孩子说到了自己可能受到伤害",
            robot_said=_ROBOT_SAID_DEFAULT,
            parent_action="尽快当面问问孩子发生了什么，如有危险请拨打110",
        ),
        "support-other-v1": ParentAlertCopy(
            category_label="孩子提到的安全话题",
            summary="孩子提到了别人的安全，或在问怎么预防",
            robot_said=_ROBOT_SAID_DEFAULT,
            parent_action="可以找时间跟孩子聊聊这个话题，听听孩子是怎么想的",
        ),
    }
)

if set(PARENT_ALERT_COPY) != set(get_args(FixedSafetyTemplateId)):
    raise RuntimeError("every fixed safety script needs parent alert copy")

# Rows written before the tiers carry 'legacy-unspecified' or an old draft id.
# The self-harm guidance names 12356, so it is the safe reading of an unknown tier.
_FALLBACK_TEMPLATE: Final[FixedSafetyTemplateId] = "crisis-self-v1"


def parse_alert_template(reply_template_id: str) -> FixedSafetyTemplateId:
    """Narrow a stored ``reply_template_id`` to a known script, or the fallback."""

    if reply_template_id in PARENT_ALERT_COPY:
        return cast(FixedSafetyTemplateId, reply_template_id)
    return _FALLBACK_TEMPLATE


def alert_time_label(at: datetime, zone: tzinfo) -> str:
    """``10月16日 19:42`` in ``zone``. Never relative, never 今天."""

    local = at.astimezone(zone)
    return f"{local.month}月{local.day}日 {local:%H:%M}"


__all__ = [
    "PARENT_ALERT_COPY",
    "ParentAlertCopy",
    "alert_time_label",
    "parse_alert_template",
]
