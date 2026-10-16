from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    interrupted_detail: str | None = None


_ROBOT_SAID_DEFAULT: Final = "机器人已提醒孩子去找身边的大人"

PARENT_ALERT_COPY: Final[Mapping[FixedSafetyTemplateId, ParentAlertCopy]] = MappingProxyType(
    {
        "crisis-self-v1": ParentAlertCopy(
            category_label="情绪和安全",
            summary="孩子说到了和自己情绪、安全有关的内容",
            robot_said=_ROBOT_SAID_DEFAULT,
            parent_action="找个安静的时间陪孩子聊聊，先听孩子说，不急着讲道理。如需帮助可拨打心理援助热线12356",
            interrupted_detail="这次的提醒话术孩子没有听完，如需帮助可拨打心理援助热线12356",
        ),
        "victim-support-v1": ParentAlertCopy(
            category_label="人身安全",
            summary="孩子说到了自己可能受到伤害",
            robot_said=_ROBOT_SAID_DEFAULT,
            parent_action="尽快当面问问孩子发生了什么，如有危险请拨打110",
            interrupted_detail="这次的提醒话术孩子没有听完，如有危险请拨打110",
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

_FALLBACK_TEMPLATE: Final[FixedSafetyTemplateId] = "crisis-self-v1"


def parse_alert_template(reply_template_id: str) -> FixedSafetyTemplateId:
    if reply_template_id in PARENT_ALERT_COPY:
        return cast(FixedSafetyTemplateId, reply_template_id)
    return _FALLBACK_TEMPLATE


def alert_time_label(at: datetime, zone: tzinfo) -> str:
    local = at.astimezone(zone)
    return f"{local.month}月{local.day}日 {local:%H:%M}"


_UNFINISHED_TERMINALS: Final = frozenset(
    {"preempted", "transport_rejected", "error", "skipped", "no_audio"}
)


def playback_left_unfinished(*, terminals: Sequence[str], playback_ended: bool) -> bool:
    observed = tuple(terminals)
    if playback_ended or "playback_ended" in observed or not observed:
        return False
    return any(terminal in _UNFINISHED_TERMINALS for terminal in observed)


__all__ = [
    "PARENT_ALERT_COPY",
    "ParentAlertCopy",
    "alert_time_label",
    "parse_alert_template",
    "playback_left_unfinished",
]
