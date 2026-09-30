"""Dependency guards for the Agent (Voice Core media bridge) image."""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _declared_dependencies() -> list[str]:
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project.get("optional-dependencies", {})
    return [*project["dependencies"], *(item for group in extras.values() for item in group)]


def test_openai_sdk_is_in_the_supported_range() -> None:
    openai_ver = version("openai")
    major = int(openai_ver.split(".", 1)[0])
    minor = int(openai_ver.split(".")[1])
    assert major == 2 and minor >= 36, f"openai must be >=2.36,<3, got {openai_ver}"


def test_livekit_is_neither_declared_nor_locked() -> None:
    """The bridge's provider adapters no longer use livekit; it must not return."""

    assert not [item for item in _declared_dependencies() if item.startswith("livekit")]
    lock = (_REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "livekit' not in lock


def test_onnxruntime_is_declared_for_the_dtln_denoiser() -> None:
    """DTLN needs onnxruntime; it used to arrive only through livekit-plugins-silero."""

    assert any(item.startswith("onnxruntime") for item in _declared_dependencies())
