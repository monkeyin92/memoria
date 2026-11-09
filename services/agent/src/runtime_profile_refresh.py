"""Runtime profile refresh mixin: signed profile, voice profile, identity epochs.

``DuplexRuntimeProfileRefreshMixin`` is composed into ``DuplexRuntime``; shared
state stays owned by the runtime dataclass and is only declared here for type
checking.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.identity_state import (
    capture_identity_tasks,
    clear_identity_private_state,
    drain_epoch_rotation,
    invalidate_identity_epochs,
)
from services.agent.src.mode_policy_client import mode_policy_after_identity_rotation
from services.agent.src.runtime_profile import VerifiedRuntimeProfile

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from services.agent.src.duplex_runtime import PendingRealtimeRequest
    from services.agent.src.generation_records import GenerationRecords
    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.orchestration.context_snapshot_manager import PendingSnapshot
    from services.agent.src.orchestration.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class DuplexRuntimeProfileRefreshMixin:
    """Refresh the signed profiles and rotate identity epochs atomically."""

    if TYPE_CHECKING:
        # Shared state owned by the DuplexRuntime dataclass.
        orchestrator: Orchestrator
        session_id: str
        RUNTIME_PROFILE_REFRESH_TIMEOUT_S: float
        _generation_records: GenerationRecords
        _mode_policy: ModePolicy
        _pending_epoch_drain: GenerationFence | None
        _rotation_captured_tasks: list[Any] | None
        _voice_profile_refresher: Callable[[], Coroutine[Any, Any, Any]] | None
        _runtime_profile_refresher: (
            Callable[[], Coroutine[Any, Any, VerifiedRuntimeProfile | None]] | None
        )
        _voice_profile_refresh_task: asyncio.Task[Any] | None
        _pending_context_snapshot: PendingSnapshot | None
        _pending_context_snapshot_epoch: int | None
        _context_prefetch_text: str
        _pending_realtime_request: PendingRealtimeRequest | None
        _persona_evidence_eligible: bool
        _next_user_prompt_kind: str
        _interaction_prefetch_epoch: int | None
        _interaction_context_prefetch_key: tuple[int, str] | None
        _interaction_warm_epoch: int | None
        _interaction_delegated_fences: set[GenerationFence]

        # Core runtime members consumed by this mixin.
        @property
        def fence(self) -> GenerationFence: ...

        def _spawn(
            self,
            coroutine: Coroutine[Any, Any, Any],
            *,
            name: str,
            durable: bool = False,
        ) -> asyncio.Task[Any]: ...

    def apply_runtime_profile(
        self, payload: object, *, now: datetime | None = None
    ) -> VerifiedRuntimeProfile | None:
        """Consumer seam: between-turn refresh/subject switch (P1-8)."""

        previous_epoch = self.fence.session_epoch
        old_fence = self.fence
        applied = self.orchestrator.runtime_profiles.apply(payload, self.fence, now=now)
        if applied is not None and applied.profile.session_epoch > previous_epoch:
            self._rotate_identity_epoch(
                applied, old_fence=old_fence, install_policy=previous_epoch > 0
            )
        return applied

    def _rotate_identity_epoch(
        self,
        profile: VerifiedRuntimeProfile | None,
        *,
        old_fence: GenerationFence,
        install_policy: bool,
    ) -> None:
        """One atomic identity-epoch rotation; ONE authority after it."""
        self._pending_epoch_drain = old_fence
        # Capture identity-owned tasks BEFORE clearing references, bump the
        # identity-private epochs so late results are logically void, then
        # clear state; the drain barrier bounded-cancels the captured tasks.
        self._rotation_captured_tasks = capture_identity_tasks(self)
        invalidate_identity_epochs(self)
        self._reset_identity_private_caches()
        clear_identity_private_state(self)
        # Mirror the orchestrator epoch bump: the rolling conversation context
        # and the snapshot manager still hold the old subject's turns, summary
        # and memory/persona capsules, and neither is keyed by the binding.
        # Without this reset a degraded or rotated epoch would keep generating
        # from the withdrawn subject's context -- a manager change withdraws
        # authorization without advancing any fence the snapshots carry (P0-04).
        self.orchestrator.context.reset_identity()
        fresh = self.orchestrator.context_snapshots.reset_identity(self.session_id)
        self.orchestrator.delegation.activate_context_version(
            self.session_id, fresh.version
        )
        if install_policy:
            # Keep companion style across epoch rotation so designed TTS can bind.
            self._mode_policy = mode_policy_after_identity_rotation(
                self._mode_policy,
                profile,
            )
            self._generation_records.retain_mode_policies_for_epoch(self.fence.session_epoch)
            self._generation_records.bind_mode_policy(self.fence, self._mode_policy)

    async def _drain_epoch_rotation(self) -> None:
        """Barrier stays visible until every physical owner drained."""
        old_fence = self._pending_epoch_drain
        if old_fence is None:
            return
        try:
            await drain_epoch_rotation(self, old_fence)
        except Exception:
            # Barrier stays visible (fail-closed) and the turn is aborted.
            self._pending_epoch_drain = old_fence
            self.orchestrator.metrics.inc_runtime_profile_refresh_failure("drain_error")
            logger.exception("epoch rotation drain failed")
            raise
        self._pending_epoch_drain = None

    async def settle_bootstrap_identity_epoch(self) -> None:
        """Drain a session-start profile rotation before pre-turn TTS.

        Binding a signed RuntimeProfile can advance ``session_epoch`` and
        leave ``_pending_epoch_drain`` set until the first user turn.
        Device wake acknowledgement is audible before any turn, so the
        barrier must close when the media session is published.
        """

        if self._pending_epoch_drain is None or self.fence.turn_id != 0:
            return
        await self._drain_epoch_rotation()

    def degrade_runtime_profile(self) -> GenerationFence:
        """Authority-loss transition to a true unknown-safe degraded epoch."""

        previous_epoch = self.fence.session_epoch
        old_fence = self.fence
        new_fence = self.orchestrator.runtime_profiles.degrade(self.fence)
        if new_fence.session_epoch == previous_epoch:
            return new_fence  # already degraded (idempotent)
        self._rotate_identity_epoch(None, old_fence=old_fence, install_policy=True)
        return new_fence

    def _reset_identity_private_caches(self) -> None:
        """Drop every pending/prefetched private field of the old subject."""

        # Called once per epoch bump, atomically inside the apply seam.
        self._pending_context_snapshot = None
        self._pending_context_snapshot_epoch = None
        self._context_prefetch_text = ""
        self._pending_realtime_request = None
        self._persona_evidence_eligible = False
        self._next_user_prompt_kind = "spontaneous"
        # The epoch-bound prefetch/warm keys self-invalidate via the bumped
        # speaker epoch, but holding them retains the old subject's query and
        # delegated fences in memory -- drop them with everything else.
        self._interaction_prefetch_epoch = None
        self._interaction_context_prefetch_key = None
        self._interaction_warm_epoch = None
        self._interaction_delegated_fences.clear()

    def set_voice_profile_refresher(
        self,
        refresher: Callable[[], Coroutine[Any, Any, Any]],
    ) -> None:
        self._voice_profile_refresher = refresher

    def set_runtime_profile_refresher(
        self,
        refresher: Callable[[], Coroutine[Any, Any, VerifiedRuntimeProfile | None]] | None,
    ) -> None:
        """Install the authoritative refresh source (P1-8/audit 2)."""

        self._runtime_profile_refresher = refresher

    async def refresh_runtime_profile(self) -> VerifiedRuntimeProfile | None:
        if self._runtime_profile_refresher is None:
            self.degrade_runtime_profile()
            return None
        try:
            verified = await asyncio.wait_for(
                self._runtime_profile_refresher(), timeout=self.RUNTIME_PROFILE_REFRESH_TIMEOUT_S
            )
        except TimeoutError:
            self.orchestrator.metrics.inc_runtime_profile_refresh_failure("timeout")
            logger.warning(
                "runtime profile refresh timed out after %ss",
                self.RUNTIME_PROFILE_REFRESH_TIMEOUT_S,
            )
            self.degrade_runtime_profile()
            return None
        except Exception:
            self.orchestrator.metrics.inc_runtime_profile_refresh_failure("error")
            logger.exception("runtime profile refresh failed")
            self.degrade_runtime_profile()
            return None
        applied = self.apply_runtime_profile(verified)
        if applied is None:
            self.orchestrator.metrics.inc_runtime_profile_refresh_failure("invalid")
            self.degrade_runtime_profile()
        return applied

    def refresh_voice_profile(self) -> asyncio.Task[Any] | None:
        if self._voice_profile_refresher is None:
            return None
        current = self._voice_profile_refresh_task
        if current is not None and not current.done():
            return current
        # Keep the currently applied voice while the resolver is in flight.
        # The result is applied atomically at the next TTS boundary; changing
        # to baseline here makes a fixed welcome sentence sound like another
        # companion whenever VAD starts a turn.
        self._voice_profile_refresh_task = self._spawn(
            self._voice_profile_refresher(),
            name="voice-profile-refresh",
        )
        return self._voice_profile_refresh_task

    async def wait_for_voice_profile_refresh(self) -> None:
        """Wait for the latest profile refresh before a response is rendered."""
        task = self._voice_profile_refresh_task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            return
        except Exception:
            # The resolver is fail-closed; the caller will apply the baseline
            # because the cache is empty, while the conversation remains live.
            logger.warning("voice profile refresh failed", exc_info=True)
