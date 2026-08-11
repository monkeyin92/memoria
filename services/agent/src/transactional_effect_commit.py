"""Transactional side-effect commit port (deep module).

A side-effect tool never writes directly.  The TaskManager calls exactly one
async port operation, :meth:`TransactionalToolEffectCommitPort.commit_prepared`,
which atomically re-verifies the complete current fence + signed
RuntimeProfile + action-specific PolicyReceipt (current evidence/expiry/
revocation) and records a durable transactional intent, returning a typed
:class:`CommitReceipt`.  Delivery/lease/retry belong to a persistent worker
adapter provided by a later Control/Policy baseline — never to an in-memory
dict inside the TaskManager.  The default port is
``DefaultDenyEffectCommitPort``, so a side-effect tool cannot register until
a real adapter is wired.  The receipt is an opaque typed value issued only by
a port implementation; it is NOT a cryptographic proof by itself.

Per-invocation authority: a mutable, registration-time :class:`ToolSpec` can
only carry static capability constraints.  The dynamic, exact authority for
one action (full fence, resource id, fresh evidence refs, stable logical
action id) lives in the immutable :class:`EffectAuthorization` passed to
``commit_prepared`` — the port never receives mutable spec/evidence state.

Idempotency: the caller derives a stable key from the complete action
identity (tool + canonical args + full fence + capability/purpose/resource +
evidence refs + optional logical action id), so a retry after a timeout
reconciles to the same durable intent instead of committing twice.  The port
exposes :meth:`TransactionalToolEffectCommitPort.reconcile` so the manager
can learn the outcome of an in-flight/late commit before issuing a new one; a
state of ``UNKNOWN`` must never be re-done under a new key (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import re
import types
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from packages.contracts.generated.python.multi_subject_contracts import PolicyReceiptV2

from services.agent.src.action_policy_client import (
    ActionReceiptRejected,
    FrozenActionFence,
    verify_action_receipt,
)
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.runtime_profile import VerifiedRuntimeProfile

if TYPE_CHECKING:
    from services.agent.src.orchestration.task_manager import ToolSpec


_MAX_PAYLOAD_BYTES = 256 * 1024
_MAX_TEXT_LEN = 512
_FINGERPRINT_RE = re.compile(r"^[a-f0-9]{64}$")


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic, bounded, JSON-only canonical bytes for an action."""

    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from None
    data = text.encode("utf-8")
    if len(data) > _MAX_PAYLOAD_BYTES:
        raise ValueError("canonical payload exceeds the bounded size")
    return data


def fence_fingerprint(fence: GenerationFence) -> str:
    """Canonical 64-hex digest of a complete action fence."""

    data = canonical_json_bytes(
        {
            "session_id": fence.session_id,
            "turn_id": fence.turn_id,
            "generation_id": fence.generation_id,
            "tool_epoch": fence.tool_epoch,
            "session_epoch": fence.session_epoch,
        }
    )
    return hashlib.sha256(data).hexdigest()


