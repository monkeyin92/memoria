from __future__ import annotations

import pytest
from scripts.provider_smoke_test import main


@pytest.mark.asyncio
async def test_provider_smoke_may_skip_in_local_offline_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.delenv("MEMORIA_PROVIDER_SMOKE_REQUIRED", raising=False)

    assert await main() == 0


@pytest.mark.asyncio
async def test_required_provider_smoke_fails_closed_when_not_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv("MEMORIA_PROVIDER_SMOKE_REQUIRED", "true")

    assert await main() == 1
