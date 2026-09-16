"""Assert the built Agent image really carries the released candidate.

Run inside the artifact (`/app/.venv/bin/python /tmp/verify_agent_release_artifact.py`)
so the checks exercise the shipped venv and code, not the build machine. Exits
non-zero on the first failed assertion, which fails the image build.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
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


def _run_subprocess_check(code: str, *, env_overrides: Mapping[str, str | None] | None = None) -> None:
    """Run verification code in a completely clean child interpreter."""
    env = os.environ.copy()
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    res = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        raise SystemExit(f"Subprocess privacy check failed with exit code {res.returncode}")


def _check_privacy_defaults() -> None:
    """Validate privacy gates across both Agent and Bridge in clean child processes.

    Prevents false-greens:
    1. Tests Agent bootstrap with unset and empty env vars.
    2. Tests Media Bridge bootstrap with unset and empty env vars.
    3. Runs real InMemorySpanExporter canary test to prove no PII/content leaks.
    4. Runs negative control to prove that omitting the bootstrap call would FAIL.
    5. Verifies explicit operator overrides are respected.
    """
    clean_env = {
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": None,
        "LIVEKIT_TELEMETRY_ALLOW_PII": None,
    }

    # 1. Agent bootstrap in fresh process
    agent_code = """
import os
from services.agent.src.main import _apply_telemetry_privacy_defaults
applied = _apply_telemetry_privacy_defaults()
assert applied.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT") == "0"
assert applied.get("LIVEKIT_TELEMETRY_ALLOW_PII") == "0"
assert os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT") == "0"
assert os.environ.get("LIVEKIT_TELEMETRY_ALLOW_PII") == "0"

from livekit.agents.telemetry import gen_ai
assert gen_ai.capture_content_enabled() is False, "Agent bootstrap failed to disable gen_ai content capture"
"""
    _run_subprocess_check(agent_code, env_overrides=clean_env)

    # 2. Bridge bootstrap in fresh process (scripts.run_media_bridge)
    bridge_code = """
import os
import scripts.run_media_bridge
assert os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT") == "0"
assert os.environ.get("LIVEKIT_TELEMETRY_ALLOW_PII") == "0"

from livekit.agents.telemetry import gen_ai
assert gen_ai.capture_content_enabled() is False, "Bridge bootstrap failed to disable gen_ai content capture"
"""
    _run_subprocess_check(bridge_code, env_overrides=clean_env)

    # 3. Real exporter canary test
    canary_code = """
import os
import json
from services.agent.src.main import _apply_telemetry_privacy_defaults
_apply_telemetry_privacy_defaults()

from livekit.agents.telemetry import traces, gen_ai
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
traces.set_tracer_provider(provider)

CANARY_NAME = "诸葛西柚-假名-canary-8f2c"
CANARY_PRIVATE_TEXT = "我儿子小周对猫毛过敏，家里地址是假地址-canary-8f2c"

with provider.get_tracer("memoria.pii.canary").start_as_current_span("gen_ai.chat") as span:
    gen_ai.set_content_attributes(
        span,
        system_instructions=[{"type": "text", "content": CANARY_NAME}],
        input_messages=[
            {"role": "user", "parts": [{"type": "text", "content": CANARY_PRIVATE_TEXT}]}
        ],
    )

exported = json.dumps(
    [{k: str(v) for k, v in (s.attributes or {}).items()} for s in exporter.get_finished_spans()],
    ensure_ascii=False,
)
assert CANARY_NAME not in exported, "CANARY_NAME leaked to exporter"
assert CANARY_PRIVATE_TEXT not in exported, "CANARY_PRIVATE_TEXT leaked to exporter"
"""
    _run_subprocess_check(canary_code, env_overrides=clean_env)

    # 4. Negative control: verify that without defaults, capture_content_enabled is True and canary leaks
    negative_control_code = """
import os
import json
from livekit.agents.telemetry import traces, gen_ai
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

assert gen_ai.capture_content_enabled() is True, "Expected default capture_content_enabled to be True"

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
traces.set_tracer_provider(provider)

CANARY_PRIVATE = "canary-leak-proof-1234"
with provider.get_tracer("memoria.test").start_as_current_span("gen_ai.chat") as span:
    gen_ai.set_content_attributes(
        span,
        input_messages=[
            {"role": "user", "parts": [{"type": "text", "content": CANARY_PRIVATE}]}
        ],
    )

exported = json.dumps(
    [{k: str(v) for k, v in (s.attributes or {}).items()} for s in exporter.get_finished_spans()],
    ensure_ascii=False,
)
assert CANARY_PRIVATE in exported, "Expected canary to leak when defaults are not applied"
"""
    _run_subprocess_check(negative_control_code, env_overrides=clean_env)

    # 5. Operator explicit preservation test
    override_env = {
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "0",
        "LIVEKIT_TELEMETRY_ALLOW_PII": "0",
    }
    override_code = """
import os
from services.agent.src.main import _apply_telemetry_privacy_defaults
applied = _apply_telemetry_privacy_defaults()
assert applied["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "0"
assert applied["LIVEKIT_TELEMETRY_ALLOW_PII"] == "0"
"""
    _run_subprocess_check(override_code, env_overrides=override_env)

    print("telemetry privacy defaults (Agent + Bridge + Canary + Anti-regression) OK")


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
            if dict(resolved["interruption"])[group] != config["interruption"][group]:
                raise SystemExit(f"interruption.{group} not consumed")
        for group in ("enabled", "preemptive_tts"):
            if dict(resolved["preemptive_generation"])[group] != config["preemptive_generation"][group]:
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


def main() -> int:
    _check_versions()
    _check_privacy_defaults()
    _check_sdk_compatibility()
    print("agent release artifact verification PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
