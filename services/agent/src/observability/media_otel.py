"""Optional OpenTelemetry bridge for the media telemetry contract."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from services.agent.src.voice_core.telemetry import MEDIA_SPAN_NAMES, TraceContext


@dataclass
class _NoopSpan(AbstractContextManager["_NoopSpan"]):
    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def set_attribute(self, name: str, value: Any) -> None:
        del name, value


class _SpanContext(AbstractContextManager[Any]):
    def __init__(self, manager: AbstractContextManager[Any], context: TraceContext) -> None:
        self._manager = manager
        self._context = context

    def __enter__(self) -> Any:
        active = self._manager.__enter__()
        for key, value in self._context.fields().items():
            if key in {"session_id", "device_id"}:
                continue
            try:
                active.set_attribute(f"memoria.{key}", value)
            except Exception:  # pragma: no cover - exporter-specific
                pass
        return active

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return self._manager.__exit__(exc_type, exc_value, traceback)


class MediaOtelBridge:
    """Start allowlisted spans when an OTLP SDK is installed/configured.

    Importing the voice agent remains safe in minimal test and offline images;
    the bridge simply becomes a no-op until the standard OpenTelemetry SDK is
    available and an application installs a provider/exporter.
    """

    def __init__(self, *, tracer_name: str = "memoria.media") -> None:
        if not tracer_name.strip():
            raise ValueError("tracer_name must be non-empty")
        self._tracer: Any | None = None
        try:
            from opentelemetry import trace

            self._tracer = trace.get_tracer(tracer_name)
        except Exception:  # pragma: no cover - optional runtime wiring
            self._tracer = None

    def span(self, name: str, context: TraceContext) -> AbstractContextManager[Any]:
        if name not in MEDIA_SPAN_NAMES:
            raise ValueError("media span name is not allowlisted")
        if self._tracer is None:
            return _NoopSpan()
        # ``start_as_current_span`` returns a context manager; attach the
        # stable, low-cardinality fence fields when the SDK is present.
        return _SpanContext(self._tracer.start_as_current_span(name), context)


def configure_otel(endpoint: str, *, service_name: str = "memoria-agent") -> bool:
    """Install a bounded OTLP exporter when an endpoint is configured.

    The function is intentionally opt-in and idempotence is left to the
    application bootstrap; test/offline processes with an empty endpoint do
    not create exporter threads.
    """

    if not endpoint.strip():
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=endpoint,
                    insecure=endpoint.startswith("http://"),
                )
            )
        )
        trace.set_tracer_provider(provider)
    except Exception:  # pragma: no cover - exporter/network configuration
        return False
    return True


__all__ = ["MediaOtelBridge", "configure_otel"]
