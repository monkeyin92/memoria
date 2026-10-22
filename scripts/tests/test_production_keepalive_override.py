from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.check_production_keepalive_180 import main, render_timeout

ROOT = Path(__file__).resolve().parents[2]
OVERRIDE = ROOT / "docker-compose.production.keepalive-180.override.yml"
SWITCH = ROOT / "scripts" / "switch_production_keepalive_180.sh"
ROLLBACK = ROOT / "scripts" / "rollback_production_keepalive_180.sh"
EXPECTED_OVERRIDE = (
    "services:\n"
    "  voice-core-media-bridge:\n"
    "    environment:\n"
    '      MEDIA_OWNER_SILENCE_TIMEOUT_S: "180"\n'
)
FORBIDDEN = (
    "sudo",
    "pkexec",
    "doas",
    "runuser",
    "nsenter",
    "capsh",
    "setpriv",
    "machinectl",
    "printenv",
    "source ",
    ".env",
    "/etc/memoria",
    "export ",
    "eval ",
    "`",
)


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


def test_override_file_is_the_switch_contract() -> None:
    assert OVERRIDE.read_text(encoding="utf-8") == EXPECTED_OVERRIDE
    script = SWITCH.read_text(encoding="utf-8")
    for line in EXPECTED_OVERRIDE.splitlines():
        assert f"'{line}'" in script


def test_switch_and_rollback_only_print_the_manual_command() -> None:
    for path, marker in (
        (SWITCH, "keepalive_timeout=180"),
        (ROLLBACK, "keepalive_timeout=30"),
    ):
        text = path.read_text(encoding="utf-8")
        assert marker in text
        assert "sha256sum --" in text
        assert "This script does not." in text
        for line in text.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("docker ")
            assert not stripped.startswith("sudo")
        lowered = text.lower()
        for token in FORBIDDEN:
            assert token not in lowered
        subprocess.run(["bash", "-n", str(path)], check=True)
