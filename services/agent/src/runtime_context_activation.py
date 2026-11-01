"""Context-snapshot activation runtime mixin: the turn boundary and the prediction of it.

``DuplexRuntimeContextActivationMixin`` is composed into ``DuplexRuntime``; shared state stays owned by the
runtime dataclass and is only declared here for type checking.  The activation rule and its prediction live
side by side so that a response-plan request sent before the commit (``ReplyPipeline``'s head start) reads the
snapshot the commit will freeze.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from services.agent.src.orchestration.context_snapshot_manager import (
    ContextSnapshot,
    ContextSnapshotDraft,
    ContextTurn,
    scope_context_snapshot_draft,
)

if TYPE_CHECKING:
    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.orchestration.context_manager import ContextManager
    from services.agent.src.orchestration.context_snapshot_manager import (
        MemoryCapsule,
        PendingSnapshot,
        PersonaCapsule,
    )
    from services.agent.src.orchestration.orchestrator import Orchestrator

SpeakerClass = Literal["owner", "guest", "uncertain"]


class DuplexRuntimeContextActivationMixin:
    """Activate a prepared context snapshot at the turn boundary."""

    if TYPE_CHECKING:
        # Shared state owned by the DuplexRuntime dataclass.
        session_id: str
        orchestrator: Orchestrator
        _pending_context_snapshot: PendingSnapshot | None
        _pending_context_snapshot_epoch: int | None
        _context_snapshot_prepare_epoch: int
        _mode_policy: ModePolicy

        # Core runtime members consumed by this mixin.
        @property
        def fence(self) -> GenerationFence: ...

        @property
        def current_speaker_class(self) -> SpeakerClass: ...

    def _context_snapshot_draft(
        self,
        context: ContextManager | None = None,
        *,
        speaker_class: SpeakerClass | None = None,
    ) -> ContextSnapshotDraft:
        """The session context as a snapshot draft; ``context`` and ``speaker_class`` default to the current ones."""

        if context is None:
            context = self.orchestrator.context
        current = self.orchestrator.context_snapshots.current(self.session_id)
        turns = tuple(
            ContextTurn(
                "user" if turn.role == "user" else "assistant",
                turn.content,
                turn.speaker_scope,
            )
            for turn in context.turns
            if turn.role in {"user", "assistant"}
        )
        policy = self._mode_policy
        # Public read-only tools stay available; revoked epochs stop them too.
        # Tool authorization is per ToolSpec, never a coarse policy aggregate.
        outputs_allowed = self.orchestrator.runtime_profiles.output_allowed(
            self.fence, current_fence=self.fence
        )
        return scope_context_snapshot_draft(
            ContextSnapshotDraft(
                recent_committed_turns=turns,
                memory_capsule=current.memory_capsule,
                persona_capsule=current.persona_capsule,
                relationship_policy=policy,
                tool_permission=outputs_allowed,
                speaker_class=speaker_class or self.current_speaker_class,
                summary=context.context_summary(),
            )
        )

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

    def prospective_context_snapshot(
        self, *, speaker_class: SpeakerClass | None = None
    ) -> ContextSnapshot:
        """The snapshot the next commit will freeze, as far as it can be told before the commit.

        ``on_turn_committed`` activates the pending snapshot under the same conditions as here; a
        snapshot that becomes pending in between is the commit's, not this one.  ``speaker_class`` is
        the class the commit will hold, when it is known before the speaker classification has run.
        """

        current = self.orchestrator.context_snapshots.current(self.session_id)
        pending = self._pending_context_snapshot
        if (
            pending is not None
            and self._pending_context_snapshot_epoch == self._context_snapshot_prepare_epoch
            and pending.candidate.speaker_class == (speaker_class or self.current_speaker_class)
            and pending.base_version == current.version
        ):
            return pending.candidate
        return current

    def predicted_generation_snapshot(
        self,
        context: ContextManager,
        *,
        speaker_class: SpeakerClass,
        memory_capsule: MemoryCapsule,
        persona_capsule: PersonaCapsule,
    ) -> ContextSnapshot | None:
        """The snapshot ``freeze_context_capsules_for_generation`` will freeze for the next commit.

        ``context`` is the session context as the commit will leave it.  The freeze builds on the
        snapshot the commit leaves current: the one it rebinds on a session's first commit (a pending
        snapshot prepared on the old version is then never activated), else the prospective one.
        Nothing is prepared or activated; None means the freeze would fail and fall back to the
        current snapshot.
        """

        manager = self.orchestrator.context_snapshots
        seeded_version = manager.seeded_version(self.session_id, first_turn=self.fence.turn_id == 0)
        rebinds = seeded_version != manager.current(self.session_id).version
        base_version = (
            seeded_version
            if rebinds
            else self.prospective_context_snapshot(speaker_class=speaker_class).version
        )
        draft = self._context_snapshot_draft(context, speaker_class=speaker_class)
        return manager.preview(
            self.session_id,
            base_version + 1,
            replace(draft, memory_capsule=memory_capsule, persona_capsule=persona_capsule),
        )
