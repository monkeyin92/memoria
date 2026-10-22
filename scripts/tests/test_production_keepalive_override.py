"""The production keepalive override renders 180. The base file stays 30."""

from __future__ import annotations

import pytest
from scripts.check_production_keepalive_180 import main, render_timeout


def test_compose_config_applies_the_keepalive_override() -> None:
    assert render_timeout(with_override=True) == "180"
    assert render_timeout(with_override=False) == "30"


def test_check_command_prints_only_the_timeout_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.out == "keepalive_timeout=180\nbase_keepalive_timeout=30\n"
    assert captured.err == ""
