"""Honest, resumable progress for a consumer voice-clone enrollment.

The mini-program cannot play A/B previews, so its enrollment used to be one
blocking request whose only feedback was a static sentence. Everything a
client needs to show real progress is already durable in the voice tables
(``voice_profiles.status`` / ``evaluation_status`` / ``sample_validation_status``
and the operation row), so progress is derived from that state rather than
tracked in a request-scoped object: leaving the page, reloading, or switching
devices cannot lose it.

Nothing here invents a percentage. ``progress`` is the fraction of the stages
this enrollment must still pass, and it is only reported for the stages the
server itself owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from services.voice_profile.domain import VoiceProfile

EnrollmentStage = Literal["queued", "cloning", "activating", "done", "failed"]

#: What the mini-program promises the user. Past this the copy stops promising
#: a minute and tells them they may leave the page.
PROMISED_TOTAL_MS = 60_000
#: Progress is a fraction of the provider-bound stages, not a fake timer.
_STAGE_ORDER: tuple[EnrollmentStage, ...] = ("queued", "cloning", "activating")
#: The percentage reported at each stage, before the terminal ones.
_STAGE_PROGRESS: dict[EnrollmentStage, float] = {
    "queued": 0.0,
    "cloning": 0.15,
    "activating": 0.9,
}
_STAGE_LABEL: dict[EnrollmentStage, str] = {
    "queued": "正在准备你的声音",
    "cloning": "正在生成你的声音",
    "activating": "正在让机器人用上它",
    "done": "自定义声音已就绪",
    "failed": "这次没生成成功",
}


@dataclass(frozen=True, slots=True)
class EnrollmentProgress:
    """One derivable statement about an in-flight (or finished) enrollment."""

    stage: EnrollmentStage
    stage_index: int
    stage_count: int
    stage_label: str
    progress: float | None
    elapsed_ms: int
    expected_total_ms: int
    over_budget: bool
    retryable: bool
    error_code: str | None = None
    validation: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "stage_index": self.stage_index,
            "stage_count": self.stage_count,
            "stage_label": self.stage_label,
            "progress": self.progress,
            "elapsed_ms": self.elapsed_ms,
            "expected_total_ms": self.expected_total_ms,
            "over_budget": self.over_budget,
            "retryable": self.retryable,
            "error_code": self.error_code,
            "validation": self.validation,
        }


def _stage_for(profile: VoiceProfile) -> EnrollmentStage:
    """Derive the stage from durable status, never from a timer."""
    if profile.status == "enrolling":
        # The sample is stored and the provider call is the only thing left.
        return "queued" if profile.provider_voice_id is None else "cloning"
    if profile.status == "candidate":
        return "activating"
    if profile.status == "active":
        return "done"
    return "failed"


def enrollment_progress(
    profile: VoiceProfile,
    *,
    now: datetime | None = None,
) -> EnrollmentProgress:
    """Derive the progress a client should render for one profile."""
    stage = _stage_for(profile)
    reference = profile.activated_at or profile.created_at
    elapsed_ms = max(
        0, int(((now or datetime.now(UTC)) - reference).total_seconds() * 1000)
    )
    terminal = stage in {"done", "failed"}
    return EnrollmentProgress(
        stage=stage,
        stage_index=_STAGE_ORDER.index(stage) + 1 if stage in _STAGE_ORDER else len(_STAGE_ORDER),
        stage_count=len(_STAGE_ORDER),
        stage_label=_STAGE_LABEL[stage],
        progress=None if terminal else _STAGE_PROGRESS[stage],
        elapsed_ms=elapsed_ms,
        expected_total_ms=PROMISED_TOTAL_MS,
        over_budget=not terminal and elapsed_ms > PROMISED_TOTAL_MS,
        # Only a failure the user can act on by trying again is retryable;
        # ``revoked`` and ``failed`` differ in cause, not in user action.
        retryable=stage == "failed" and profile.status == "failed",
        error_code=None if stage != "failed" else profile.status,
    )
