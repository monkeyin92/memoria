"""Ch.5 version pin assertions."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

# The installed distribution and the declared pins must agree. A version drift
# here means either a stray local install or a half-applied lock change, both of
# which invalidate the 1.8.x compatibility evidence.
_EXPECTED_AGENT_STACK = {
    "livekit-agents": "1.8.1",
    "livekit-plugins-openai": "1.8.1",
    "livekit-plugins-silero": "1.8.1",
}

# Updated together by the same group upgrade. RTC 1.1.18 is what Agents 1.8.x
# requires exactly; API 1.2.1 in turn requires protocol >= 1.1.25.
_EXPECTED_TRANSITIVE_STACK = {
    "livekit": "1.1.18",
    "livekit-api": "1.2.1",
    "livekit-protocol": "1.1.26",
    "livekit-local-inference": "0.2.7",
}

_REPO_ROOT = Path(__file__).resolve().parents[4]


def test_livekit_and_openai_versions() -> None:
    for package, exp in _EXPECTED_AGENT_STACK.items():
        try:
            actual = version(package)
        except PackageNotFoundError as exc:
            raise AssertionError(f"{package} not installed") from exc
        assert actual == exp, f"{package}: expected {exp}, got {actual}"

    openai_ver = version("openai")
    major = int(openai_ver.split(".", 1)[0])
    minor = int(openai_ver.split(".")[1])
    assert major == 2 and minor >= 36, f"openai must be >=2.36,<3, got {openai_ver}"


def test_transitive_livekit_stack_matches_the_same_group_upgrade() -> None:
    for package, exp in _EXPECTED_TRANSITIVE_STACK.items():
        try:
            actual = version(package)
        except PackageNotFoundError as exc:
            raise AssertionError(f"{package} not installed") from exc
        assert actual == exp, f"{package}: expected {exp}, got {actual}"


def test_pyproject_pins_match_the_installed_candidate() -> None:
    """Reject a green venv that no longer matches the declared dependency pins."""

    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for package, exp in _EXPECTED_AGENT_STACK.items():
        assert f'"{package}=={exp}"' in text, f"pyproject must pin {package}=={exp}"
    assert '"livekit-api>=1.2.1,<2"' in text, "pyproject must require livekit-api>=1.2.1"
    assert '"livekit-protocol>=1.1.25,<2"' in text, (
        "pyproject must require livekit-protocol>=1.1.25"
    )


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        ("livekit-agents", "1.8.1"),
        ("livekit-plugins-openai", "1.8.1"),
        ("livekit-plugins-silero", "1.8.1"),
        ("livekit", "1.1.18"),
        ("livekit-api", "1.2.1"),
        ("livekit-protocol", "1.1.26"),
        ("livekit-local-inference", "0.2.7"),
    ],
)
def test_lock_records_the_same_group_upgrade(package: str, expected: str) -> None:
    """The committed lock must carry the same versions the venv was built from."""

    lock = (_REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    marker = f'name = "{package}"\nversion = "{expected}"'
    assert marker in lock, f"uv.lock must record {package}=={expected}"