def freeze_json_value(value: Any) -> Any:
    """Deep-freeze a JSON value so a prepared intent cannot be mutated."""

    if isinstance(value, dict):
        return types.MappingProxyType({key: freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze_json_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze_json_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"prepared payload must contain only JSON values (got {type(value).__name__})")


@dataclass(frozen=True, slots=True)
class EffectAuthorization:
    """Immutable per-invocation authority for one side-effect action.

    Carries the complete action fence plus the exact capability, purpose,
    resource id and fresh evidence refs the commit must verify, and an
    optional stable logical action id used for idempotent retries.  Built by
    the runtime at dispatch time; never shared across subjects/sessions.
    """

    fence: GenerationFence
    capability: str
    purpose: str
    resource_id: str
    evidence_refs: tuple[str, ...]
    action_id: str | None = None
    obligations: tuple[str, ...] = ()
    runtime_profile: VerifiedRuntimeProfile | None = None
    policy_receipt: PolicyReceiptV2 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fence, GenerationFence):
            raise ValueError("authorization fence must be a GenerationFence")
        if self.fence.session_epoch < 1:
            raise ValueError("side effects require a signed runtime profile (session_epoch >= 1)")
        for label, value in (
            ("capability", self.capability),
            ("purpose", self.purpose),
            ("resource_id", self.resource_id),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT_LEN:
                raise ValueError(f"{label} must be a bounded non-blank string")
        if (
            not isinstance(self.evidence_refs, tuple)
            or not self.evidence_refs
            or any(
                not isinstance(ref, str) or not ref.strip() or len(ref) > _MAX_TEXT_LEN
                for ref in self.evidence_refs
            )
        ):
            raise ValueError("evidence_refs must be a non-empty tuple of bounded ids")
        if self.action_id is not None and (
            not isinstance(self.action_id, str)
            or not self.action_id.strip()
            or len(self.action_id) > _MAX_TEXT_LEN
        ):
            raise ValueError("action_id must be a bounded non-blank string")
        if (
            not isinstance(self.obligations, tuple)
            or any(
                not isinstance(obligation, str)
                or not obligation.strip()
                or len(obligation) > _MAX_TEXT_LEN
                for obligation in self.obligations
            )
        ):
            raise ValueError("obligations must be a tuple of bounded strings")
        if self.runtime_profile is not None and not isinstance(
            self.runtime_profile, VerifiedRuntimeProfile
        ):
            raise ValueError("runtime_profile must be a verified profile")
        if self.policy_receipt is not None and not isinstance(
            self.policy_receipt, PolicyReceiptV2
        ):
            raise ValueError("policy_receipt must be a PolicyReceiptV2")


@dataclass(frozen=True, slots=True)
class CommitReceipt:
    """Typed, immutable receipt issued by a transactional commit port.

    Carries the durable intent id, the caller's idempotency key, the commit
    timestamp (aware), a fence fingerprint, the capability/purpose/resource
    verified for this exact action, the sha256 of the canonical prepared
    payload and the evidence refs that authorised it — so a consumer can
    re-verify the receipt before adopting it.  Construction validates every
    field; the value is opaque to callers (no free-form fabrication).
    """

    intent_id: str
    idempotency_key: str
    committed_at: datetime
    fence_fingerprint: str
    authority_revision: int
    capability: str
    purpose: str
    resource_id: str
    intent_sha256: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("intent_id", self.intent_id),
            ("idempotency_key", self.idempotency_key),
            ("capability", self.capability),
            ("purpose", self.purpose),
            ("resource_id", self.resource_id),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT_LEN:
                raise ValueError(f"{label} must be a bounded non-blank string")
        if (
            not isinstance(self.committed_at, datetime)
            or self.committed_at.tzinfo is None
            or self.committed_at.utcoffset() is None
        ):
            raise ValueError("committed_at must be timezone-aware")
        if not isinstance(self.fence_fingerprint, str) or not _FINGERPRINT_RE.fullmatch(
            self.fence_fingerprint
        ):
            raise ValueError("fence_fingerprint must be a 64-char hex digest")
        if type(self.authority_revision) is not int or self.authority_revision < 1:
            raise ValueError("authority_revision must be a positive int")
        if not isinstance(self.intent_sha256, str) or not _FINGERPRINT_RE.fullmatch(
            self.intent_sha256
        ):
            raise ValueError("intent_sha256 must be a 64-char hex digest")
        if (
            not isinstance(self.evidence_refs, tuple)
            or not self.evidence_refs
            or any(
                not isinstance(ref, str) or not ref.strip() or len(ref) > _MAX_TEXT_LEN
                for ref in self.evidence_refs
            )
        ):
            raise ValueError("evidence_refs must be a non-empty tuple of bounded ids")


@dataclass(frozen=True, slots=True)
class PreparedToolEffect:
    """Immutable prepared side-effect intent produced by a preparer.

    The preparer only assembles a serializable intent; every external write
    lives exclusively inside the commit port.  The payload is canonicalised
    to bounded JSON bytes and deep-frozen at construction (non-JSON values,
    NaN/Infinity, mutable containers and oversized payloads are rejected),
    and pinned by a sha256 digest, so the port always receives fixed content
    that cannot be mutated in place.
    """

    intent: str
    payload: Any
    payload_json: bytes = field(init=False, repr=False)
    payload_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.intent, str)
            or not self.intent.strip()
            or len(self.intent) > _MAX_TEXT_LEN
        ):
            raise ValueError("intent must be a bounded non-blank string")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be a mapping")
        data = canonical_json_bytes(self.payload)
        object.__setattr__(self, "payload_json", data)
        object.__setattr__(self, "payload_sha256", hashlib.sha256(data).hexdigest())
        object.__setattr__(self, "payload", freeze_json_value(self.payload))


