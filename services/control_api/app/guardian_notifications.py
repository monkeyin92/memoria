"""Render guardian safety alerts and their delivery state.

The notification list, the unread badge and the single-notification view share
one payload: the parent-facing copy for the reply template, the delivery
receipt, and the interrupted-script detail. Rendering lives here so the router
keeps only the lookups and the response envelopes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from zoneinfo import ZoneInfo

from fastapi import Request

from services.common.companion_response_safety import guardian_attention_copy
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.guardian_push import resolve_minor_display_name
from services.control_api.app.security import AuthenticatedUser
from services.guardian.crisis import GuardianNotification
from services.guardian.crisis_delivery_receipt import (
    crisis_delivery_receipt_payload,
    project_crisis_delivery_receipt,
)
from services.guardian.parent_alert import (
    PARENT_ALERT_COPY,
    alert_time_label,
    interrupted_parent_detail,
)

__all__ = ["safety_alert_items"]



def _profiles(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _settings(request: Request) -> ControlSettings:
    return cast(ControlSettings, request.app.state.settings)


def _safety_alert_item(
    notification: GuardianNotification,
    *,
    minor_display_name: str,
    zone: ZoneInfo,
) -> dict[str, Any]:
    copy = (
        PARENT_ALERT_COPY[notification.reply_template_id]
        if notification.reply_template_id is not None
        else None
    )
    receipt = project_crisis_delivery_receipt(notification)
    return {
        "notification_id": notification.notification_id,
        "minor_user_id": notification.minor_user_id,
        "minor_display_name": minor_display_name,
        "occurred_at": notification.created_at.isoformat(),
        "occurred_label": alert_time_label(notification.created_at, zone),
        "delivery_status": notification.status,
        "delivery_receipt": crisis_delivery_receipt_payload(receipt),
        "channel": notification.channel,
        "message": guardian_attention_copy(
            possibly_story_retell=notification.possibly_story_retell
        ),
        "category_label": "" if copy is None else copy.category_label,
        "summary": "" if copy is None else copy.summary,
        "robot_said": "" if copy is None else copy.robot_said,
        "parent_action": "" if copy is None else copy.parent_action,
        "contains_transcript": False,
        "contains_severity": False,
    }


def _interrupted_detail(notification: GuardianNotification) -> str | None:
    return interrupted_parent_detail(
        notification.reply_template_id,
        script_interrupted=notification.script_interrupted,
    )


async def safety_alert_items(
    request: Request,
    user: AuthenticatedUser,
    notifications: Sequence[GuardianNotification],
) -> list[dict[str, Any]]:
    display_name = resolve_minor_display_name(
        _profiles(request),
        getattr(request.app.state, "identity_service", None),
    )
    zone = ZoneInfo(_settings(request).memoria_timezone)
    items: list[dict[str, Any]] = []
    for item in notifications:
        payload = _safety_alert_item(
            item,
            minor_display_name=str(
                await display_name(user.user_id, item.minor_user_id) or "TA"
            ),
            zone=zone,
        )
        detail = _interrupted_detail(item)
        if detail is not None:
            payload["detail"] = detail
        items.append(payload)
    return items
