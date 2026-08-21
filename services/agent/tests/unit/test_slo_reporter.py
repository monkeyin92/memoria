from __future__ import annotations

import httpx
import pytest
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.voice_core.slo_reporter import (
    MediaSLOReporter,
    MediaSLOReporterConfig,
    parse_prometheus_slo_snapshot,
)


@pytest.mark.asyncio
async def test_slo_reporter_posts_only_aggregate_allowlisted_metrics() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers["X-Media-SLO-Token"]
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(202)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reporter = MediaSLOReporter(
        MediaSLOReporterConfig(
            endpoint="http://control-api:8000/v1/internal/media-runtime/slo",
            token="slo-token-material-that-is-at-least-32-chars",
            source="voice-core-test",
        ),
        client=client,
    )
    try:
        await reporter.report(
            {
                "first_audio_p95_ms": 800,
                "interrupt_stop_p95_ms": 180,
                "session_failure_rate": 0.01,
                "stale_generation_total": 0,
                "stale_asr_final_total": 0,
            }
        )
    finally:
        await reporter.close()
        await client.aclose()
    assert seen["token"] == "slo-token-material-that-is-at-least-32-chars"
    assert '"first_audio_p95_ms":800.0' in str(seen["body"])
    assert "raw_audio" not in str(seen["body"])
    assert "text" not in str(seen["body"])


def test_media_slo_snapshot_omits_uninstrumented_latency() -> None:
    metrics = MetricsRegistry()
    snapshot = metrics.media_slo_snapshot()

    assert snapshot["stale_generation_total"] == 0
    assert snapshot["stale_asr_final_total"] == 0
    assert "first_audio_p95_ms" not in snapshot
    assert "session_failure_rate" not in snapshot

    metrics.set_voice_latency("first_audio", "p95", 0.8)
    metrics.observe_voice_latency("tts_first_frame", 0.12)
    metrics.set_voice_latency("interrupt_stop", "p95", 0.18)
    metrics._inc("media_sessions_total", amount=100)
    metrics._inc("media_sessions_failed_total", amount=1)
    snapshot = metrics.media_slo_snapshot()
    assert snapshot["first_audio_p95_ms"] == 800
    assert snapshot["tts_first_frame_p95_ms"] == 120
    assert snapshot["interrupt_stop_p95_ms"] == 180
    assert snapshot["session_failure_rate"] == pytest.approx(0.01)


def test_prometheus_snapshot_parser_is_allowlisted_and_derives_failure_rate() -> None:
    snapshot = parse_prometheus_slo_snapshot(
        '\n'.join(
            (
                'stale_result_dropped_total{source="media_generation"} 0',
                'stale_result_dropped_total{source="asr_final"} 1',
                'voice_latency_seconds{quantile="p95",stage="first_audio"} 0.8',
                'voice_latency_seconds{quantile="p95",stage="tts_first_frame"} 0.12',
                'media_sessions_total 10',
                'media_sessions_failed_total 1',
                'unrelated_secret_metric{token="nope"} 42',
            )
        )
    )
    assert snapshot == {
        "stale_generation_total": 0.0,
        "stale_asr_final_total": 1.0,
        "first_audio_p95_ms": 800.0,
        "tts_first_frame_p95_ms": 120.0,
        "session_failure_rate": 0.1,
    }
