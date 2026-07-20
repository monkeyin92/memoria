from __future__ import annotations

import pytest
from services.speaker.domain import EmbeddingResult, EnrollmentQualityError
from services.speaker.policy import (
    classification_quality_reason,
    cosine_similarity,
    normalize_embedding,
    require_enrollment_quality,
)


def _embedding(**overrides: float | int | str | tuple[float, ...]) -> EmbeddingResult:
    values: dict[str, float | int | str | tuple[float, ...]] = {
        "vector": (1.0, 0.0),
        "speech_ms": 2_000,
        "snr_db": 20.0,
        "quality_score": 0.9,
        "replay_risk": 0.1,
        "synthetic_risk": 0.1,
        "risk_assessment": "verified",
    }
    values.update(overrides)
    return EmbeddingResult(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("embedding", "reason"),
    [
        (_embedding(speech_ms=799), "insufficient_speech"),
        (_embedding(snr_db=7.9), "low_snr"),
        (_embedding(quality_score=0.49), "low_quality"),
        (_embedding(replay_risk=0.5), "replay_risk"),
        (_embedding(synthetic_risk=0.5), "synthetic_risk"),
        (_embedding(risk_assessment="unavailable"), "risk_assessment_unavailable"),
        (_embedding(), None),
    ],
)
def test_classification_quality_policy_is_adapter_independent(
    embedding: EmbeddingResult,
    reason: str | None,
) -> None:
    assert classification_quality_reason(embedding) == reason


def test_enrollment_and_vector_policy_is_shared() -> None:
    with pytest.raises(EnrollmentQualityError, match="low_snr"):
        require_enrollment_quality(_embedding(snr_db=1.0))
    with pytest.raises(EnrollmentQualityError, match="zero norm"):
        normalize_embedding((0.0, 0.0))

    assert normalize_embedding((3.0, 4.0)) == pytest.approx((0.6, 0.8))
    assert cosine_similarity((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert cosine_similarity((1.0,), (1.0, 0.0)) == -1.0
