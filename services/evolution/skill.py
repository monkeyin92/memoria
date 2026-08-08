"""Skill activation and benefit telemetry adapter."""

from __future__ import annotations

import asyncio

from services.archive.skill_domain import SkillRun, SkillRunRequest
from services.evolution.account_fence import AccountWriteBlockedError
from services.evolution.curation import EvolutionControlPlane
from services.evolution.store import EvolutionNotFoundError


class SkillActivationObserver:
    """Record activation/adherence without making skill execution depend on evolution."""

    def __init__(self, control_plane: EvolutionControlPlane) -> None:
        self._control_plane = control_plane

    async def on_run(
        self,
        request: SkillRunRequest,
        run: SkillRun,
        *,
        succeeded: bool,
    ) -> None:
        candidate_id = request.evolution_candidate_id
        if candidate_id is None:
            return
        try:
            candidate = await asyncio.to_thread(
                self._control_plane.store.get_candidate,
                candidate_id,
            )
            if candidate.status not in {"canary", "stable"}:
                return
            await asyncio.to_thread(
                self._control_plane.record_activation,
                candidate_id=candidate_id,
                task_id=run.run_id,
                activated=True,
                adhered=succeeded,
                outcome_passed=succeeded and run.status == "succeeded",
                evidence_event_id=request.confirmation_event_id,
            )
        except EvolutionNotFoundError:
            # A stale candidate reference cannot make a valid skill run fail.
            return
        except AccountWriteBlockedError:
            # Account erasure wins over best-effort activation telemetry.
            return


__all__ = ["SkillActivationObserver"]
