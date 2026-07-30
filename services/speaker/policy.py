"""Backend-independent speaker embedding policy."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable

from services.speaker.domain import EmbeddingResult, EnrollmentQualityError


def embedding_quality_reason(result: EmbeddingResult) -> str | None:
    if result.speech_ms < 800:
        return "insufficient_speech"
    if result.snr_db < 8:
        return "low_snr"
    if result.quality_score < 0.5:
        return "low_quality"
    return None


def classification_quality_reason(result: EmbeddingResult) -> str | None:
    quality_reason = embedding_quality_reason(result)
    if quality_reason is not None:
        return quality_reason
    if result.risk_assessment != "verified":
        return "risk_assessment_unavailable"
    if result.replay_risk >= 0.5:
        return "replay_risk"
    if result.synthetic_risk >= 0.5:
        return "synthetic_risk"
    return None


def require_enrollment_quality(result: EmbeddingResult) -> None:
    reason = embedding_quality_reason(result)
    if reason is None and result.risk_assessment == "verified":
        if result.replay_risk >= 0.5:
            reason = "replay_risk"
        elif result.synthetic_risk >= 0.5:
            reason = "synthetic_risk"
    if reason is not None:
        raise EnrollmentQualityError(reason)


def normalize_embedding(vector: tuple[float, ...]) -> tuple[float, ...]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1e-12:
        raise EnrollmentQualityError("speaker embedding has zero norm")
    return tuple(value / norm for value in vector)


def cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 1e-12 or right_norm <= 1e-12:
        return -1.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def serialize_embedding_template(vectors: Iterable[tuple[float, ...]]) -> bytes:
    """Keep each enrollment condition as an independently matchable prototype."""

    prototypes = tuple(normalize_embedding(tuple(vector)) for vector in vectors)
    if not prototypes:
        raise EnrollmentQualityError("speaker template requires at least one prototype")
    if len({len(prototype) for prototype in prototypes}) != 1:
        raise EnrollmentQualityError("speaker embeddings have inconsistent dimensions")
    return json.dumps(
        {
            "format": "multi-prototype-v1",
            "prototypes": prototypes,
        },
        separators=(",", ":"),
    ).encode()


def deserialize_embedding_template(payload: bytes) -> tuple[tuple[float, ...], ...]:
    """Read current multi-prototype templates and legacy single-centroid templates."""

    try:
        decoded = json.loads(payload)
        raw_prototypes = (
            decoded.get("prototypes")
            if isinstance(decoded, dict) and decoded.get("format") == "multi-prototype-v1"
            else [decoded]
            if isinstance(decoded, list)
            and all(isinstance(value, (int, float)) for value in decoded)
            else None
        )
        if not isinstance(raw_prototypes, list) or not raw_prototypes:
            raise ValueError("speaker template payload is invalid")
        prototypes = tuple(
            tuple(float(value) for value in prototype) for prototype in raw_prototypes
        )
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError("speaker template payload is invalid") from exc
    if (
        not prototypes
        or any(not prototype for prototype in prototypes)
        or len({len(prototype) for prototype in prototypes}) != 1
        or any(not math.isfinite(value) for prototype in prototypes for value in prototype)
    ):
        raise ValueError("speaker template payload is invalid")
    return prototypes


def template_similarity(
    vector: tuple[float, ...],
    prototypes: tuple[tuple[float, ...], ...],
) -> float:
    if not prototypes or any(len(vector) != len(prototype) for prototype in prototypes):
        return -1.0
    return max(cosine_similarity(vector, prototype) for prototype in prototypes)
