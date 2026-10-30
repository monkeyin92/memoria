"""PR #378: control-api and bridge must name the same crisis tier for C1-C9."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from services.common.crisis_policy import route_crisis
from services.control_api.app.crisis_routing import route_crisis_with_bounded_evidence

_DEVICE_CRISIS_SENTENCES = (
    ("我不想活了", "crisis-self-v1"),
    ("我想从楼上跳下去", "crisis-self-escalation"),
    ("我想拿刀划自己", "crisis-self-escalation"),
    ("救命，有人打我", "victim-support-v1"),
    ("有个叔叔摸我不让我告诉妈妈", "victim-support-v1"),
    ("我被关在房间里出不去了", "victim-support-v1"),
    ("我同学被别人打了", "support-other-v1"),
    ("我听说有人欺负小朋友", "support-other-v1"),
    ("怎么才能不被坏人骗走", "support-other-v1"),
)


def _request() -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(crisis_semantic_classifier=None))
    )


@pytest.mark.hermetic
@pytest.mark.parametrize(("query", "expected"), _DEVICE_CRISIS_SENTENCES)
@pytest.mark.asyncio
async def test_control_api_crisis_route_matches_the_local_tier(
    query: str, expected: str
) -> None:
    control_api = await route_crisis_with_bounded_evidence(_request(), query)
    local = route_crisis(query)

    assert control_api.script is not None
    assert local.script is not None
    assert control_api.script.template_id == expected
    assert local.script.template_id == control_api.script.template_id
    assert control_api.action == local.action
