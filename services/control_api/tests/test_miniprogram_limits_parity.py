from __future__ import annotations

import re
from pathlib import Path

import pytest
from services.common.daily_chat_limit import DAILY_CHAT_SECONDS_KEY
from services.policy.engine import DEFAULT_QUIET_HOURS

MINIPROGRAM_UTILS = Path(__file__).resolve().parents[3] / "apps/miniprogram/utils"


@pytest.mark.hermetic
def test_the_miniprogram_reads_the_daily_budget_under_the_signed_key() -> None:
    source = (MINIPROGRAM_UTILS / "session-limits.js").read_text(encoding="utf-8")
    assert re.findall(r'^const DAILY_CHAT_SECONDS_KEY = "([^"]*)";$', source, re.MULTILINE) == [
        DAILY_CHAT_SECONDS_KEY
    ]


@pytest.mark.hermetic
def test_the_miniprogram_defaults_to_the_quiet_hours_the_policy_signs() -> None:
    source = (MINIPROGRAM_UTILS / "quiet-hours.js").read_text(encoding="utf-8")
    assert re.findall(
        r'^const DEFAULT_QUIET_HOURS = Object\.freeze\(\{ start: "([^"]*)", end: "([^"]*)" \}\);$',
        source,
        re.MULTILINE,
    ) == [DEFAULT_QUIET_HOURS]
