"""Run the self-authored media-v1 gRPC bridge as a bounded Voice Core process.

This process owns the Media Edge ↔ Voice Core transport boundary only.  A
deployment that wants to attach FunASR/LLM/TTS supplies handlers through the
``MediaBridgeGrpcServer`` API; the default process deliberately does not
invent a second orchestration stack.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import signal
import sys
from collections.abc import Callable
from typing import Any, cast

from services.agent.src.config import load_settings
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import GLOBAL_METRICS
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer, MediaBridgeTLS
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry

logger = logging.getLogger("memoria.media_bridge")


def _load_session_factory(settings: Any) -> Callable[[SessionIdentity], Any] | None:
    """Load the single session-scoped production Agent composition root."""

    reference = os.getenv("MEDIA_BRIDGE_SESSION_FACTORY", "").strip()
    if not reference:
        if getattr(settings, "environment", "development") == "production":
            raise ValueError("production media bridge requires MEDIA_BRIDGE_SESSION_FACTORY")
        return None
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("MEDIA_BRIDGE_SESSION_FACTORY must be module:callable")
    builder = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(builder):
        raise ValueError("MEDIA_BRIDGE_SESSION_FACTORY is not callable")
    session_factory = builder(settings)
    if not callable(session_factory):
        raise ValueError("media bridge session factory did not return a callable")
    return cast(Callable[[SessionIdentity], Any], session_factory)


def _load_provider_factory(
    settings: Any,
) -> Callable[[SessionIdentity], Any] | None:
    """Load an explicitly deployed provider adapter without hard-coding vendors."""

    reference = os.getenv("MEDIA_BRIDGE_PROVIDER_FACTORY", "").strip()
    if not reference:
        if getattr(settings, "environment", "development") == "production":
            raise ValueError("production media bridge requires MEDIA_BRIDGE_PROVIDER_FACTORY")
        return None
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("MEDIA_BRIDGE_PROVIDER_FACTORY must be module:callable")
    factory = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(factory):
        raise ValueError("MEDIA_BRIDGE_PROVIDER_FACTORY is not callable")
    provider_factory = factory(settings)
    if not callable(provider_factory):
        raise ValueError("media bridge provider factory did not return a callable")
    return cast(Callable[[SessionIdentity], Any], provider_factory)


def _load_runtime_factory(
    settings: Any,
) -> Callable[[str], DuplexRuntime] | None:
    """Load the full Agent runtime factory; never invent a production shell."""

    reference = os.getenv("MEDIA_BRIDGE_RUNTIME_FACTORY", "").strip()
    if not reference:
        if getattr(settings, "environment", "development") == "production":
            raise ValueError("production media bridge requires MEDIA_BRIDGE_RUNTIME_FACTORY")
        return None
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("MEDIA_BRIDGE_RUNTIME_FACTORY must be module:callable")
    factory = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(factory):
        raise ValueError("MEDIA_BRIDGE_RUNTIME_FACTORY is not callable")
    runtime_factory = factory(settings)
    if not callable(runtime_factory):
        raise ValueError("media bridge runtime factory did not return a callable")
    return cast(Callable[[str], DuplexRuntime], runtime_factory)


def _tls_for_settings(settings: object) -> MediaBridgeTLS | None:
    enabled = bool(getattr(settings, "media_bridge_mtls", False))
    if not enabled:
        if getattr(settings, "environment", "development") == "production":
            raise ValueError("production media bridge cannot run without mTLS")
        return None
    return MediaBridgeTLS.from_files(
        private_key_file=str(getattr(settings, "media_bridge_tls_key_file", "")),
        certificate_chain_file=str(getattr(settings, "media_bridge_tls_cert_file", "")),
        client_ca_file=str(getattr(settings, "media_bridge_client_ca_file", "")),
    )


async def run() -> None:
    settings = load_settings(require_keys=False)
    if not settings.media_bridge_grpc_enabled:
        raise RuntimeError(
            "MEDIA_BRIDGE_GRPC_ENABLED=false; enable the bridge explicitly before starting it"
        )
    logging.basicConfig(level=settings.log_level)
    prometheus_port = int(getattr(settings, "prometheus_port", 0))
    if prometheus_port > 0:
        try:
            # The media bridge is a separate process from the LiveKit worker;
            # expose its own registry so the SLO sidecar never reports the
            # wrong process's counters.
            GLOBAL_METRICS.start_http_server(prometheus_port)
        except OSError:
            logger.warning("media bridge metrics exporter unavailable", exc_info=True)
    server = MediaBridgeGrpcServer(
        max_pending_audio_frames=settings.media_bridge_max_pending_audio_frames,
        max_pending_messages=settings.media_bridge_max_pending_messages,
        allow_go_shadow=settings.media_bridge_go_shadow_enabled,
    )
    session_factory = _load_session_factory(settings)
    try:
        registry: MediaVoiceCoreRegistry | None = None
        if session_factory is not None:
            registry = MediaVoiceCoreRegistry(
                bridge=server,
                session_factory=session_factory,
            )
            registry.install()
            logger.info("media bridge shared Agent session registry installed")
        else:
            provider_factory = _load_provider_factory(settings)
            runtime_factory = _load_runtime_factory(settings)
        if session_factory is None and provider_factory is not None:
            registry = (
                MediaVoiceCoreRegistry(
                    bridge=server,
                    provider_factory=provider_factory,
                    runtime_factory=runtime_factory,
                )
                if runtime_factory is not None
                else MediaVoiceCoreRegistry(
                    bridge=server,
                    provider_factory=provider_factory,
                )
            )
            registry.install()
            logger.info("media bridge Voice Core provider registry installed")
        elif session_factory is None:
            logger.warning(
                "media bridge is running provider-neutral; no Voice Core provider factory configured"
            )
        tls = _tls_for_settings(settings)
        port = await server.start(settings.media_bridge_grpc_addr, tls=tls)
        logger.info(
            "media bridge listening address=%s bound_port=%s mtls=%s",
            settings.media_bridge_grpc_addr,
            port,
            tls is not None,
        )
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stopped.set)
            except (NotImplementedError, RuntimeError):
                # Windows and embedded event loops may not expose POSIX handlers.
                pass
        await stopped.wait()
    finally:
        primary_error = sys.exception()
        stop_error: BaseException | None = None
        try:
            await server.stop()
        except BaseException as exc:
            stop_error = exc
            logger.error("media bridge server shutdown failed", exc_info=True)
        close_factory = getattr(session_factory, "aclose", None)
        close_error: BaseException | None = None
        if callable(close_factory):
            try:
                await close_factory()
            except BaseException as exc:
                close_error = exc
                logger.error("media bridge session factory shutdown failed", exc_info=True)
        if primary_error is None:
            if stop_error is not None:
                raise stop_error
            if close_error is not None:
                raise close_error


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