class CommitReconcileState(StrEnum):
    """Outcome of an idempotency-key lookup against the durable intent store."""

    COMMITTED = "committed"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CommitReconcileResult:
    """Typed reconcile/lookup outcome; ``UNKNOWN`` must never be re-done."""

    state: CommitReconcileState
    receipt: CommitReceipt | None = None

    def __post_init__(self) -> None:
        if self.state is CommitReconcileState.COMMITTED and self.receipt is None:
            raise ValueError("COMMITTED requires the original receipt")
        if self.state is not CommitReconcileState.COMMITTED and self.receipt is not None:
            raise ValueError("only COMMITTED carries a receipt")


class TransactionalToolEffectCommitPort(Protocol):
    """One atomic verify-and-record operation for side-effect intents."""

    async def commit_prepared(
        self,
        *,
        spec: ToolSpec,
        authorization: EffectAuthorization,
        prepared: PreparedToolEffect,
        idempotency_key: str,
        now: datetime,
    ) -> CommitReceipt | None:
        """Atomically verify and durably record; None fails closed.

        The implementation MUST re-verify, inside one transactional step:
        the complete authorization fence against the runtime, the signed
        RuntimeProfile/capability, and the action-specific PolicyReceipt
        evidence (exact capability/purpose/resource/current validity/expiry/
        revocation).  It then durably inserts/CASes the prepared intent under
        the caller-supplied ``idempotency_key`` and returns a typed
        :class:`CommitReceipt` carrying the exact verified fields.

        Atomic semantics under cancellation/timeout: the operation is all-or-
        nothing at the record boundary.  If the caller's timeout cancels the
        coroutine mid-transaction, the implementation either (a) commits and
        keeps the record discoverable by the same ``idempotency_key`` for a
        later reconciliation, or (b) rolls back.  A late return value after
        cancellation is ignored by the caller; a re-commit under the same key
        must reconcile to the same intent (idempotent), never a duplicate.
        """
        ...

    async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
        """Look up a previously requested intent by its stable key.

        ``COMMITTED`` carries the original :class:`CommitReceipt` (the caller
        must verify it against the exact action identity before adopting and
        must not commit again); ``NOT_FOUND`` means the intent was never
        durably recorded and a new commit is allowed; ``UNKNOWN`` means the
        authority cannot currently determine the outcome — the caller must
        fail closed and never retry under a new key.
        """
        ...


class DefaultDenyEffectCommitPort:
    """Fail-closed core: no side-effect intent can commit until a real
    transactional adapter (Control/Policy baseline) is wired."""

    async def commit_prepared(
        self,
        *,
        spec: ToolSpec,
        authorization: EffectAuthorization,
        prepared: PreparedToolEffect,
        idempotency_key: str,
        now: datetime,
    ) -> CommitReceipt | None:
        del spec, authorization, prepared, idempotency_key, now
        return None

    async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
        del idempotency_key
        return CommitReconcileResult(CommitReconcileState.NOT_FOUND)


