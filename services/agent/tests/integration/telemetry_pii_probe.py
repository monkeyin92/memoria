"""Real-exporter PII probe, runnable as a test helper or inside the image.

The release verifier's canary uses ``InMemorySpanExporter``: it proves the SDK's
gating logic but never exercises an exporter, a wire payload or a receiver.  This
probe closes that gap with the real OTLP/HTTP exporter against a loopback
collector, and asserts on the bytes that actually arrived.

It is deliberately pytest-free so the exact same code can run inside the built
candidate image:

    /app/.venv/bin/python -m services.agent.tests.integration.telemetry_pii_probe

Each case runs in a FRESH interpreter (``sys.executable -m ...telemetry_pii_emitter``)
because ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` is read when
``livekit.agents.telemetry.gen_ai`` is imported.

The SDK has two independent switches; both must be open for content to leave the
process (measured here, not inferred from the docstrings):

- ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT``: whether the SDK writes
  message content onto spans; unset/empty mean ON;
- ``LIVEKIT_TELEMETRY_ALLOW_PII``: whether the in-process filter lets that content
  reach exporters; unset means ON, and the shipped bootstrap forces ``0``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from services.agent.tests.integration.telemetry_pii_emitter import (
    CANARY_MODEL,
    CANARY_NAME,
    CANARY_PRIVATE_TEXT,
)

CAPTURE_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
ALLOW_PII_ENV = "LIVEKIT_TELEMETRY_ALLOW_PII"
EMITTER_MODULE = "services.agent.tests.integration.telemetry_pii_emitter"
_REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class ProbeCase:
    name: str
    capture_env: str | None
    allow_pii_env: str | None
    skip_bootstrap: bool
    expect_canary: bool
    expectation: str


CASES: tuple[ProbeCase, ...] = (
    ProbeCase(
        name="unset-env-defaults-close-content",
        capture_env=None,
        allow_pii_env=None,
        skip_bootstrap=False,
        expect_canary=False,
        expectation="the shipped bootstrap injects both switches as off",
    ),
    ProbeCase(
        name="empty-env-is-not-a-safe-value",
        capture_env="",
        allow_pii_env="",
        skip_bootstrap=False,
        expect_canary=False,
        expectation="empty values are replaced by the bootstrap defaults",
    ),
    ProbeCase(
        name="capture-on-without-pii-opt-in-still-withholds",
        capture_env="1",
        allow_pii_env=None,
        skip_bootstrap=False,
        expect_canary=False,
        expectation="the bootstrap closes the second switch, so content stays in-process",
    ),
    ProbeCase(
        name="explicit-operator-opt-in-carries-content",
        capture_env="1",
        allow_pii_env="1",
        skip_bootstrap=False,
        expect_canary=True,
        expectation="both switches on: the harness really can carry content",
    ),
    ProbeCase(
        name="without-bootstrap-content-leaks",
        capture_env=None,
        allow_pii_env=None,
        skip_bootstrap=True,
        expect_canary=True,
        expectation="anti-regression: the probe is not vacuous",
    ),
)


class OtlpCollector:
    """A loopback OTLP/HTTP receiver that keeps the raw request bodies."""

    def __init__(self) -> None:
        self.raw_bodies: list[bytes] = []
        self.requests: list[Any] = []
        collector = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if self.path.rstrip("/") != "/v1/traces":
                    self.send_error(404)
                    return
                collector.raw_bodies.append(body)
                request = trace_service_pb2.ExportTraceServiceRequest()
                request.ParseFromString(body)
                collector.requests.append(request)
                payload = trace_service_pb2.ExportTraceServiceResponse().SerializeToString()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> OtlpCollector:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1/traces"

    def exported_attributes(self) -> str:
        attributes: list[str] = []
        for request in self.requests:
            for resource_spans in request.resource_spans:
                for scope_spans in resource_spans.scope_spans:
                    for span in scope_spans.spans:
                        # OTLP carries attributes as repeated KeyValue, not a mapping.
                        for key_value in span.attributes:
                            value = key_value.value
                            which = value.WhichOneof("value")
                            attributes.append(
                                f"{key_value.key}={getattr(value, which) if which else ''}"
                            )
        return "\n".join(attributes)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    case: ProbeCase
    ok: bool
    detail: str
    exported: str


def run_case(case: ProbeCase, *, timeout_s: float = 180.0) -> ProbeResult:
    with OtlpCollector() as collector:
        env = os.environ.copy()
        env.pop(CAPTURE_ENV, None)
        env.pop(ALLOW_PII_ENV, None)
        if case.capture_env is not None:
            env[CAPTURE_ENV] = case.capture_env
        if case.allow_pii_env is not None:
            env[ALLOW_PII_ENV] = case.allow_pii_env
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
        )
        command = [sys.executable, "-m", EMITTER_MODULE, "--endpoint", collector.endpoint]
        if case.skip_bootstrap:
            command.append("--skip-bootstrap")
        completed = subprocess.run(
            command,
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
        if completed.returncode != 0:
            return ProbeResult(
                case=case,
                ok=False,
                detail=f"emitter exited {completed.returncode}: {completed.stderr.strip()}",
                exported="",
            )
        exported = collector.exported_attributes()
        if f"gen_ai.request.model={CANARY_MODEL}" not in exported:
            return ProbeResult(
                case=case,
                ok=False,
                detail=f"no span reached the collector: {exported!r}",
                exported=exported,
            )
        attributes_carry = CANARY_NAME in exported or CANARY_PRIVATE_TEXT in exported
        wire_carries = any(
            CANARY_PRIVATE_TEXT.encode("utf-8") in body for body in collector.raw_bodies
        )
        if attributes_carry != case.expect_canary or wire_carries != case.expect_canary:
            return ProbeResult(
                case=case,
                ok=False,
                detail=(
                    "canary expectation not met: "
                    f"attributes={attributes_carry} wire={wire_carries} "
                    f"expected={case.expect_canary} ({case.expectation})"
                ),
                exported=exported,
            )
        return ProbeResult(
            case=case,
            ok=True,
            detail=(
                f"canary {'present' if case.expect_canary else 'absent'} "
                f"({case.expectation})"
            ),
            exported=exported,
        )


def run_all() -> tuple[ProbeResult, ...]:
    return tuple(run_case(case) for case in CASES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="report as JSON")
    args = parser.parse_args(argv)

    results = run_all()
    failed = [result for result in results if not result.ok]
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "case": result.case.name,
                        "ok": result.ok,
                        "detail": result.detail,
                    }
                    for result in results
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for result in results:
            status = "ok" if result.ok else "FAILED"
            print(f"[{status}] {result.case.name}: {result.detail}")
    if failed:
        print(f"real-exporter PII probe failed: {len(failed)}/{len(results)}", file=sys.stderr)
        return 1
    print(f"real-exporter PII probe passed: {len(results)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
