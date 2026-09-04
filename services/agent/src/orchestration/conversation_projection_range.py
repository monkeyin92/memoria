"""Expand a lagging provisional capture range before media commit."""

from __future__ import annotations

from dataclasses import replace

from services.agent.src.orchestration.conversation_projection import (
    ConversationProjection,
    ProjectionPatch,
)
from services.agent.src.orchestration.projection_types import ProjectionEventKind


def align_provisional_range(
    projection: ConversationProjection,
    capture_start_sample: int,
    capture_end_sample: int,
) -> ProjectionPatch | None:
    """Expand the live provisional so a later ASR pin can still commit.

    Media commit uses VAD/ASR turn bounds. Projection can lag that clock
    when a VAD-only provisional never absorbed the accepted final
    (2026-09-04 epoch 1384 weekday: ``projection_range_mismatch``).
    Never shrink the interval; a contained ASR subrange stays valid.
    """

    current = projection._provisional
    if (
        current is None
        or capture_start_sample < 0
        or capture_end_sample <= capture_start_sample
    ):
        return None
    start_sample = min(current.capture_start_sample, capture_start_sample)
    end_sample = max(current.capture_end_sample, capture_end_sample)
    if (
        start_sample == current.capture_start_sample
        and end_sample == current.capture_end_sample
    ):
        return None
    updated = replace(
        current,
        revision=projection._next_revision(),
        capture_start_sample=start_sample,
        capture_end_sample=end_sample,
    )
    projection._provisional = updated
    return ProjectionPatch(ProjectionEventKind.PROVISIONAL_PATCH, updated)
