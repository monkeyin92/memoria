"""Immutable, versioned context snapshots prepared off the realtime path."""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.observability.metrics import MetricsRegistry

ContextRole = Literal["user", "assistant"]
SpeakerScope = Literal["owner", "public"]
SnapshotSpeakerClass = Literal["owner", "guest", "uncertain"]


@dataclass(frozen=True, slots=True)
class ContextTurn:
    role: ContextRole
    content: str
    speaker_scope: SpeakerScope = "public"


@dataclass(frozen=True, slots=True)
class MemoryCapsuleEntry:
    item_id: str
    kind: str
    content: str
    source_refs: tuple[str, ...] = ()
    use_as: str = "fact"
    confidence: float | None = None
    sharing_scope: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryCapsule:
    entries: tuple[MemoryCapsuleEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class PersonaCapsule:
    version_id: str | None = None
    version_number: int | None = None
    prompt_fragment: str = ""


@dataclass(frozen=True, slots=True)
class ContextSnapshotDraft:
    recent_committed_turns: tuple[ContextTurn, ...] = ()
    memory_capsule: MemoryCapsule = field(default_factory=MemoryCapsule)
    persona_capsule: PersonaCapsule = field(default_factory=PersonaCapsule)
    relationship_policy: ModePolicy = field(
        default_factory=lambda: ModePolicy.unavailable("context_not_prepared")
    )
    tool_permission: bool = False
    speaker_class: SnapshotSpeakerClass = "uncertain"
    summary: str = ""


def scope_context_snapshot_draft(draft: ContextSnapshotDraft) -> ContextSnapshotDraft:
    """Remove owner-only context before a non-owner snapshot can be used."""

    if draft.speaker_class == "owner":
        return draft
    return replace(
        draft,
        recent_committed_turns=tuple(
            turn for turn in draft.recent_committed_turns if turn.speaker_scope == "public"
        ),
        memory_capsule=MemoryCapsule(),
        persona_capsule=PersonaCapsule(),
        tool_permission=False,
        summary="",
    )


@dataclass(frozen=True, slots=True)
class ContextSnapshot(ContextSnapshotDraft):
    session_id: str = ""
    version: int = 0
    created_at_ms: int = 0

    @property
    def size_chars(self) -> int:
        return (
            sum(len(turn.content) for turn in self.recent_committed_turns)
            + sum(
                len(item.item_id)
                + len(item.kind)
                + len(item.content)
                + sum(map(len, item.source_refs))
                + len(item.use_as)
                + len(item.sharing_scope or "")
                for item in self.memory_capsule.entries
            )
            + len(self.persona_capsule.prompt_fragment)
            + len(self.summary)
            + sum(len(key) + len(str(value)) for key, value in self.relationship_policy.references)
        )


@dataclass(frozen=True, slots=True)
class PendingSnapshot:
    session_id: str
    base_version: int
    candidate: ContextSnapshot
    build_seconds: float


@dataclass(frozen=True, slots=True)
class ContextConflict:
    session_id: str
    expected_version: int
    actual_version: int


SnapshotBuilder = Callable[
    [ContextSnapshot, tuple[ContextTurn, ...]],
    ContextSnapshotDraft | Awaitable[ContextSnapshotDraft],
]


@dataclass(slots=True)
class ContextSnapshotManager:
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    builder: SnapshotBuilder | None = None
    max_recent_turns: int = 16
    max_snapshot_chars: int = 16_000
    _current: dict[str, ContextSnapshot] = field(default_factory=dict)

    def initialize(
        self,
        session_id: str,
        *,
        draft: ContextSnapshotDraft | None = None,
    ) -> ContextSnapshot:
        if not session_id:
            raise ValueError("context snapshot session_id must not be empty")
        current = self._current.get(session_id)
        if current is not None:
            return current
        snapshot = self._snapshot(session_id, 0, draft or ContextSnapshotDraft())
        self._current[session_id] = snapshot
        self.metrics.set_context_snapshot_size(snapshot.size_chars)
        return snapshot

    def current(self, session_id: str) -> ContextSnapshot:
        return self.initialize(session_id)

    def seed_initial(
        self,
        session_id: str,
        draft: ContextSnapshotDraft,
    ) -> ContextSnapshot:
        current = self.current(session_id)
        if current.version != 0:
            raise RuntimeError("only an unbound initial context snapshot can be seeded")
        snapshot = self._snapshot(session_id, 0, draft)
        self._current[session_id] = snapshot
        self.metrics.set_context_snapshot_size(snapshot.size_chars)
        return snapshot

    async def prepare_next(
        self,
        session_id: str,
        *,
        base_version: int,
        committed_events: tuple[ContextTurn, ...],
        draft: ContextSnapshotDraft | None = None,
    ) -> PendingSnapshot | ContextConflict:
        base = self.current(session_id)
        if base.version != base_version:
            return ContextConflict(session_id, base_version, base.version)
        started = time.monotonic()
        try:
            if draft is None:
                builder = self.builder
                if builder is None:
                    draft = ContextSnapshotDraft(
                        recent_committed_turns=committed_events[-self.max_recent_turns :],
                        memory_capsule=base.memory_capsule,
                        persona_capsule=base.persona_capsule,
                        relationship_policy=base.relationship_policy,
                        tool_permission=base.tool_permission,
                        speaker_class=base.speaker_class,
                        summary=base.summary,
                    )
                else:
                    built = builder(base, committed_events)
                    draft = await built if inspect.isawaitable(built) else built
            candidate = self._snapshot(session_id, base_version + 1, draft)
        except Exception:
            self.metrics.inc_context_snapshot_build_failed("builder")
            raise
        elapsed = time.monotonic() - started
        self.metrics.observe_context_snapshot_build(elapsed)
        self.metrics.set_context_snapshot_size(candidate.size_chars)
        return PendingSnapshot(session_id, base_version, candidate, elapsed)

    def activate(
        self,
        pending: PendingSnapshot,
        *,
        expected_current_version: int,
    ) -> ContextSnapshot | ContextConflict:
        current = self.current(pending.session_id)
        if (
            current.version != expected_current_version
            or pending.base_version != expected_current_version
        ):
            return ContextConflict(
                pending.session_id,
                expected_current_version,
                current.version,
            )
        self._current[pending.session_id] = pending.candidate
        self.metrics.set_context_snapshot_size(pending.candidate.size_chars)
        return pending.candidate

    def _snapshot(
        self,
        session_id: str,
        version: int,
        draft: ContextSnapshotDraft,
    ) -> ContextSnapshot:
        if version < 0:
            raise ValueError("context snapshot version must be non-negative")
        draft = scope_context_snapshot_draft(draft)
        recent = draft.recent_committed_turns[-self.max_recent_turns :]
        snapshot = ContextSnapshot(
            recent_committed_turns=recent,
            memory_capsule=draft.memory_capsule,
            persona_capsule=draft.persona_capsule,
            relationship_policy=draft.relationship_policy,
            tool_permission=draft.tool_permission,
            speaker_class=draft.speaker_class,
            summary=draft.summary,
            session_id=session_id,
            version=version,
            created_at_ms=int(time.time() * 1_000),
        )
        if snapshot.size_chars > self.max_snapshot_chars:
            self.metrics.inc_context_snapshot_build_failed("too_large")
            raise ValueError("context snapshot exceeds configured size")
        return snapshot