@dataclass(frozen=True, slots=True)
class TransactionalToolEffectCommitClientConfig:
    """Internal Control endpoints and bounded HTTP timeout."""

    endpoint: str
    reconcile_endpoint: str
    internal_token: str
    timeout_s: float = 0.8

    def __post_init__(self) -> None:
        for value, name in (
            (self.endpoint, "commit endpoint"),
            (self.reconcile_endpoint, "reconcile endpoint"),
        ):
            parsed = httpx.URL(value)
            if parsed.scheme not in {"http", "https"} or not parsed.host:
                raise ValueError(f"{name} must be HTTP(S)")
        if not self.endpoint.endswith("/tool-effect/commit"):
            raise ValueError("commit endpoint must end with /tool-effect/commit")
        if not self.reconcile_endpoint.endswith("/tool-effect/reconcile"):
            raise ValueError(
                "reconcile endpoint must end with /tool-effect/reconcile"
            )
        if not self.internal_token.strip():
            raise ValueError("transactional effect internal token must not be blank")
        if self.timeout_s <= 0:
            raise ValueError("transactional effect timeout must be positive")


_COMMIT_RECEIPT_KEYS = frozenset(
    {
        "intent_id",
        "idempotency_key",
        "committed_at",
        "fence_fingerprint",
        "authority_revision",
        "capability",
        "purpose",
        "resource_id",
        "intent_sha256",
        "evidence_refs",
    }
)


def _parse_commit_receipt(value: object) -> CommitReceipt | None:
    if not isinstance(value, dict) or set(value) != _COMMIT_RECEIPT_KEYS:
        return None
    try:
        committed_at_raw = value["committed_at"]
        if not isinstance(committed_at_raw, str):
            return None
        committed_at = datetime.fromisoformat(committed_at_raw.replace("Z", "+00:00"))
        evidence_raw = value["evidence_refs"]
        if not isinstance(evidence_raw, list) or any(
            not isinstance(item, str) for item in evidence_raw
        ):
            return None
        return CommitReceipt(
            intent_id=value["intent_id"],
            idempotency_key=value["idempotency_key"],
            committed_at=committed_at,
            fence_fingerprint=value["fence_fingerprint"],
            authority_revision=value["authority_revision"],
            capability=value["capability"],
            purpose=value["purpose"],
            resource_id=value["resource_id"],
            intent_sha256=value["intent_sha256"],
            evidence_refs=tuple(evidence_raw),
        )
    except (TypeError, ValueError):
        return None


