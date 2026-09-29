"""Fence-keyed response-plan cache owned by the reply pipeline."""

from __future__ import annotations

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.response_planner_client import ResponsePlan


class ResponsePlanCache:
    """Response plans by exact ``GenerationFence``, each with its authorizing profile.

    A legal manager change withdraws authorization without advancing any fence
    the key carries, so fence equality alone would keep serving the old
    subject's grounded items on a same-fence retry (P0-04). Every entry
    therefore also records the signed ``runtime_profile_id`` that authorized it.
    """

    MAX_ENTRIES = 32

    def __init__(self) -> None:
        self._plans: dict[GenerationFence, ResponsePlan] = {}
        self._profiles: dict[GenerationFence, str | None] = {}

    def __contains__(self, fence: object) -> bool:
        return fence in self._plans

    def __len__(self) -> int:
        return len(self._plans)

    def get(self, fence: GenerationFence) -> ResponsePlan | None:
        return self._plans.get(fence)

    def authorizing_profile_id(self, fence: GenerationFence) -> str | None:
        return self._profiles.get(fence)

    def store(self, plan: ResponsePlan, *, authorizing_profile_id: str | None = None) -> None:
        """Cache ``plan`` under its own fence and prune what it supersedes."""

        self._plans[plan.fence] = plan
        self._profiles[plan.fence] = authorizing_profile_id
        # Actively drop superseded identity epochs: an older-epoch entry can
        # never match a current-fence reuse (epochs advance strictly), and
        # holding it retains the old subject's grounded items in memory.
        for fence in tuple(self._plans):
            if fence.session_epoch < plan.fence.session_epoch:
                self.discard(fence)
        while len(self._plans) > self.MAX_ENTRIES:
            self.discard(next(iter(self._plans)))

    def discard(self, fence: GenerationFence) -> None:
        self._plans.pop(fence, None)
        self._profiles.pop(fence, None)

    def authorized(self, plan: ResponsePlan, *, current_profile_id: str | None) -> bool:
        """Whether the cached plan's authorizing profile is still current.

        Drop (fail closed) exactly when a stamped profile id is present and a
        different profile -- or no profile after a degrade -- is current now.
        Entries cached while authority-less stay reusable on fence match: the
        local-safe fallback carries no grounded items. Unstamped entries are
        likewise fenced by exact fence match only.
        """

        stamped = self._profiles.get(plan.fence)
        if stamped is None:
            return True
        return stamped == current_profile_id


__all__ = ["ResponsePlanCache"]
