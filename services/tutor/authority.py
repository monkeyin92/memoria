"""Server-owned tutor assessment evidence, fence gate, receipts, and rubric.

Learning outcomes are never decided by a client.  A
:class:`TutorAssessmentEvidence` is minted only by a server-side assessment
authority (the voice-agent seam), stored in a server-owned immutable vault,
and handed to consumers as an opaque one-time ``assessment_id``.  The record
is HMAC-signed by the authority; :class:`TutorEvidenceGate` re-verifies the
signature and every fence field (subject, actor, device, binding/version,
session/epoch, runtime profile, subject revision, policy receipt) against the
authoritative session fence before any outcome is derived.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from threading import Lock, RLock
from typing import Literal, Protocol

from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyActionResourceFence,
)

from services.policy.action_fence import (
    DEFAULT_ACTION_FENCE_TTL,
    build_action_resource_fence,
)
from services.tutor.catalog import criteria_for
from services.tutor.domain import LessonTask, PracticeOutcome, TutorFocus

TutorSupportLevel = Literal["none", "hint", "minimal_hint", "full_guidance"]
TutorCompletion = Literal["completed", "partial", "gave_up"]

ASSESSMENT_ISSUER: str = "memoria.tutor.assessment.v1"
ASSESSMENT_SIGNATURE_SCHEMA: str = "tutor-assessment-v1"
EVALUATOR_VERSION: str = "tutor-rubric-v2"
MASTERY_MIN_CORRECTNESS: float = 0.8
MASTERY_MIN_ATTEMPTS: int = 2
TUTOR_ACTION_PURPOSE: str = "runtime_sensitive_action"
TUTOR_ACTION_OBLIGATION: str = "WRITE_SUBJECT_SCOPED_PROGRESS"

_TUTOR_SUPPORT_LEVELS: frozenset[str] = frozenset(
    {"none", "hint", "minimal_hint", "full_guidance"}
)
_TUTOR_COMPLETIONS: frozenset[str] = frozenset({"completed", "partial", "gave_up"})
_MAX_PRACTICE_SECONDS: int = 3 * 60 * 60
_VAULT_MAX_ENTRIES: int = 4096


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded(value: str, name: str, *, maximum: int = 128) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must be a bounded non-blank string")
    return normalized


def _non_negative(value: int, name: str) -> int:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


class TutorEvidenceRejected(PermissionError):
    """The evidence or its fence failed a fail-closed check."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TutorTurnCounters:
    """The voice-agent turn fence consumed by one assessment."""

    generation_id: int
    turn_id: int
    tool_epoch: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation_id", _non_negative(self.generation_id, "generation_id"))
        object.__setattr__(self, "turn_id", _non_negative(self.turn_id, "turn_id"))
        object.__setattr__(self, "tool_epoch", _non_negative(self.tool_epoch, "tool_epoch"))