class TransactionalToolEffectCommitClient:
    """Real Agent HTTP adapter for the durable Control commit authority."""

    def __init__(
        self,
        config: TransactionalToolEffectCommitClientConfig | None = None,
        *,
        endpoint: str | None = None,
        reconcile_endpoint: str | None = None,
        internal_token: str | None = None,
        timeout_s: float = 0.8,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config is None:
            if endpoint is None or reconcile_endpoint is None or internal_token is None:
                raise ValueError("commit client config is incomplete")
            config = TransactionalToolEffectCommitClientConfig(
                endpoint=endpoint,
                reconcile_endpoint=reconcile_endpoint,
                internal_token=internal_token,
                timeout_s=timeout_s,
            )
        self._config = config
        self._client = client or httpx.AsyncClient(
            timeout=config.timeout_s,
            transport=transport,
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def commit_prepared(
        self,
        *,
        spec: ToolSpec,
        authorization: EffectAuthorization,
        prepared: PreparedToolEffect,
        idempotency_key: str,
        now: datetime,
    ) -> CommitReceipt | None:
        profile = authorization.runtime_profile
        policy_receipt = authorization.policy_receipt
        if profile is None or policy_receipt is None:
            return None
        try:
            frozen = FrozenActionFence.from_profile(profile.profile, authorization.fence)
            verify_action_receipt(
                policy_receipt,
                fence=frozen,
                capability=authorization.capability,
                now=now,
                reject_persistence_obligations=True,
            )
        except (ActionReceiptRejected, ValueError):
            return None
        if (
            spec.required_capability != authorization.capability
            or spec.required_purpose != authorization.purpose
            or policy_receipt.purpose.value != authorization.purpose
            or policy_receipt.action_resource_fence.action_resource_id
            != authorization.resource_id
            or authorization.evidence_refs != (policy_receipt.receipt_id,)
        ):
            return None
        try:
            payload = json.loads(prepared.payload_json)
            response = await self._client.post(
                self._config.endpoint,
                headers={"X-Memoria-Internal-Token": self._config.internal_token},
                json={
                    "session_id": authorization.fence.session_id,
                    "fence": {
                        "session_id": authorization.fence.session_id,
                        "session_epoch": authorization.fence.session_epoch,
                        "generation_id": authorization.fence.generation_id,
                        "turn_id": authorization.fence.turn_id,
                        "tool_epoch": authorization.fence.tool_epoch,
                    },
                    "runtime_profile": profile.wire_payload_copy(),
                    "policy_receipt": policy_receipt.model_dump(mode="json"),
                    "capability": authorization.capability,
                    "purpose": authorization.purpose,
                    "resource_id": authorization.resource_id,
                    "evidence_refs": list(authorization.evidence_refs),
                    "idempotency_key": idempotency_key,
                    "intent": prepared.intent,
                    "payload": payload,
                    "payload_sha256": prepared.payload_sha256,
                    "fence_fingerprint": fence_fingerprint(authorization.fence),
                },
                timeout=self._config.timeout_s,
            )
        except (httpx.TimeoutException, httpx.NetworkError, ValueError, TypeError):
            return None
        if response.status_code != 200:
            return None
        try:
            receipt = _parse_commit_receipt(response.json())
        except ValueError:
            return None
        if receipt is None or not _receipt_matches_action(
            receipt,
            authorization=authorization,
            prepared=prepared,
            idempotency_key=idempotency_key,
        ):
            return None
        return receipt

    async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
        try:
            response = await self._client.post(
                self._config.reconcile_endpoint,
                headers={"X-Memoria-Internal-Token": self._config.internal_token},
                json={"idempotency_key": idempotency_key},
                timeout=self._config.timeout_s,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return CommitReconcileResult(CommitReconcileState.UNKNOWN)
        if response.status_code != 200:
            return CommitReconcileResult(CommitReconcileState.UNKNOWN)
        try:
            value = response.json()
        except ValueError:
            return CommitReconcileResult(CommitReconcileState.UNKNOWN)
        if not isinstance(value, dict):
            return CommitReconcileResult(CommitReconcileState.UNKNOWN)
        state_raw = value.get("state")
        if state_raw == CommitReconcileState.NOT_FOUND.value and set(value) == {"state"}:
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)
        if state_raw == CommitReconcileState.UNKNOWN.value and set(value) == {"state"}:
            return CommitReconcileResult(CommitReconcileState.UNKNOWN)
        if state_raw != CommitReconcileState.COMMITTED.value or set(value) != {
            "state",
            "receipt",
        }:
            return CommitReconcileResult(CommitReconcileState.UNKNOWN)
        receipt = _parse_commit_receipt(value.get("receipt"))
        if receipt is None or receipt.idempotency_key != idempotency_key:
            return CommitReconcileResult(CommitReconcileState.UNKNOWN)
        return CommitReconcileResult(CommitReconcileState.COMMITTED, receipt)


def _receipt_matches_action(
    receipt: CommitReceipt,
    *,
    authorization: EffectAuthorization,
    prepared: PreparedToolEffect,
    idempotency_key: str,
) -> bool:
    return (
        receipt.idempotency_key == idempotency_key
        and receipt.fence_fingerprint == fence_fingerprint(authorization.fence)
        and receipt.capability == authorization.capability
        and receipt.purpose == authorization.purpose
        and receipt.resource_id == authorization.resource_id
        and receipt.intent_sha256 == prepared.payload_sha256
        and receipt.evidence_refs == authorization.evidence_refs
    )


__all__ = [
    "CommitReceipt",
    "CommitReconcileResult",
    "CommitReconcileState",
    "DefaultDenyEffectCommitPort",
    "EffectAuthorization",
    "PreparedToolEffect",
    "TransactionalToolEffectCommitClient",
    "TransactionalToolEffectCommitClientConfig",
    "TransactionalToolEffectCommitPort",
    "canonical_json_bytes",
    "fence_fingerprint",
    "freeze_json_value",
]
