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
from collections.abc import Callable
from typing import Any, cast

from services.agent.src.config import load_settings
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer, MediaBridgeTLS
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry

logger = logging.getLogger("memoria.media_bridge")


def _load_provider_factory(
    settings: Any,
) -> Callable[[SessionIdentity], Any] | None:
    """Load an explicitly deployed provider adapter without hard-coding vendors."""

    reference = os.getenv("MEDIA_BRIDGE_PROVIDER_FACTORY", "").strip()
    if not reference:
        if getattr(settings, "environment", "development") == "production":
            raise ValueError(
                "production media bridge requires MEDIA_BRIDGE_PROVIDER_FACTORY"
            )
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
    server = MediaBridgeGrpcServer(
        max_pending_audio_frames=settings.media_bridge_max_pending_audio_frames,
        max_pending_messages=settings.media_bridge_max_pending_messages,
    )
    provider_factory = _load_provider_factory(settings)
    registry: MediaVoiceCoreRegistry | None = None
    if provider_factory is not None:
        registry = MediaVoiceCoreRegistry(
            bridge=server,
            provider_factory=provider_factory,
        )
        registry.install()
        logger.info("media bridge Voice Core provider registry installed")
    else:
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
    try:
        await stopped.wait()
    finally:
        _ = registry
        await server.stop()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
