"""Fixed weather replies for one control-api session.

The prior user line lives on the process. A city-only follow-up is the next
turn in that same session, not a second copy of the weather question.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import Request

from services.agent.src.providers.weather_places import is_city_name_report
from services.common.companion_response_safety import fixed_companion_reply
from services.common.companions import companion_definition
from services.common.realtime_information import (
    fixed_realtime_reply,
    mentions_weather,
    weather_lookup_configured,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.mode_policy import FrozenMode


def remember_user_query(request: Request, *, session_id: str, query: str) -> str:
    store = getattr(request.app.state, "prior_user_queries", None)
    if not isinstance(store, dict):
        store = {}
        request.app.state.prior_user_queries = store
    prior = str(store.get(session_id, ""))
    store[session_id] = query
    return prior


def fixed_reply_for_query(
    *,
    query: str,
    frozen: FrozenMode,
    now: datetime,
    settings: ControlSettings,
    request: Request,
    session_id: str,
) -> str | None:
    companion = companion_definition(frozen.companion_style_id)
    safety_reply = fixed_companion_reply(
        query=query,
        is_companion=frozen.interaction_mode == "companion",
        display_name=companion.display_name if companion is not None else None,
        style_description=companion.style_description if companion is not None else None,
    )
    if safety_reply is not None:
        return safety_reply
    if frozen.interaction_mode != "companion":
        return None
    prior = remember_user_query(request, session_id=session_id, query=query)
    return fixed_realtime_reply(
        query=query,
        now=now,
        weather_lookup_available=weather_lookup_configured(settings),
        city_followup=mentions_weather(prior) and is_city_name_report(query),
    )
