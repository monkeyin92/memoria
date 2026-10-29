"""Context-snapshot activation runtime mixin: the turn boundary and the prediction of it.

``DuplexRuntimeContextActivationMixin`` is composed into ``DuplexRuntime``; shared state stays owned by the
runtime dataclass and is only declared here for type checking.  The activation rule and its prediction live
side by side so that a response-plan request sent before the commit (``ReplyPipeline``'s head start) reads the
snapshot the commit will freeze.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from services.agent.src.orchestration.context_snapshot_manager import ContextSnapshot

if TYPE_CHECKING:
    from services.agent.src.orchestration.context_snapshot_manager import PendingSnapshot
    from services.agent.src.orchestration.orchestrator import Orchestrator


class DuplexRuntimeContextActivationMixin:
    """Activate a prepared context snapshot at the turn boundary."""

    if TYPE_CHECKING:
        # Shared state owned by the DuplexRuntime dataclass.
        session_id: str
        orchestrator: Orchestrator
        _pending_context_snapshot: PendingSnapshot | None
        _pending_context_snapshot_epoch: int | None
        _context_snapshot_prepare_epoch: int

        # Core runtime members consumed by this mixin.
        @property
        def current_speaker_class(self) -> Literal["owner", "guest", "uncertain"]: ...

    def _activate_pending_context_snapshot(self) -> None:
        pending = self._pending_context_snapshot
        if (
            pending is None
            or self._pending_context_snapshot_epoch != self._context_snapshot_prepare_epoch
        ):
            return
        if pending.candidate.speaker_class != self.current_speaker_class:
            self._pending_context_snapshot = None
            self._pending_context_snapshot_epoch = None
            return
        activated = self.orchestrator.context_snapshots.activate(
            pending,
            expected_current_version=pending.base_version,
        )
        self._pending_context_snapshot = None
        self._pending_context_snapshot_epoch = None
        if isinstance(activated, ContextSnapshot):
            self.orchestrator.delegation.activate_context_version(
                self.session_id,
                activated.version,
            )

    def prospective_context_snapshot(self) -> ContextSnapshot:
        """The snapshot the next commit will freeze, as far as it can be told before the commit.

        ``on_turn_committed`` activates the pending snapshot under the same conditions as here; a
        snapshot that becomes pending in between is the commit's, not this one.
        """

        current = self.orchestrator.context_snapshots.current(self.session_id)
        pending = self._pending_context_snapshot
        if (
            pending is not None
            and self._pending_context_snapshot_epoch == self._context_snapshot_prepare_epoch
            and pending.candidate.speaker_class == self.current_speaker_class
            and pending.base_version == current.version
        ):
            return pending.candidate
        return current
