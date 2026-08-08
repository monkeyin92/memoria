"""Durable periodic trigger for offline evolution sleep cycles."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from services.evolution.curation import EvolutionControlPlane

logger = logging.getLogger(__name__)


class EvolutionSleepWorker:
    """Trigger curation without putting candidate generation on the request path."""

    def __init__(
        self,
        control_plane: EvolutionControlPlane,
        *,
        interval_s: float,
        candidate_kind: Literal["knowledge", "prompt", "skill", "harness", "parameter"] = "prompt",
    ) -> None:
        if interval_s < 0:
            raise ValueError("evolution sleep interval must be non-negative")
        self._control_plane = control_plane
        self._interval_s = interval_s
        self._candidate_kind = candidate_kind
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._interval_s == 0 or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="evolution-sleep-cycle")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                report = await asyncio.to_thread(
                    self._control_plane.sleep_cycle,
                    candidate_kind=self._candidate_kind,
                )
            except Exception:
                logger.exception("evolution sleep cycle failed")
                continue
            logger.info(
                "evolution sleep cycle ran=%s new_signals=%s clusters=%s candidates=%s",
                report.ran,
                report.new_signal_count,
                len(report.clusters),
                len(report.candidates),
            )


__all__ = ["EvolutionSleepWorker"]
