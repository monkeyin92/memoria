from __future__ import annotations

from services.speaker.evaluation import EvaluationTrial, evaluate_speaker_thresholds


def test_evaluation_report_computes_far_frr_eer_and_activation_gate() -> None:
    trials = tuple(
        [EvaluationTrial("owner", 0.90) for _ in range(100)]
        + [EvaluationTrial("guest", 0.10) for _ in range(100)]
    )

    report = evaluate_speaker_thresholds(
        trials,
        model_version="campplus-eval-v1",
        template_version=3,
        owner_threshold=0.8,
        guest_threshold=0.4,
        max_far=0.01,
        max_frr=0.05,
        max_eer=0.05,
        min_unknown_rejection=0.9,
    )

    assert report.sample_count == 200
    assert report.far == 0
    assert report.frr == 0
    assert report.eer == 0
    assert report.unknown_rejection == 1
    assert report.passed is True
    evaluation = report.for_activation()
    assert evaluation.sample_count == 200
    assert evaluation.passed is True


def test_failed_report_cannot_be_promoted_to_activation() -> None:
    trials = tuple(
        [EvaluationTrial("owner", 0.20) for _ in range(100)]
        + [EvaluationTrial("guest", 0.90) for _ in range(100)]
    )
    report = evaluate_speaker_thresholds(
        trials,
        model_version="campplus-eval-v1",
        template_version=1,
        owner_threshold=0.8,
        guest_threshold=0.4,
    )

    assert report.passed is False
    try:
        report.for_activation()
    except ValueError as exc:
        assert "must pass" in str(exc)
    else:  # pragma: no cover - behavioral assertion
        raise AssertionError("failed evaluation unexpectedly activated")
