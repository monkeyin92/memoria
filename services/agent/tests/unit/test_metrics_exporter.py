from __future__ import annotations

import urllib.request

from services.agent.src.observability.metrics import MetricsRegistry


def test_prometheus_http_exporter_serves_labeled_metrics() -> None:
    metrics = MetricsRegistry()
    metrics.set_sessions_active(2)
    metrics.inc_stale_result_dropped("tts")
    server, thread = metrics.start_http_server(0, addr="127.0.0.1")
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback test server
            f"http://127.0.0.1:{server.server_port}/metrics",
            timeout=2,
        ) as response:
            body = response.read().decode()
        assert "# TYPE voice_sessions_active gauge" in body
        assert "voice_sessions_active 2.0" in body
        assert 'stale_result_dropped_total{source="tts"} 1.0' in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_required_metric_helpers_and_snapshot() -> None:
    metrics = MetricsRegistry()
    metrics.inc_state_transition("listening", "thinking", "turn_committed")
    metrics.inc_interruptions_confirmed()
    metrics.inc_false_interruptions()
    metrics.inc_interruption_candidate()
    metrics.inc_tts_connections_discarded("cancel")
    metrics.set_tts_pool_available(3)
    metrics.set_tool_tasks_active("search", 2)
    metrics.set_voice_latency("eou_to_first_audio", "0.95", 1.1)
    metrics.add_audio_input_seconds(0.5)
    metrics.inc_asr_request("ok", "fun-asr-realtime")
    metrics.inc_asr_reconnect()
    metrics.set_provider_ws_active("asr", 1)
    metrics.set_provider_ws_active("tts", 2)
    metrics.inc_provider_ws_reconnect("tts")
    metrics.inc_llm_request("deepseek-v4-flash", "ok", thinking=False)
    metrics.inc_tts_request("ok", "cosyvoice-v3-flash", "longanyang")
    metrics.set_media_metric("asr_send_lag_ms", 12.5)
    metrics.set_media_metric("asr_partial_age_ms", 34.5)
    metrics.set_media_metric("tts_frame_age_ms", 6.5)

    snapshot = metrics.snapshot()
    assert snapshot["tts_pool_available"] == 3
    assert snapshot['tool_tasks_active{tool="search"}'] == 2
    assert metrics.get("asr_reconnects_total") == 1
    assert metrics.get("provider_ws_active", {"provider": "asr"}) == 1
    assert metrics.get("provider_ws_active", {"provider": "tts"}) == 2
    assert metrics.get("provider_ws_reconnect_total", {"provider": "asr"}) == 1
    assert metrics.get("provider_ws_reconnect_total", {"provider": "tts"}) == 1
    assert metrics.get("asr_send_lag_ms") == 12.5
    assert metrics.get("asr_partial_age_ms") == 34.5
    assert metrics.get("tts_frame_age_ms") == 6.5
    assert metrics.get("missing") == 0
    body = metrics.render_prometheus().decode()
    assert 'llm_requests_total{model="deepseek-v4-flash",status="ok",thinking="false"}' in body
    assert 'voice_latency_seconds{quantile="0.95",stage="eou_to_first_audio"} 1.1' in body
    assert 'provider_ws_active{provider="asr"} 1' in body
    assert 'provider_ws_reconnect_total{provider="tts"} 1' in body
    assert "# TYPE asr_send_lag_ms gauge" in body
    assert "asr_partial_age_ms 34.5" in body
    assert "tts_frame_age_ms 6.5" in body


def test_provider_connection_gauge_aggregates_independent_connections() -> None:
    metrics = MetricsRegistry()

    metrics.add_provider_ws_active("asr", 1)
    metrics.add_provider_ws_active("asr", 1)
    metrics.add_provider_ws_active("asr", -1)

    assert metrics.get("provider_ws_active", {"provider": "asr"}) == 1
