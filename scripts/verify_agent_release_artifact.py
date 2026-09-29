"""Assert the built Agent image really carries the released candidate.

The image runs the Voice Core media bridge (``scripts.run_media_bridge``); the
LiveKit Agent worker it used to run is retired, but the bridge still imports
``livekit.agents`` (LLM/TTS/STT adapters and the GenAI telemetry module), so the
pinned SDK and its privacy defaults remain part of the release contract.

Run inside the artifact (`/app/.venv/bin/python -m scripts.verify_agent_release_artifact`)
so the checks exercise the shipped venv and code, not the build machine. Exits
non-zero on the first failed assertion, which fails the image build.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version

# The libraries the bridge imports. The privacy defaults below depend on the
# 1.8.x telemetry behaviour (content capture on unless disabled), so a silent
# SDK drift must fail the build rather than quietly change what is exported.
EXPECTED_VERSIONS = {
    "livekit-agents": "1.8.1",
    "livekit-plugins-openai": "1.8.1",
    "livekit": "1.1.18",
}

# The factory the production compose file hands the bridge
# (MEDIA_BRIDGE_SESSION_FACTORY in docker-compose.production.yml).
PRODUCTION_SESSION_FACTORY = (
    "services.agent.src.media_agent_factory:build_production_media_session_factory"
)


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
    """Validate the bridge's privacy gates in clean child processes.

    Prevents false-greens:
    1. Tests the shared privacy bootstrap with unset env vars.
    2. Tests the Media Bridge entrypoint bootstrap with unset env vars.
    3. Runs real InMemorySpanExporter canary test to prove no PII/content leaks.
    4. Runs negative control to prove that omitting the bootstrap call would FAIL.
    5. Verifies explicit operator overrides are respected.

    ``_check_real_exporter_privacy`` then repeats the canary over a real OTLP/HTTP
    export, which is the only form that proves what an exporter actually receives.
    """
    clean_env = {
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": None,
        "LIVEKIT_TELEMETRY_ALLOW_PII": None,
    }

    # 1. Shared bootstrap in fresh process
    agent_code = """
import os
from services.agent.src.telemetry_privacy import _apply_telemetry_privacy_defaults
applied = _apply_telemetry_privacy_defaults()
assert applied.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT") == "0"
assert applied.get("LIVEKIT_TELEMETRY_ALLOW_PII") == "0"
assert os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT") == "0"
assert os.environ.get("LIVEKIT_TELEMETRY_ALLOW_PII") == "0"

from livekit.agents.telemetry import gen_ai
assert gen_ai.capture_content_enabled() is False, "privacy bootstrap failed to disable gen_ai content capture"
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
from services.agent.src.telemetry_privacy import _apply_telemetry_privacy_defaults
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
from services.agent.src.telemetry_privacy import _apply_telemetry_privacy_defaults
applied = _apply_telemetry_privacy_defaults()
assert applied["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "0"
assert applied["LIVEKIT_TELEMETRY_ALLOW_PII"] == "0"
"""
    _run_subprocess_check(override_code, env_overrides=override_env)

    print("telemetry privacy defaults (bootstrap + Bridge + Canary + Anti-regression) OK")


def _check_bridge_wiring() -> None:
    """The shipped tree must resolve the bridge and its production session factory.

    Runs in a clean child so the bridge entrypoint is imported exactly as the
    container does (``-m scripts.run_media_bridge`` applies the privacy
    defaults at import) and the lazily imported OpenAI-compatible LLM plugin
    the factory needs is present in the venv.
    """

    module_name, _, attribute = PRODUCTION_SESSION_FACTORY.partition(":")
    code = f"""
import importlib

import scripts.run_media_bridge as bridge
assert callable(bridge.main), "run_media_bridge has no main()"
builder = getattr(importlib.import_module({module_name!r}), {attribute!r}, None)
assert callable(builder), "production media session factory is not callable"
from livekit.plugins import openai  # noqa: F401  (media_agent_factory imports it lazily)
from services.agent.src.agent import DuplexVoiceAgent  # noqa: F401
"""
    _run_subprocess_check(
        code,
        env_overrides={
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": None,
            "LIVEKIT_TELEMETRY_ALLOW_PII": None,
        },
    )
    print("media bridge wiring OK")


def _check_real_exporter_privacy() -> None:
    """Drive the same privacy canary through a REAL OTLP exporter.

    The in-process canary above uses ``InMemorySpanExporter``: it proves the SDK's
    gating logic but never exercises an exporter, a wire payload or a receiver.
    This step runs ``services.agent.tests.integration.telemetry_pii_probe``, which
    starts the shipped bootstrap in fresh interpreters, exports over real OTLP/HTTP
    to a loopback collector and asserts on the bytes that arrived -- including that
    an explicit operator opt-in still carries content (so the gate is not vacuous).
    """

    code = """
from services.agent.tests.integration.telemetry_pii_probe import run_all

results = run_all()
failed = [(result.case.name, result.detail) for result in results if not result.ok]
assert not failed, failed
print(f"real-exporter PII probe OK: {len(results)} cases")
"""
    _run_subprocess_check(
        code,
        env_overrides={
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": None,
            "LIVEKIT_TELEMETRY_ALLOW_PII": None,
        },
    )
    print("real-exporter telemetry privacy probe OK")


def main() -> int:
    _check_versions()
    _check_privacy_defaults()
    _check_real_exporter_privacy()
    _check_bridge_wiring()
    print("agent release artifact verification PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
