"""Memory Scope and family shared-memory domain (PR-12/PR-14).

Public surface: ``MemoryScopeService`` (facade), ``MemoryScopeResolver``
(pure decisions), domain contracts and the three store adapters.
"""

from services.memory_scope.domain import (
    ActorNotAuthorizedError,
    ConfirmationVote,
    ConsentSnapshotInput,
    CoSubjectContext,
    CrossFamilyAccessError,
    MemoryAuditEvent,
    MemoryNotFoundError,
    MemoryOutboxEvent,
    MemoryRecord,
    MemoryRecordStatusEvent,
    MemoryScope,
    MemoryWriteDraft,
    MemoryWriteRejected,
    NotAuthorizedError,
    PolicyDecisionInput,
    PolicyEffect,
    PolicyObligation,
    ProposalStateError,
    ResolutionContext,
    ScopeResolution,
    SharedMemoryProposal,
    SharedVisibility,
    SubjectContext,
    WriteFence,
    WriteFenceError,
    WriteFenceExpiredError,
    WriteFenceMismatchError,
    WriteFenceMissingError,
)
from services.memory_scope.in_memory_store import InMemoryMemoryStore
from services.memory_scope.postgres_store import PostgresMemoryStore
from services.memory_scope.repository import MemoryStore
from services.memory_scope.resolver import MemoryScopeResolver
from services.memory_scope.service import MemoryScopeService
from services.memory_scope.sqlite_store import SqliteMemoryStore

__all__ = [
    "ActorNotAuthorizedError",
    "ConfirmationVote",
    "ConsentSnapshotInput",
    "CoSubjectContext",
    "CrossFamilyAccessError",
    "InMemoryMemoryStore",
    "MemoryAuditEvent",
    "MemoryNotFoundError",
    "MemoryOutboxEvent",
    "MemoryRecord",
    "MemoryRecordStatusEvent",
    "MemoryScope",
    "MemoryScopeResolver",
    "MemoryScopeService",
    "MemoryStore",
    "MemoryWriteDraft",
    "MemoryWriteRejected",
    "NotAuthorizedError",
    "PolicyEffect",
    "PolicyObligation",
    "PolicyDecisionInput",
    "PostgresMemoryStore",
    "ProposalStateError",
    "ResolutionContext",
    "ScopeResolution",
    "SharedMemoryProposal",
    "SharedVisibility",
    "SqliteMemoryStore",
    "SubjectContext",
    "WriteFence",
    "WriteFenceError",
    "WriteFenceExpiredError",
    "WriteFenceMismatchError",
    "WriteFenceMissingError",
]
