"""Projection adapter for independently evaluated archive trajectories.

The online archive owns raw delivery evidence.  This module intentionally does
not infer task success from ``actual_heard``; it only appends a learning signal
after an offline evaluator has supplied bounded task/process/quality evidence.
"""

from __future__ import annotations

from collections.abc import Mapping

from services.evolution.curation import EvolutionControlPlane
from services.evolution.trajectory import (
    CanonicalTrajectory,
    CanonicalTrajectoryError,
    TrajectoryAssessment,
)


class EvolutionRuntimeCapture:
    """Project canonical pairs only when independent evaluation is present."""

    def __init__(self, control_plane: EvolutionControlPlane) -> None:
        self._control_plane = control_plane

    def capture_event(self, event: Mapping[str, object]) -> str | None:
        """Compatibility no-op for the online delivery telemetry hook."""

        del event
        return None

    def capture_pair(
        self,
        user_event: Mapping[str, object],
        assistant_event: Mapping[str, object],
    ) -> str | None:
        """Compatibility no-op for an online actual-heard pair."""

        del user_event, assistant_event
        return None

    def capture_evaluation_pair(
        self,
        user_event: Mapping[str, object],
        assistant_event: Mapping[str, object],
        *,
        evaluation_id: str,
        evaluation: Mapping[str, object],
    ) -> str | None:
        """Persist a bounded, independently evaluated canonical trajectory."""

        try:
            trajectory = CanonicalTrajectory.from_mappings(user_event, assistant_event)
            assessment = TrajectoryAssessment.from_mapping(evaluation)
            observation = assessment.observation(trajectory, evaluation_id=evaluation_id)
        except CanonicalTrajectoryError:
            return None
        report = self._control_plane.observe(observation)
        return report.signal_id

    def pending_count(self) -> int:
        """Retained for old callers; delivery telemetry no longer buffers turns."""

        return 0

    def discard_session(self, session_id: str) -> int:
        """Retained for old callers; there is no online pending state to discard."""

        del session_id
        return 0


def pending_key(session_id: str, turn_id: int, generation_id: int, tool_epoch: int) -> str:
    """Legacy key helper kept for callers that identify a generation fence."""

    return f"{session_id}:{turn_id}:{generation_id}:{tool_epoch}"


__all__ = ["EvolutionRuntimeCapture", "pending_key"]
