"""Ch.5 version pin assertions."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def test_livekit_and_openai_versions() -> None:
    expected = {
        "livekit-agents": "1.6.10",
        "livekit-plugins-openai": "1.6.10",
        "livekit-plugins-silero": "1.6.10",
    }
    for package, exp in expected.items():
        try:
            actual = version(package)
        except PackageNotFoundError as exc:
            raise AssertionError(f"{package} not installed") from exc
        assert actual == exp, f"{package}: expected {exp}, got {actual}"

    openai_ver = version("openai")
    major = int(openai_ver.split(".", 1)[0])
    minor = int(openai_ver.split(".")[1])
    assert major == 2 and minor >= 36, f"openai must be >=2.36,<3, got {openai_ver}"
