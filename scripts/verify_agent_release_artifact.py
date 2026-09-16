"""Assert the built Agent image really carries the released candidate.

Run inside the artifact (`/app/.venv/bin/python /tmp/verify_agent_release_artifact.py`)
so the checks exercise the shipped venv and code, not the build machine. Exits
non-zero on the first failed assertion, which fails the image build.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version

EXPECTED_VERSIONS = {
    "livekit-agents": "1.8.1",
    "livekit-plugins-openai": "1.8.1",
    "livekit-plugins-silero": "1.8.1",
    "livekit": "1.1.18",
    "livekit-api": "1.2.1",
    "livekit-protocol": "1.1.26",
    "livekit-local-inference": "0.2.7",
}

# Memoria product flags returned alongside the LiveKit turn-handling keys.
_MEMORIA_ONLY_TURN_KEYS = frozenset({"stream_speak_while_think"})


def _check_versions() -> None:
    for package, expected in EXPECTED_VERSIONS.items():
        try:
            actual = version(package)
        except PackageNotFoundError as exc:  # pragma: no cover - build-time guard
            raise SystemExit(f"{package} is not installed in the artifact") from exc
        if actual != expected:
            raise SystemExit(f"{package}: expected {expected}, got {actual}")
    print(f"version pins OK: {EXPECTED_VERSIONS}")


def _check_sdk_compatibility() -> None:
    from livekit.agents import AgentSession
    from livekit.agents.voice.turn import (
        EndpointingOptions,
        InterruptionOptions,
        PreemptiveGenerationOptions,
        TurnHandlingOptions,
    )
    from services.agent.src import session_entrypoint as se

    for device_vad in (False, True):
        config = se.build_turn_handling_config("cn_self_hosted", device_vad=device_vad)
        unknown = set(config) - set(TurnHandlingOptions.__annotations__) - _MEMORIA_ONLY_TURN_KEYS
        if unknown:
            raise SystemExit(f"turn_handling keys would be dropped: {sorted(unknown)}")
        for key, typed_dict in (
            ("endpointing", EndpointingOptions),
            ("interruption", InterruptionOptions),
            ("preemptive_generation", PreemptiveGenerationOptions),
        ):
            dropped = set(config[key]) - set(typed_dict.__annotations__)
            if dropped:
                raise SystemExit(f"{key} keys would be dropped: {sorted(dropped)}")

        kwargs = se.build_session_kwargs(
            vad=None,
            stt=None,
            llm=None,
            tts=None,
            profile="cn_self_hosted",
            offline=True,
            device_vad=device_vad,
        )
        if "turn_handling" not in kwargs:
            raise SystemExit("candidate SDK did not accept our TurnHandlingOptions")
        session = AgentSession(**kwargs)
        resolved = session._opts.turn_handling

        if dict(resolved["endpointing"]) != config["endpointing"]:
            raise SystemExit(f"endpointing not consumed: {dict(resolved['endpointing'])}")
        for group in ("enabled", "mode", "min_duration", "false_interruption_timeout"):
            if resolved["interruption"][group] != config["interruption"][group]:
                raise SystemExit(f"interruption.{group} not consumed")
        for group in ("enabled", "preemptive_tts"):
            if resolved["preemptive_generation"][group] != config["preemptive_generation"][group]:
                raise SystemExit(f"preemptive_generation.{group} not consumed")
        # 1.8.x adds user_turn_limit; the upgrade must not start enforcing it.
        limits = resolved["user_turn_limit"]
        if limits["max_words"] is not None or limits["max_duration"] is not None:
            raise SystemExit(f"user_turn_limit unexpectedly enforced: {limits}")

    half_duplex = AgentSession(
        **se.build_session_kwargs(
            vad=None,
            stt=None,
            llm=None,
            tts=None,
            profile="cn_self_hosted",
            offline=True,
            interruptions_enabled=False,
        )
    )
    if half_duplex._opts.turn_handling["interruption"]["enabled"] is not False:
        raise SystemExit("half-duplex path is interruptible in the artifact")
    if half_duplex._opts.turn_handling["preemptive_generation"]["enabled"] is not False:
        raise SystemExit("preemptive generation enabled in the artifact")
    print("in-artifact SDK compatibility OK")


def _check_privacy_defaults() -> None:
    from services.agent.src import main as main_module

    for name in (
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        "LIVEKIT_TELEMETRY_ALLOW_PII",
    ):
        os.environ.pop(name, None)
    applied = main_module._apply_telemetry_privacy_defaults()
    if applied["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] != "0":
        raise SystemExit(f"content capture not disabled by default: {applied}")
    if applied["LIVEKIT_TELEMETRY_ALLOW_PII"] != "0":
        raise SystemExit(f"PII export not disabled by default: {applied}")
    print(f"telemetry privacy defaults OK: {applied}")


def main() -> int:
    _check_versions()
    _check_sdk_compatibility()
    _check_privacy_defaults()
    print("agent release artifact verification PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