@dataclass(frozen=True, slots=True)
class TutorFenceSnapshot:
    """The server-side session authority view of one voice session."""

    voice_session_id: str
    actor_id: str
    active_subject_id: str
    device_id: str
    binding_id: str
    binding_version: int
    subject_revision: int
    session_epoch: int
    runtime_profile_id: str
    policy_receipt_ids: tuple[str, ...]
    expires_at: datetime
    resolved_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "voice_session_id", _bounded(self.voice_session_id, "voice_session_id", maximum=256))
        object.__setattr__(self, "actor_id", _bounded(self.actor_id, "actor_id"))
        object.__setattr__(self, "active_subject_id", _bounded(self.active_subject_id, "active_subject_id"))
        object.__setattr__(self, "device_id", _bounded(self.device_id, "device_id"))
        object.__setattr__(self, "binding_id", _bounded(self.binding_id, "binding_id"))
        for name, minimum in (
            ("binding_version", 1),
            ("session_epoch", 1),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        object.__setattr__(self, "subject_revision", _non_negative(self.subject_revision, "subject_revision"))
        object.__setattr__(self, "runtime_profile_id", _bounded(self.runtime_profile_id, "runtime_profile_id", maximum=256))
        receipts = tuple(dict.fromkeys(str(value).strip() for value in self.policy_receipt_ids))
        if not receipts or any(not value for value in receipts):
            raise ValueError("policy_receipt_ids must contain at least one receipt id")
        object.__setattr__(self, "policy_receipt_ids", receipts)
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
        object.__setattr__(self, "resolved_at", _utc(self.resolved_at, "resolved_at"))


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def policy_action_resource_fence(
    *,
    capability: str,
    purpose: str,
    action_resource_id: str,
    action_revision: int,
    generation_id: int,
    turn_id: int,
    tool_epoch: int,
    issued_at: datetime,
    valid_until: datetime,
) -> PolicyActionResourceFence:
    """Build the canonical generated fence via the Policy module.

    ``action_evidence_hash`` and ``canonical_hash`` are derived by the
    Policy builder from the structural fields; callers can never
    self-attest a hash.
    """

    return build_action_resource_fence(
        capability=capability,  # type: ignore[arg-type]
        purpose=purpose,  # type: ignore[arg-type]
        action_resource_id=action_resource_id,
        action_revision=action_revision,
        generation_id=generation_id,
        turn_id=turn_id,
        tool_epoch=tool_epoch,
        issued_at=issued_at,
        valid_until=valid_until,
    )


def sign_assessment_payload(
    payload: dict[str, object],
    *,
    signing_key: bytes,
) -> str:
    """HMAC signature over the canonical unsigned envelope."""

    encoded = _canonical(payload).encode("utf-8")
    return hmac.new(signing_key, encoded, hashlib.sha256).hexdigest()


def verify_assessment_signature(
    payload: dict[str, object],
    *,
    signing_key: bytes,
) -> bool:
    signature = payload.get("signature")
    if not isinstance(signature, str) or not signature:
        return False
    if payload.get("signature_schema") != ASSESSMENT_SIGNATURE_SCHEMA:
        return False
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    expected = sign_assessment_payload(unsigned, signing_key=signing_key)
    return hmac.compare_digest(signature, expected)


@dataclass(frozen=True, slots=True)
class TutorAssessmentEvidence:
    """Immutable, signed, server-minted learning evidence bound to one fence.

    The constructor is deliberately public for the issuing authority only:
    the signature is produced by :func:`sign_assessment_payload` with a
    server-held key, so a caller who fabricates an object cannot produce a
    valid signature.  Consumers must fetch evidence from the server-owned
    vault by ``assessment_id`` and let :class:`TutorEvidenceGate` verify it.
    """

    event_id: str
    subject_id: str
    actor_id: str
    voice_session_id: str
    device_id: str
    binding_id: str
    binding_version: int
    subject_revision: int
    task_id: str
    focus: TutorFocus
    occurred_at: datetime
    duration_seconds: int
    session_epoch: int
    runtime_profile_id: str
    generation_id: int
    turn_id: int
    tool_epoch: int
    policy_receipt_id: str
    criterion_ids: tuple[str, ...]
    correctness_score: float | None
    evaluator_version: str
    support_level: TutorSupportLevel
    completion: TutorCompletion
    attempt_count: int
    issuer: str = ASSESSMENT_ISSUER
    signature: str = ""
    schema_version: int = 1
    content_sha256: str = field(init=False)
    idempotency_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name, maximum in (
            ("event_id", 256),
            ("subject_id", 128),
            ("actor_id", 128),
            ("voice_session_id", 256),
            ("device_id", 128),
            ("binding_id", 128),
            ("task_id", 128),
            ("runtime_profile_id", 256),
            ("policy_receipt_id", 256),
            ("evaluator_version", 64),
            ("issuer", 64),
        ):
            object.__setattr__(self, name, _bounded(str(getattr(self, name)), name, maximum=maximum))
        if self.focus not in {"tutor_english", "tutor_homework"}:
            raise ValueError("assessment focus must be a tutor focus")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))
        if isinstance(self.duration_seconds, bool) or not 0 <= self.duration_seconds <= _MAX_PRACTICE_SECONDS:
            raise ValueError("duration_seconds must be between zero and three hours")
        for name, minimum in (
            ("binding_version", 1),
            ("session_epoch", 1),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        for name in ("subject_revision", "generation_id", "turn_id", "tool_epoch", "attempt_count"):
            object.__setattr__(self, name, _non_negative(getattr(self, name), name))
        if self.support_level not in _TUTOR_SUPPORT_LEVELS:
            raise ValueError("support_level must be a server-defined signal")
        if self.completion not in _TUTOR_COMPLETIONS:
            raise ValueError("completion must be a server-defined signal")
        criteria = tuple(dict.fromkeys(_bounded(value, "criterion_id", maximum=128) for value in self.criterion_ids))
        if not criteria:
            raise ValueError("criterion_ids must contain at least one server criterion")
        object.__setattr__(self, "criterion_ids", criteria)
        score = self.correctness_score
        if score is not None and (
            isinstance(score, bool) or not isinstance(score, float) or not 0.0 <= score <= 1.0
        ):
            raise ValueError("correctness_score must be null or between zero and one")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")
        envelope = self._unsigned_payload()
        # The receipt binding must not participate in the content identity:
        # the action fence's evidence hash is computable before the receipt is
        # verified, and a re-issued receipt for the same evidence must not
        # change the evidence digest.
        identity = {key: value for key, value in envelope.items() if key != "occurred_at"}
        content_identity = {
            key: value
            for key, value in envelope.items()
            if key not in {"occurred_at", "policy_receipt_id"}
        }
        object.__setattr__(
            self,
            "content_sha256",
            hashlib.sha256(_canonical(content_identity).encode()).hexdigest(),
        )
        object.__setattr__(self, "idempotency_sha256", hashlib.sha256(_canonical(identity).encode()).hexdigest())

    def _unsigned_payload(self) -> dict[str, object]:
        return {
            "signature_schema": ASSESSMENT_SIGNATURE_SCHEMA,
            "issuer": self.issuer,
            "event_id": self.event_id,
            "subject_id": self.subject_id,
            "actor_id": self.actor_id,
            "voice_session_id": self.voice_session_id,
            "device_id": self.device_id,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "subject_revision": self.subject_revision,
            "task_id": self.task_id,
            "focus": self.focus,
            "occurred_at": self.occurred_at.isoformat(),
            "duration_seconds": self.duration_seconds,
            "session_epoch": self.session_epoch,
            "runtime_profile_id": self.runtime_profile_id,
            "generation_id": self.generation_id,
            "turn_id": self.turn_id,
            "tool_epoch": self.tool_epoch,
            "policy_receipt_id": self.policy_receipt_id,
            "criterion_ids": list(self.criterion_ids),
            "correctness_score": self.correctness_score,
            "evaluator_version": self.evaluator_version,
            "support_level": self.support_level,
            "completion": self.completion,
            "attempt_count": self.attempt_count,
            "schema_version": self.schema_version,
        }

    def signed(self, signing_key: bytes) -> TutorAssessmentEvidence:
        """Return the same record with the authority's HMAC signature."""

        unsigned = self._unsigned_payload()
        return replace(
            self,
            signature=sign_assessment_payload(unsigned, signing_key=signing_key),
        )


class TutorAssessmentVault:
    """Server-owned, append-only store of issued assessments.

    An ``assessment_id`` is an opaque one-time token: it resolves to the
    exact immutable record that was issued and can be consumed exactly once
    per event.  Persistence beyond process lifetime is a dependency gate for
    the production assessment store.
    """

    def __init__(self, *, max_entries: int = _VAULT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("assessment vault bound must be positive")
        self._max_entries = max_entries
        self._lock = Lock()
        self._records: OrderedDict[str, TutorAssessmentEvidence] = OrderedDict()
        self._consumed: dict[str, str] = {}

    def put(self, evidence: TutorAssessmentEvidence) -> str:
        assessment_id = str(uuid.uuid4())
        with self._lock:
            self._records[assessment_id] = evidence
            self._records.move_to_end(assessment_id)
            while len(self._records) > self._max_entries:
                self._records.popitem(last=False)
        return assessment_id

    def get(self, assessment_id: str) -> TutorAssessmentEvidence | None:
        with self._lock:
            return self._records.get(assessment_id)

    def consume(self, assessment_id: str, *, event_id: str) -> bool:
        """One-time CAS: only the first consumer of a token wins."""

        with self._lock:
            if self._records.get(assessment_id) is None:
                return False
            current = self._consumed.get(assessment_id)
            if current is not None:
                return current == event_id
            self._consumed[assessment_id] = event_id
            return True


class TutorSessionFencePort(Protocol):
    """Resolve the authoritative subject fence for one voice session."""

    async def resolve_fence(
        self,
        *,
        actor_id: str,
        voice_session_id: str,
        now: datetime,
    ) -> TutorFenceSnapshot | None: ...


class TutorAssessmentAuthorityPort(Protocol):
    """Server-side producer of signed assessments (voice-agent seam).

    ``issue_assessment`` atomically checks and consumes the turn fence, so two
    concurrent requests cannot both obtain evidence for the same turn.  The
    returned ``assessment_id`` is an opaque one-time token; evidence is only
    retrievable from the server-owned vault.
    """

    async def issue_assessment(
        self,
        *,
        fence: TutorFenceSnapshot,
        task: LessonTask,
        duration_seconds: int,
        now: datetime,
        event_id: str,
        receipt_template: TutorReceiptExpectation,
        receipt_verifier: TutorPolicyReceiptVerifierPort,
    ) -> TutorAssessmentGrant:
        """Mint one signed assessment; idempotent per (voice session, event).

        Receipt verification happens inside this atomic operation with the
        exact counters and evidence reference, so a profile-level receipt can
        never authorize the write and no TOCTOU window exists between the
        receipt check and the turn-fence consume.
        """

    async def fetch_assessment(
        self,
        *,
        assessment_id: str,
    ) -> TutorAssessmentEvidence | None: ...


@dataclass(frozen=True, slots=True)
class TutorAssessmentGrant:
    """One issued assessment plus the verified receipt and canonical fence."""

    assessment_id: str
    receipt_id: str
    receipt_expires_at: datetime
    action_fence: PolicyActionResourceFence

    def __post_init__(self) -> None:
        object.__setattr__(self, "assessment_id", _bounded(self.assessment_id, "assessment_id", maximum=256))
        object.__setattr__(self, "receipt_id", _bounded(self.receipt_id, "receipt_id", maximum=256))
        object.__setattr__(self, "receipt_expires_at", _utc(self.receipt_expires_at, "receipt_expires_at"))
        if not isinstance(self.action_fence, PolicyActionResourceFence):
            raise ValueError("action_fence must be the canonical generated fence")


def _turn_binding_key(
    voice_session_id: str,
    signals: TutorAssessmentSignals,
) -> str:
    """The immutable full-fence key one turn's state is stored under."""

    return _canonical(
        {
            "voice_session_id": voice_session_id,
            "subject_id": signals.subject_id,
            "runtime_profile_id": signals.runtime_profile_id,
            "session_epoch": signals.session_epoch,
            "task_id": signals.task_id,
            "evaluator_version": signals.evaluator_version,
            "generation_id": signals.generation_id,
            "turn_id": signals.turn_id,
            "tool_epoch": signals.tool_epoch,
        }
    )


@dataclass(frozen=True, slots=True)
class _TurnState:
    """One agent-clock turn: counters plus fully bound signals."""

    counters: TutorTurnCounters
    signals: TutorAssessmentSignals


def _expected_binding_key(
    voice_session_id: str,
    fence: TutorFenceSnapshot,
    task: LessonTask,
    counters: TutorTurnCounters,
) -> str:
    """The full-fence key the current session/task/counters require."""

    return _canonical(
        {
            "voice_session_id": voice_session_id,
            "subject_id": fence.active_subject_id,
            "runtime_profile_id": fence.runtime_profile_id,
            "session_epoch": fence.session_epoch,
            "task_id": task.task_id,
            "evaluator_version": EVALUATOR_VERSION,
            "generation_id": counters.generation_id,
            "turn_id": counters.turn_id,
            "tool_epoch": counters.tool_epoch,
        }
    )


@dataclass(frozen=True, slots=True)
class TutorAssessmentSignals:
    """Server-side assessment signals bound to the exact fence they assessed.

    The signals carry subject, runtime profile, session epoch, task,
    evaluator version and the turn counters.  ``issue_assessment`` compares
    every binding field against the current fence, so an old turn's signals
    can never be reused by a newer turn, a different task, or another subject.
    """

    subject_id: str
    runtime_profile_id: str
    session_epoch: int
    task_id: str
    evaluator_version: str
    generation_id: int
    turn_id: int
    tool_epoch: int
    support_level: TutorSupportLevel
    completion: TutorCompletion
    criterion_ids: tuple[str, ...]
    attempt_count: int = 1
    correctness_score: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject_id", _bounded(self.subject_id, "subject_id"))
        object.__setattr__(
            self,
            "runtime_profile_id",
            _bounded(self.runtime_profile_id, "runtime_profile_id", maximum=256),
        )
        if isinstance(self.session_epoch, bool) or self.session_epoch < 1:
            raise ValueError("session_epoch must be a positive integer")
        object.__setattr__(self, "task_id", _bounded(self.task_id, "task_id"))
        object.__setattr__(
            self,
            "evaluator_version",
            _bounded(self.evaluator_version, "evaluator_version", maximum=64),
        )
        for name in ("generation_id", "turn_id", "tool_epoch"):
            object.__setattr__(self, name, _non_negative(getattr(self, name), name))
        if self.support_level not in _TUTOR_SUPPORT_LEVELS:
            raise ValueError("support_level must be a server-defined signal")
        if self.completion not in _TUTOR_COMPLETIONS:
            raise ValueError("completion must be a server-defined signal")
        criteria = tuple(
            dict.fromkeys(_bounded(value, "criterion_id", maximum=128) for value in self.criterion_ids)
        )
        if not criteria:
            raise ValueError("criterion_ids must contain at least one server criterion")
        object.__setattr__(self, "criterion_ids", criteria)
        object.__setattr__(self, "attempt_count", _non_negative(self.attempt_count, "attempt_count"))
        score = self.correctness_score
        if score is not None and (
            isinstance(score, bool) or not isinstance(score, float) or not 0.0 <= score <= 1.0
        ):
            raise ValueError("correctness_score must be null or between zero and one")


class FencedAssessmentAuthority:
    """Reference server-side authority: atomic fence consume + signed mint.

    ``issue_assessment`` is the single atomic operation that checks and
    consumes the turn fence under a per-turn lock with a two-phase version
    CAS, so two concurrent requests cannot both obtain evidence for the same
    turn (P0-5) while slow receipt verification never blocks other turns.
    Idempotent retries with the same ``event_id`` return the already-issued
    token.  Turn state is keyed by its full immutable fence binding, so an
    old turn's signals can never be reused after the agent clock moves.

    This is an in-memory reference/test adapter, not the production
    authority.  Production practice scoring requires the agent-side signed
    assessment input; until that seam is wired the route fails closed (503).
    """

    def __init__(
        self,
        *,
        signing_key: bytes,
        vault: TutorAssessmentVault | None = None,
    ) -> None:
        if len(signing_key) < 16:
            raise ValueError("assessment signing key must contain at least 16 bytes")
        self._signing_key = signing_key
        self._vault = vault or TutorAssessmentVault()
        self._state_lock = RLock()
        # Turn state is keyed by the full immutable fence binding; the voice
        # session only points at the agent clock's current key.
        self._turn_states: dict[str, _TurnState] = {}
        self._current: dict[str, str] = {}
        self._versions: dict[str, int] = {}
        self._consumed: dict[str, bool] = {}
        self._issued: dict[tuple[str, str], TutorAssessmentGrant] = {}
        self._turn_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._max_turn_locks = 256

    def set_turn_state(
        self,
        voice_session_id: str,
        *,
        counters: TutorTurnCounters,
        signals: TutorAssessmentSignals,
    ) -> None:
        """Atomically install one turn's counters and fully bound signals.

        The state is stored under its full fence-binding key (voice, subject,
        profile, epoch, task, evaluator, generation, turn, tool); the voice
        session pointer and a per-key version advance atomically under the
        state lock, so issue's two-phase CAS can detect a concurrent switch.
        """

        key = _turn_binding_key(voice_session_id, signals)
        state = _TurnState(counters=counters, signals=signals)
        with self._state_lock:
            self._turn_states[key] = state
            self._current[voice_session_id] = key
            self._versions[key] = self._versions.get(key, 0) + 1

    async def issue_assessment(
        self,
        *,
        fence: TutorFenceSnapshot,
        task: LessonTask,
        duration_seconds: int,
        now: datetime,
        event_id: str,
        receipt_template: TutorReceiptExpectation,
        receipt_verifier: TutorPolicyReceiptVerifierPort,
    ) -> TutorAssessmentGrant:
        with self._state_lock:
            cached = self._issued.get((fence.voice_session_id, event_id))
            if cached is not None:
                return cached
            current_key = self._current.get(fence.voice_session_id)
            state = (
                self._turn_states.get(current_key)
                if current_key is not None
                else None
            )
            if state is None:
                raise TutorEvidenceRejected("stale_event_counters")
            counters = state.counters
            signals = state.signals
            stored_key = current_key
            expected_key = _expected_binding_key(
                fence.voice_session_id,
                fence,
                task,
                counters,
            )
            if stored_key != expected_key:
                raise TutorEvidenceRejected("assessment_signal_fence_mismatch")
            if self._consumed.get(stored_key):
                raise TutorEvidenceRejected("stale_event_counters")
            version = self._versions.get(stored_key, 0)
            turn_lock = self._turn_locks.get(stored_key)
            if turn_lock is None:
                turn_lock = asyncio.Lock()
                self._turn_locks[stored_key] = turn_lock
                self._turn_locks.move_to_end(stored_key)
                while len(self._turn_locks) > self._max_turn_locks:
                    self._turn_locks.popitem(last=False)
        async with turn_lock:
            self._assert_turn_current(
                fence.voice_session_id,
                stored_key,
                version,
            )
            action_fence = policy_action_resource_fence(
                capability=receipt_template.capability,
                purpose=receipt_template.purpose,
                action_resource_id=receipt_template.action_resource_id,
                action_revision=receipt_template.action_revision,
                generation_id=counters.generation_id,
                turn_id=counters.turn_id,
                tool_epoch=counters.tool_epoch,
                issued_at=now,
                valid_until=now + DEFAULT_ACTION_FENCE_TTL,
            )
            receipt_id, receipt_expires_at = await self._verify_action_receipt(
                fence=fence,
                expectation=receipt_template,
                action_fence=action_fence,
                verifier=receipt_verifier,
                now=now,
            )
            draft = TutorAssessmentEvidence(
                event_id=event_id,
                subject_id=fence.active_subject_id,
                actor_id=fence.actor_id,
                voice_session_id=fence.voice_session_id,
                device_id=fence.device_id,
                binding_id=fence.binding_id,
                binding_version=fence.binding_version,
                subject_revision=fence.subject_revision,
                task_id=task.task_id,
                focus=task.focus,
                occurred_at=now,
                duration_seconds=duration_seconds,
                session_epoch=fence.session_epoch,
                runtime_profile_id=fence.runtime_profile_id,
                generation_id=counters.generation_id,
                turn_id=counters.turn_id,
                tool_epoch=counters.tool_epoch,
                policy_receipt_id="pending",
                criterion_ids=signals.criterion_ids,
                correctness_score=signals.correctness_score,
                evaluator_version=EVALUATOR_VERSION,
                support_level=signals.support_level,
                completion=signals.completion,
                attempt_count=signals.attempt_count,
            )
            evidence = replace(draft, policy_receipt_id=receipt_id).signed(
                self._signing_key
            )
            with self._state_lock:
                self._assert_turn_current(
                    fence.voice_session_id,
                    stored_key,
                    version,
                )
                if self._consumed.get(stored_key):
                    raise TutorEvidenceRejected("stale_event_counters")
                self._consumed[stored_key] = True
                assessment_id = self._vault.put(evidence)
                grant = TutorAssessmentGrant(
                    assessment_id=assessment_id,
                    receipt_id=receipt_id,
                    receipt_expires_at=receipt_expires_at,
                    action_fence=action_fence,
                )
                self._issued[(fence.voice_session_id, event_id)] = grant
            return grant

    def _assert_turn_current(
        self,
        voice_session_id: str,
        stored_key: str,
        version: int,
    ) -> None:
        with self._state_lock:
            if (
                self._current.get(voice_session_id) != stored_key
                or self._versions.get(stored_key, 0) != version
            ):
                raise TutorEvidenceRejected("turn_state_changed")

    @staticmethod
    async def _verify_action_receipt(
        *,
        fence: TutorFenceSnapshot,
        expectation: TutorReceiptExpectation,
        action_fence: PolicyActionResourceFence,
        verifier: TutorPolicyReceiptVerifierPort,
        now: datetime,
    ) -> tuple[str, datetime]:
        """Pick the exact action receipt among the profile's receipt ids."""

        for receipt_id in fence.policy_receipt_ids:
            result = await verifier.verify_receipt(
                receipt_id=receipt_id,
                expectation=expectation,
                action_fence=action_fence,
                now=now,
            )
            if result.ok and result.expires_at is not None:
                return receipt_id, result.expires_at
        raise TutorEvidenceRejected("tutor_receipt_required")

    async def fetch_assessment(
        self,
        *,
        assessment_id: str,
    ) -> TutorAssessmentEvidence | None:
        return self._vault.get(assessment_id)


@dataclass(frozen=True, slots=True)
class TutorReceiptExpectation:
    """The exact action receipt a tutor progress write must carry.

    A profile-issuance receipt (``runtime_profile_issue``) is never enough:
    every practice write requires a receipt for ``runtime_sensitive_action``
    whose canonical ``PolicyActionResourceFence`` binds the exact action
    resource id/revision, the evidence hash and the full
    generation/turn/tool counters, and whose literal fields match the
    subject fence.  The Policy port must emit these receipts; until the seam
    is wired the route fails closed (503).
    """

    capability: str
    purpose: str
    actor_id: str
    subject_id: str
    device_id: str
    binding_id: str
    binding_version: int
    session_id: str
    session_epoch: int
    runtime_profile_id: str
    subject_revision: int
    action_resource_id: str
    action_revision: int
    required_obligations: tuple[str, ...] = (TUTOR_ACTION_OBLIGATION,)

    def __post_init__(self) -> None:
        for name in (
            "capability",
            "purpose",
            "actor_id",
            "subject_id",
            "device_id",
            "binding_id",
            "session_id",
            "runtime_profile_id",
            "action_resource_id",
        ):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must be a non-blank string")
            object.__setattr__(self, name, value)
        for name, minimum in (("binding_version", 1), ("session_epoch", 1)):
            value = getattr(self, name)
            if isinstance(value, bool) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        for name, minimum in (
            ("subject_revision", 0),
            ("action_revision", 1),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if not self.required_obligations:
            raise ValueError("required_obligations must not be empty")


@dataclass(frozen=True, slots=True)
class TutorReceiptVerification:
    """Outcome of one receipt verification."""

    ok: bool
    receipt_id: str = ""
    expires_at: datetime | None = None
    reason: str = ""


class TutorPolicyReceiptVerifierPort(Protocol):
    """Policy transaction-bound verification of the action receipt.

    The production implementation is the Policy seam: it must validate the
    receipt against the current policy state (receipt validity window,
    exact-fence evidence, active consent/relationship snapshots, binding
    canonical hash and revocation) and compare the canonical
    ``action_resource_fence`` with the generated fence and this expectation.
    Until that seam is wired, the route fails closed (503); no weaker
    in-repo verifier may replace it.
    """

    async def verify_receipt(
        self,
        *,
        receipt_id: str,
        expectation: TutorReceiptExpectation,
        action_fence: PolicyActionResourceFence,
        now: datetime,
        connection: object | None = None,
    ) -> TutorReceiptVerification: ...


class TutorEvidenceGate:
    """Fail closed unless evidence matches the authoritative current fence."""

    def __init__(self, *, signing_key: bytes) -> None:
        if len(signing_key) < 16:
            raise ValueError("assessment signing key must contain at least 16 bytes")
        self._signing_key = signing_key

    def verify(
        self,
        *,
        evidence: TutorAssessmentEvidence,
        fence: TutorFenceSnapshot,
        known_event_ids: Iterable[str],
        now: datetime,
        receipt_expires_at: datetime | None = None,
    ) -> None:
        if evidence.event_id in set(known_event_ids):
            raise TutorEvidenceRejected("replayed_evidence")
        if evidence.issuer != ASSESSMENT_ISSUER:
            raise TutorEvidenceRejected("unknown_issuer")
        if not verify_assessment_signature(
            {**_signed_payload(evidence), "signature": evidence.signature},
            signing_key=self._signing_key,
        ):
            raise TutorEvidenceRejected("forged_envelope")
        if now >= fence.expires_at:
            raise TutorEvidenceRejected("expired_session_fence")
        if fence.resolved_at > evidence.occurred_at:
            raise TutorEvidenceRejected("evidence_before_fence")
        upper_bound = fence.expires_at
        if receipt_expires_at is not None and receipt_expires_at < upper_bound:
            upper_bound = receipt_expires_at
        if evidence.occurred_at >= upper_bound:
            raise TutorEvidenceRejected("evidence_after_fence")
        for name, expected in (
            ("subject_id", fence.active_subject_id),
            ("actor_id", fence.actor_id),
            ("voice_session_id", fence.voice_session_id),
            ("device_id", fence.device_id),
            ("binding_id", fence.binding_id),
            ("binding_version", fence.binding_version),
            ("subject_revision", fence.subject_revision),
            ("session_epoch", fence.session_epoch),
            ("runtime_profile_id", fence.runtime_profile_id),
        ):
            if getattr(evidence, name) != expected:
                code = (
                    "cross_subject_evidence"
                    if name == "subject_id"
                    else "actor_mismatch"
                    if name == "actor_id"
                    else "stale_session_fence"
                    if name in {"session_epoch", "runtime_profile_id", "subject_revision"}
                    else "evidence_fence_mismatch"
                )
                raise TutorEvidenceRejected(code)
        if evidence.policy_receipt_id not in fence.policy_receipt_ids:
            raise TutorEvidenceRejected("unauthorized_policy_receipt")


def _signed_payload(evidence: TutorAssessmentEvidence) -> dict[str, object]:
    return {**evidence._unsigned_payload(), "signature": evidence.signature}


class TutorScoringRubric:
    """Deterministic server-owned rubric over server-issued signals.

    ``mastered`` requires an assessed correctness score at or above the
    threshold, at least two independent attempts, no high-level help, a
    masterable criterion from the server catalog, and the current evaluator
    version.  A completed-but-wrong turn is ``struggled``; a completed turn
    without correctness evidence is ``attempted``; open (non-masterable)
    criteria can never produce ``mastered``.
    """

    def score(
        self,
        evidence: TutorAssessmentEvidence,
        task: LessonTask,
    ) -> tuple[PracticeOutcome, str]:
        if evidence.task_id != task.task_id:
            raise ValueError("evidence task does not match the server task")
        criteria = criteria_for(task)
        by_id = {criterion.criterion_id: criterion for criterion in criteria}
        if any(criterion_id not in by_id for criterion_id in evidence.criterion_ids):
            raise ValueError("evidence criteria are not in the server catalog for the task")
        skills = {
            by_id[criterion_id].skill_key for criterion_id in evidence.criterion_ids
        }
        if len(skills) != 1:
            raise ValueError("evidence criteria must map to exactly one skill")
        skill_key = next(iter(skills))
        masterable = all(
            by_id[criterion_id].masterable for criterion_id in evidence.criterion_ids
        )
        if evidence.completion == "gave_up":
            return "gave_up", skill_key
        if evidence.support_level == "full_guidance":
            return "supported", skill_key
        if evidence.completion == "completed":
            if evidence.support_level != "none":
                return "supported", skill_key
            score = evidence.correctness_score
            if score is not None and score < MASTERY_MIN_CORRECTNESS:
                return "struggled", skill_key
            if (
                masterable
                and score is not None
                and score >= MASTERY_MIN_CORRECTNESS
                and evidence.attempt_count >= MASTERY_MIN_ATTEMPTS
                and evidence.evaluator_version == EVALUATOR_VERSION
            ):
                return "mastered", skill_key
            return "attempted", skill_key
        if evidence.support_level != "none":
            return "struggled", skill_key
        return "attempted", skill_key


__all__ = [
    "ASSESSMENT_ISSUER",
    "ASSESSMENT_SIGNATURE_SCHEMA",
    "EVALUATOR_VERSION",
    "FencedAssessmentAuthority",
    "MASTERY_MIN_ATTEMPTS",
    "MASTERY_MIN_CORRECTNESS",
    "TUTOR_ACTION_OBLIGATION",
    "TUTOR_ACTION_PURPOSE",
    "TutorAssessmentAuthorityPort",
    "TutorAssessmentEvidence",
    "TutorAssessmentGrant",
    "TutorAssessmentSignals",
    "TutorAssessmentVault",
    "TutorCompletion",
    "TutorEvidenceGate",
    "TutorEvidenceRejected",
    "TutorFenceSnapshot",
    "TutorPolicyReceiptVerifierPort",
    "TutorReceiptExpectation",
    "TutorReceiptVerification",
    "TutorScoringRubric",
    "TutorSessionFencePort",
    "TutorSupportLevel",
    "TutorTurnCounters",
    "policy_action_resource_fence",
    "sign_assessment_payload",
    "verify_assessment_signature",
]
