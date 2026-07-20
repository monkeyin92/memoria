"""Deterministic FAR/FRR/EER report used before a shadow profile can activate."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Literal

from services.speaker.domain import SpeakerEvaluation


@dataclass(frozen=True, slots=True)
class EvaluationTrial:
    expected: Literal["owner", "guest"]
    score: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.score) or not -1 <= self.score <= 1:
            raise ValueError("evaluation score must be finite and between -1 and 1")


@dataclass(frozen=True, slots=True)
class SpeakerEvaluationReport:
    report_ref: str
    model_version: str
    template_version: int
    sample_count: int
    owner_count: int
    guest_count: int
    owner_threshold: float
    guest_threshold: float
    far: float
    frr: float
    eer: float
    unknown_rejection: float
    passed: bool

    def for_activation(self) -> SpeakerEvaluation:
        return SpeakerEvaluation(
            report_ref=self.report_ref,
            sample_count=self.sample_count,
            far=self.far,
            frr=self.frr,
            eer=self.eer,
            unknown_rejection=self.unknown_rejection,
            passed=self.passed,
        )


def evaluate_speaker_thresholds(
    trials: tuple[EvaluationTrial, ...],
    *,
    model_version: str,
    template_version: int,
    owner_threshold: float,
    guest_threshold: float,
    max_far: float = 0.05,
    max_frr: float = 0.15,
    max_eer: float = 0.10,
    min_unknown_rejection: float = 0.80,
) -> SpeakerEvaluationReport:
    owners = [trial.score for trial in trials if trial.expected == "owner"]
    guests = [trial.score for trial in trials if trial.expected == "guest"]
    if len(trials) < 200 or len(owners) < 50 or len(guests) < 50:
        raise ValueError("speaker evaluation requires 200 trials and at least 50 per class")
    if not model_version.strip() or template_version < 1:
        raise ValueError("speaker evaluation requires model and template versions")
    if not 0 <= guest_threshold < owner_threshold <= 1:
        raise ValueError("speaker thresholds must satisfy guest < owner")
    far = sum(score >= owner_threshold for score in guests) / len(guests)
    frr = sum(score < owner_threshold for score in owners) / len(owners)
    unknown_rejection = sum(score <= guest_threshold for score in guests) / len(guests)
    thresholds = sorted({*owners, *guests})
    eer = min(
        (
            (
                abs(
                    sum(score >= threshold for score in guests) / len(guests)
                    - sum(score < threshold for score in owners) / len(owners)
                ),
                (
                    sum(score >= threshold for score in guests) / len(guests)
                    + sum(score < threshold for score in owners) / len(owners)
                )
                / 2,
            )
            for threshold in thresholds
        ),
        key=lambda item: (item[0], item[1]),
    )[1]
    passed = (
        far <= max_far
        and frr <= max_frr
        and eer <= max_eer
        and unknown_rejection >= min_unknown_rejection
    )
    manifest = json.dumps(
        {
            "model_version": model_version,
            "template_version": template_version,
            "owner_threshold": owner_threshold,
            "guest_threshold": guest_threshold,
            "trials": [
                {"expected": trial.expected, "score": trial.score} for trial in trials
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    report_ref = f"speaker-eval:{hashlib.sha256(manifest.encode()).hexdigest()}"
    return SpeakerEvaluationReport(
        report_ref=report_ref,
        model_version=model_version,
        template_version=template_version,
        sample_count=len(trials),
        owner_count=len(owners),
        guest_count=len(guests),
        owner_threshold=owner_threshold,
        guest_threshold=guest_threshold,
        far=far,
        frr=frr,
        eer=eer,
        unknown_rejection=unknown_rejection,
        passed=passed,
    )
