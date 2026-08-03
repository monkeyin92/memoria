"""Run the agent-side aggregate SLO reporter as a small sidecar process."""

from __future__ import annotations

import asyncio
import logging
import signal

import httpx
from services.agent.src.config import load_settings
from services.agent.src.voice_core.slo_reporter import (
    MediaSLOReporter,
    MediaSLOReporterConfig,
    parse_prometheus_slo_snapshot,
)


async def run() -> None:
    settings = load_settings(require_keys=False)
    if not settings.media_slo_report_enabled:
        raise RuntimeError(
            "MEDIA_SLO_REPORT_ENABLED=false; enable the reporter explicitly before starting it"
        )
    logging.basicConfig(level=settings.log_level)
    reporter = MediaSLOReporter(
        MediaSLOReporterConfig(
            endpoint=settings.media_slo_report_url,
            token=settings.media_slo_report_token.get_secret_value(),
            source="voice-core",
            interval_s=settings.media_slo_report_interval_s,
            timeout_s=settings.media_slo_report_timeout_s,
        )
    )
    metrics_client = httpx.AsyncClient(timeout=settings.media_slo_report_timeout_s)

    async def snapshot_provider() -> dict[str, float]:
        response = await metrics_client.get(settings.media_slo_metrics_url)
        response.raise_for_status()
        return parse_prometheus_slo_snapshot(response.text)

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopped.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        # The sidecar reads the explicitly configured media-runtime endpoint.
        # Importing GLOBAL_METRICS here would create a new process-local, empty
        # registry and make every rollout report fail closed for the wrong
        # reason.
        await reporter.run(snapshot_provider, stopped)
    finally:
        await metrics_client.aclose()
        await reporter.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
