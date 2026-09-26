"""The response-plan cache forgets a deleted subject's plans (P2-03)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from services.control_api.app.response_plan_cache import (
    ResponsePlanCache,
    forget_subject_plans,
)


@pytest.mark.asyncio
async def test_invalidate_subject_drops_only_that_subjects_plans() -> None:
    cache = ResponsePlanCache()
    child_a = ("session-1", 1, 1, 0, "v1", "person-child")
    child_b = ("session-2", 4, 2, 1, "v1", "person-child")
    holder = ("session-1", 2, 1, 0, "v1", "account-holder")
    for key in (child_a, child_b, holder):
        await cache.put(key, "fingerprint", {"grounded_items": [key[5]]})

    dropped = await forget_subject_plans(SimpleNamespace(response_plan_cache=cache), "person-child")

    assert dropped == 2
    assert await cache.get(child_a, "fingerprint") is None
    assert await cache.get(child_b, "fingerprint") is None
    assert await cache.get(holder, "fingerprint") == {"grounded_items": ["account-holder"]}


@pytest.mark.asyncio
async def test_forgetting_without_a_cache_is_a_no_op() -> None:
    assert await forget_subject_plans(SimpleNamespace(), "person-child") == 0
