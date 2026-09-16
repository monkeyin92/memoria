"""Emit one GenAI span through a REAL OTLP exporter, for the PII gate.

Run in a fresh interpreter by
``services/agent/tests/integration/test_telemetry_pii_real_exporter.py``:

    python -m services.agent.tests.integration.telemetry_pii_emitter \
        --endpoint http://127.0.0.1:PORT/v1/traces [--skip-bootstrap]

With ``--skip-bootstrap`` the process deliberately mirrors what the SDK does on
its own (no ``_apply_telemetry_privacy_defaults``), which is the anti-regression
control: the canary must then reach the collector.

Import order mirrors the shipped entrypoint (``services.agent.src.main``): the
privacy defaults are applied by ``main()`` *before* anything imports
``livekit.agents.telemetry.gen_ai``, which is where the SDK reads its
content-capture switch (at import time).
"""

from __future__ import annotations

import argparse
import sys

CANARY_NAME = "诸葛西柚-假名-canary-real-4b91"
CANARY_PRIVATE_TEXT = "我儿子小周对猫毛过敏，家住假地址-canary-real-4b91"
CANARY_MODEL = "canary-model-real-4b91"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--skip-bootstrap", action="store_true")
    args = parser.parse_args(argv)

    if not args.skip_bootstrap:
        from services.agent.src.main import _apply_telemetry_privacy_defaults

        _apply_telemetry_privacy_defaults()

    from livekit.agents.telemetry import gen_ai, traces
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=args.endpoint, timeout=5))
    )
    traces.set_tracer_provider(provider)

    tracer = provider.get_tracer("memoria.pii.real-exporter")
    with tracer.start_as_current_span("gen_ai.chat") as span:
        # Non-content attributes must survive either way: they prove the span
        # really travelled through the exporter.
        gen_ai.set_request_attributes(
            span,
            operation="chat",
            provider="openai",
            model=CANARY_MODEL,
        )
        # Content goes through the one gated writer.
        gen_ai.set_content_attributes(
            span,
            system_instructions=[{"type": "text", "content": CANARY_NAME}],
            input_messages=[
                {"role": "user", "parts": [{"type": "text", "content": CANARY_PRIVATE_TEXT}]}
            ],
        )
    provider.shutdown()
    print(f"capture_content_enabled={gen_ai.capture_content_enabled()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
