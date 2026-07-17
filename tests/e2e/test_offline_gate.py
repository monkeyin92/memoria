"""Thin wrapper so pytest collects offline e2e smoke."""

from __future__ import annotations

import pytest
from services.agent.tests.integration.test_offline_pipeline import test_offline_asr_llm_tts


@pytest.mark.asyncio
async def test_e2e_offline_pipeline() -> None:
    await test_offline_asr_llm_tts()
