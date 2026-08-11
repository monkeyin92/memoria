"""Memoria Consent Authority domain and storage (Agent A).

Public API:
* evidence: immutable, hash-verified consent / relationship / binding evidence
  and versioned snapshots (``services.consent.evidence``);
* events: outbox and audit rows written in the same transaction
  (``services.consent.events``);
* authority: ``ConsentAuthority`` (sync) and ``AsyncConsentAuthority`` (async)
  implementing the authoritative consent rules (``services.consent.authority``);
* stores: ``ConsentStorePort`` / ``AsyncConsentStorePort`` seams plus InMemory,
  SQLite (local fixture only) and PostgreSQL (production, FORCE RLS) adapters.
"""

from services.consent.authority import (
    AsyncConsentAuthority,
    AsyncEvidenceResolverPort,
    ConsentAuthority,
    ConsentDeniedError,
    ConsentOperationResult,
    EvidenceResolverPort,
    SubjectProof,
)
from services.consent.events import AuditEntry, ConsentOutboxEvent
from services.consent.evidence import (
    ALLOWED_PURPOSES,
    DEVICE_TRANSFER_PURPOSE,
    BindingEvidence,
    ConsentEvidence,
    ConsentOffer,
    ConsentOfferStatus,
    ConsentParams,
    ConsentSnapshot,
    ConsentStatus,
    RelationshipEvidence,
    canonical_json,
    compute_canonical_hash,
    is_guardian_of,
    is_parent_of,
)
from services.consent.in_memory_store import InMemoryConsentStore
from services.consent.postgres_store import PostgresConsentStore
from services.consent.sqlite_store import SqliteConsentStore
from services.consent.store import (
    AsyncConsentStorePort,
    ConsentConflictError,
    ConsentConsistencyError,
    ConsentNotFoundError,
    ConsentStorePort,
    IdempotencyRecord,
)
from services.consent.transaction_authorizer import (
    ConsentFenceMismatchError,
    ExpectedConsentFence,
    TransactionBoundConsentAuthorizer,
)

__all__ = [
    "ALLOWED_PURPOSES",
    "AsyncConsentAuthority",
    "AsyncEvidenceResolverPort",
    "AsyncConsentStorePort",
    "AuditEntry",
    "BindingEvidence",
    "ConsentAuthority",
    "ConsentConflictError",
    "ConsentConsistencyError",
    "ConsentDeniedError",
    "ConsentEvidence",
    "ConsentFenceMismatchError",
    "ConsentNotFoundError",
    "ConsentOffer",
    "ConsentOfferStatus",
    "ConsentOperationResult",
    "ConsentOutboxEvent",
    "ConsentParams",
    "ConsentSnapshot",
    "ConsentStorePort",
    "ConsentStatus",
    "DEVICE_TRANSFER_PURPOSE",
    "EvidenceResolverPort",
    "ExpectedConsentFence",
    "IdempotencyRecord",
    "InMemoryConsentStore",
    "PostgresConsentStore",
    "RelationshipEvidence",
    "SqliteConsentStore",
    "SubjectProof",
    "TransactionBoundConsentAuthorizer",
    "canonical_json",
    "compute_canonical_hash",
    "is_guardian_of",
    "is_parent_of",
]
