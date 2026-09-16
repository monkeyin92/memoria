"""Telemetry privacy defaults for LiveKit GenAI and third-party exporters.

LiveKit 1.8.x collects GenAI message content and allows third-party exporters
to receive conversational content by default unless explicitly disabled.
Both switches are read from the environment, and ``capture_content`` is read
when ``livekit.agents.telemetry.gen_ai`` is imported, so they must be set
before any module imports ``livekit.agents``.
"""

from __future__ import annotations

import os

#: LiveKit GenAI content capture and third-party PII export switches.
_TELEMETRY_PRIVACY_DEFAULTS = {
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "0",
    "LIVEKIT_TELEMETRY_ALLOW_PII": "0",
}


def _apply_telemetry_privacy_defaults() -> dict[str, str]:
    """Fail closed on LiveKit content capture / PII export before SDK import.

    An operator who really needs message content in an external GenAI backend
    can still opt in explicitly; the default must not be "collect".
    """

    applied: dict[str, str] = {}
    for name, value in _TELEMETRY_PRIVACY_DEFAULTS.items():
        current = os.environ.get(name)
        if current is None or current.strip() == "":
            os.environ[name] = value
        applied[name] = os.environ[name]
    return applied


__all__ = [
    "_TELEMETRY_PRIVACY_DEFAULTS",
    "_apply_telemetry_privacy_defaults",
]
