from services.agent.src.voice_core.slo import MediaSLO, evaluate_slo


def test_slo_fails_closed_on_stale_generation() -> None:
    report = evaluate_slo({"first_audio_p95_ms": 400, "stale_generation_total": 1})
    assert not report.passed
    assert report.rollback_required
    assert "stale_generation_total>0" in report.failures


def test_slo_passes_with_baseline_observations() -> None:
    report = evaluate_slo(
        {
            "first_audio_p95_ms": 700,
            "interrupt_stop_p95_ms": 100,
            "session_failure_rate": 0.01,
            "stale_generation_total": 0,
            "stale_asr_final_total": 0,
        },
        slo=MediaSLO(),
    )
    assert report.passed and not report.rollback_required


def test_empty_slo_observation_fails_closed() -> None:
    report = evaluate_slo({})
    assert report.rollback_required
    assert "missing_first_audio_p95_ms" in report.failures


def test_fractional_stale_counter_fails_closed_instead_of_truncating() -> None:
    report = evaluate_slo({"stale_generation_total": 0.9, "stale_asr_final_total": 0})
    assert report.rollback_required
    assert "invalid_stale_generation_total" in report.failures
