"""Assert the built Agent image really carries a runnable Voice Core media bridge.

The image runs the Voice Core media bridge (``scripts.run_media_bridge``). Its
provider adapters (FunASR, the OpenAI-compatible chat model, Doubao and
CosyVoice TTS) no longer use ``livekit-agents``; the gate proves the shipped
tree still wires the production session factory, loads the pinned DTLN
denoiser (the only production user of ``onnxruntime``), and imports no
``livekit`` module.

Run inside the artifact (`/app/.venv/bin/python -m scripts.verify_agent_release_artifact`)
so the checks exercise the shipped venv and code, not the build machine. Exits
non-zero on the first failed assertion, which fails the image build.
"""

from __future__ import annotations

import os
import subprocess
import sys

# The factory the production compose file hands the bridge
# (MEDIA_BRIDGE_SESSION_FACTORY in docker-compose.production.yml).
PRODUCTION_SESSION_FACTORY = (
    "services.agent.src.media_agent_factory:build_production_media_session_factory"
)

# Appended to a check that imported the bridge: no livekit module was loaded.
NO_LIVEKIT_IMPORTED = """
import sys
leaked = sorted(name for name in sys.modules if name == "livekit" or name.startswith("livekit."))
assert not leaked, f"the bridge imports livekit again: {leaked}"
"""


def _run_subprocess_check(code: str) -> None:
    """Run verification code in a fresh child interpreter."""
    res = subprocess.run(
        [sys.executable, "-c", code],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        raise SystemExit(f"Subprocess check failed with exit code {res.returncode}")


def _check_bridge_wiring() -> None:
    """The shipped tree must resolve the bridge, its factory and its providers.

    Runs in a clean child so the bridge entrypoint is imported exactly as the
    container does. The import graph, not the venv, is what must stay free of
    ``livekit``: a source-overlay image still carries the package in the venv
    of its older dependency base.
    """

    module_name, _, attribute = PRODUCTION_SESSION_FACTORY.partition(":")
    code = f"""
import importlib

import scripts.run_media_bridge as bridge
assert callable(bridge.main), "run_media_bridge has no main()"
builder = getattr(importlib.import_module({module_name!r}), {attribute!r}, None)
assert callable(builder), "production media session factory is not callable"
# media_agent_factory imports the chat model lazily; the TTS factory imports
# its adapters lazily as well.
from services.agent.src.providers.openai_chat import OpenAIChatModel  # noqa: F401
from services.agent.src.providers.doubao_tts import DoubaoTTS  # noqa: F401
from services.agent.src.providers.cosyvoice_tts import CosyVoiceTTS  # noqa: F401
from services.agent.src.providers.funasr_stt import FunASRSession  # noqa: F401
from services.agent.src.reply_pipeline import ReplyPipeline  # noqa: F401
"""
    _run_subprocess_check(code + NO_LIVEKIT_IMPORTED)
    print("media bridge wiring OK (no livekit import)")


def _check_dtln_denoiser() -> None:
    """Every media session builds the required DTLN denoiser on onnxruntime."""

    code = """
from services.agent.src.voice_core.deep_denoiser import DeepDenoiser

denoiser = DeepDenoiser()
pcm = b"\\x00\\x01" * 1600
assert len(denoiser.process(pcm)) == len(pcm), "DTLN changed the PCM length"
"""
    _run_subprocess_check(code)
    print("DTLN denoiser OK")


def _check_media_telemetry_privacy() -> None:
    """Prove the self-owned media telemetry bridge does not export raw identity/content."""

    code = """
import json

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from services.agent.src.observability.media_otel import MediaOtelBridge
from services.agent.src.voice_core.telemetry import MediaTelemetry, TraceContext, TurnTimeline

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

context = TraceContext(
    trace_id="trace-canary-8f2c",
    session_id="session-private-canary-8f2c",
    stream_epoch=7,
    turn_id=3,
    generation_id=4,
    tool_epoch=5,
    device_id="device-private-canary-8f2c",
)
with MediaOtelBridge().span("device.playback_ended", context):
    pass

exported = json.dumps(
    [
        {key: str(value) for key, value in (span.attributes or {}).items()}
        for span in exporter.get_finished_spans()
    ],
    ensure_ascii=False,
)
assert "session-private-canary-8f2c" not in exported
assert "device-private-canary-8f2c" not in exported
assert "trace-canary-8f2c" in exported

timeline = TurnTimeline(context)
for forbidden in ("transcript", "audio_payload", "provider_token", "session_id"):
    try:
        timeline.add("asr.final", fields={forbidden: "private-canary"})
    except ValueError:
        pass
    else:
        raise AssertionError(f"sensitive field was accepted: {forbidden}")

try:
    MediaTelemetry().inc("voice_kws_hits_total", labels={"session_id": "private-canary"})
except ValueError:
    pass
else:
    raise AssertionError("session_id telemetry label was accepted")

try:
    MediaOtelBridge().span("not.allowlisted", context)
except ValueError:
    pass
else:
    raise AssertionError("unallowlisted media span was accepted")
"""
    _run_subprocess_check(code)
    print("media telemetry privacy gate OK")


def main() -> int:
    _check_bridge_wiring()
    _check_media_telemetry_privacy()
    _check_dtln_denoiser()
    print("agent release artifact verification PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
