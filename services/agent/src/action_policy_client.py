"""Action-time deferred-capability authorization client (archive consumer).

The only authority for an archived user/assistant turn is the internal Control
``POST /v1/interaction/action-policy`` endpoint, which returns one complete,
immutable ``PolicyReceiptV2`` bound to an exact action fence.  This module is
the deep consumer-side seam:

* :class:`FrozenActionFence` freezes the complete profile/session/generation/
  turn/tool identity *before* any HTTP call.  Nothing is ever accepted from the
  caller at verify time; a fence whose session or epoch does not exactly match
  the signed ``RuntimeProfile`` (a subject switch or a late event from a
  previous epoch) cannot even be frozen.
* :class:`ActionPolicyClient` fails closed: a transport error, non-200 status,
  contract-invalid payload or any identity/fence mismatch becomes
  :class:`ActionPolicyUnavailable`, never a partial authorization.
* :func:`verify_action_receipt` locally re-checks every field that pins the
  receipt to one exact profile and one exact action fence (actor, subject,
  resource owner, device, binding/version, profile, session, epoch, subject
  revision, generation/turn/tool, capability/purpose, exact evidence fence,
  both action hashes and the validity window).
* :func:`user_event_evidence_fence` / :func:`verify_assistant_child_fence`
  build and cross-check the canonical archive evidence fence: an assistant
  child event may only be archived with the exact parent fence, and an event
  without an authoritative fence is never archived.

The runtime installs this client at the side-effect TaskManager seam;
DefaultDenyActionPolicy remains the production fallback when the authority
token or endpoint is unavailable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

import httpx
from packages.contracts.generated.python.multi_subject_contracts import (
    DataClassification,
    SafetyState,
)

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.receipt_evidence import RECEIPT_PURPOSE_CONTRACT
from services.agent.src.runtime_profile import RuntimeProfile
from services.policy.action_fence import verify_action_resource_fence
from services.policy.receipts import PolicyReceiptV2

EVIDENCE_FENCE_SCHEMA: Final[str] = "action-policy-evidence-fence-v1"
ACTION_POLICY_CAPABILITIES: Final[frozenset[str]] = frozenset(
    RECEIPT_PURPOSE_CONTRACT
) - {"chat"}

_RECEIPT_TOKEN = object()
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

# Canonical archive evidence-fence key set.  A fence is only valid when it
# carries exactly these keys: no extra caller fields can survive.
EVIDENCE_FENCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        "session_id",
        "runtime_profile_id",
        "session_epoch",
        "generation_id",
        "turn_id",
        "tool_epoch",
        "actor_id",
        "subject_id",
        "resource_owner_id",
        "device_id",
        "binding_id",
        "binding_version",
        "subject_revision",
        "capability",
        "purpose",
        "policy_receipt_id",
        "action_fence_hash",
        "context_hash",
        "exact_fence",
        "issued_at",
        "expires_at",
    }
)

_BOUNDED_ID_KEYS: Final[frozenset[str]] = frozenset(
    {
        "session_id",
        "runtime_profile_id",
        "actor_id",
        "subject_id",
        "resource_owner_id",
        "device_id",
        "binding_id",
        "policy_receipt_id",
    }
)

_INTEGER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "session_epoch",
        "generation_id",
        "turn_id",
        "tool_epoch",
        "binding_version",
        "subject_revision",
    }
)


class ActionFenceRejected(ValueError):
    """The profile and generation fence do not form one exact identity pair."""


class ActionReceiptRejected(ValueError):
    """A server-issued receipt failed local fence verification (fail closed)."""


@dataclass(frozen=True, slots=True)
class FrozenActionFence:
    """Complete frozen profile/session/generation/turn/tool identity.

    Only :meth:`from_profile` can build one; it pins the signed profile's
    identity fields together with one exact ``GenerationFence`` so a subject
    switch, epoch advance or late fence can never be authorized.
    """

    session_id: str
    runtime_profile_id: str
    session_epoch: int
    generation_id: int
    turn_id: int
    tool_epoch: int
    actor_id: str
    subject_id: str
    resource_owner_id: str
    device_id: str
    binding_id: str
    binding_version: int
    subject_revision: int

    @classmethod
    def from_profile(
        cls,
        profile: RuntimeProfile,
        fence: GenerationFence,
    ) -> FrozenActionFence:
        """Freeze one exact identity pair, or raise ``ActionFenceRejected``.

        Fail-closed rules:
        * the fence session and session epoch must equal the profile's (a
          late event from a previous subject/epoch cannot be frozen);
        * a confirmed speaker with a named active subject is required (an
          unknown-safe or unconfirmed session has no subject to pin);
        * the resource owner is pinned to the active subject; owner-scoped
          family flows that need a different owner fail closed here until the
          profile carries that identity.
        """

        if fence.session_id != profile.session_id:
            raise ActionFenceRejected(
                "fence session does not match the signed profile session"
            )
        if fence.session_epoch != profile.session_epoch:
            raise ActionFenceRejected(
                "fence session epoch does not match the signed profile epoch "
                "(subject switch or late fence)"
            )
        if profile.active_subject_id is None or not profile.active_subject_id.strip():
            raise ActionFenceRejected("profile has no active subject to pin")
        if profile.speaker_state != "confirmed":
            raise ActionFenceRejected("profile speaker is not confirmed")
        return cls(
            session_id=profile.session_id,
            runtime_profile_id=profile.runtime_profile_id,
            session_epoch=profile.session_epoch,
            generation_id=fence.generation_id,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            actor_id=profile.actor_id,
            subject_id=profile.active_subject_id,
            resource_owner_id=profile.active_subject_id,
            device_id=profile.device_id,
            binding_id=profile.binding_id,
            binding_version=profile.binding_version,
            subject_revision=profile.subject_revision,
        )

    def request_body(
        self,
        *,
        capability: str,
        data_classification: str,
        safety_state: str,
    ) -> dict[str, object]:
        """The exact wire body; identity fields are never caller-supplied."""

        return {
            "session_id": self.session_id,
            "runtime_profile_id": self.runtime_profile_id,
            "capability": capability,
            "session_epoch": self.session_epoch,
            "generation_id": self.generation_id,
            "turn_id": self.turn_id,
            "tool_epoch": self.tool_epoch,
            "data_classification": data_classification,
            "safety_state": safety_state,
        }


@dataclass(frozen=True, slots=True, init=False)
class VerifiedActionReceipt:
    """A server-issued ``PolicyReceiptV2`` locally pinned to one exact fence.

    Construction is token-guarded: only :func:`verify_action_receipt` can
    produce this type after every identity and fence field has been checked.
    """

    receipt_id: str
    capability: str
    purpose: str
    effect: str
    reason_code: str
    policy_version: str
    context_hash: str
    action_fence_hash: str
    exact_fence: bool
    actor_id: str
    subject_id: str
    resource_owner_id: str
    device_id: str
    binding_id: str
    binding_version: int
    runtime_profile_id: str
    session_id: str
    session_epoch: int
    subject_revision: int
    generation_id: int
    turn_id: int
    tool_epoch: int
    issued_at: datetime
    expires_at: datetime
    obligations: tuple[str, ...]
    receipt: PolicyReceiptV2

    def __init__(
        self,
        *,
        receipt_id: str,
        capability: str,
        purpose: str,
        effect: str,
        reason_code: str,
        policy_version: str,
        context_hash: str,
        action_fence_hash: str,
        exact_fence: bool,
        actor_id: str,
        subject_id: str,
        resource_owner_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
        runtime_profile_id: str,
        session_id: str,
        session_epoch: int,
        subject_revision: int,
        generation_id: int,
        turn_id: int,
        tool_epoch: int,
        issued_at: datetime,
        expires_at: datetime,
        obligations: tuple[str, ...],
        receipt: PolicyReceiptV2,
        _token: object,
    ) -> None:
        if _token is not _RECEIPT_TOKEN:
            raise TypeError("VerifiedActionReceipt: forged construction rejected")
        if not exact_fence:
            raise ValueError("VerifiedActionReceipt requires exact_fence=True")
        if expires_at.tzinfo is None or issued_at.tzinfo is None:
            raise ValueError("fence timestamps must carry a timezone")
        for name, value in (
            ("receipt_id", receipt_id),
            ("capability", capability),
            ("purpose", purpose),
            ("actor_id", actor_id),
            ("subject_id", subject_id),
            ("resource_owner_id", resource_owner_id),
            ("device_id", device_id),
            ("binding_id", binding_id),
            ("runtime_profile_id", runtime_profile_id),
            ("session_id", session_id),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"{name} must be a bounded non-blank string")
        for numeric_name, numeric_value in (
            ("binding_version", binding_version),
            ("session_epoch", session_epoch),
            ("subject_revision", subject_revision),
            ("generation_id", generation_id),
            ("turn_id", turn_id),
            ("tool_epoch", tool_epoch),
        ):
            if (
                not isinstance(numeric_value, int)
                or isinstance(numeric_value, bool)
                or numeric_value < 0
            ):
                raise ValueError(
                    f"{numeric_name} must be a non-negative integer"
                )
        if binding_version < 1 or session_epoch < 1:
            raise ValueError("binding_version and session_epoch must be >= 1")
        if not _SHA256_PATTERN.fullmatch(context_hash):
            raise ValueError("context_hash must be a sha256 digest")
        if not _SHA256_PATTERN.fullmatch(action_fence_hash):
            raise ValueError("action_fence_hash must be a sha256 digest")
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "effect", effect)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "policy_version", policy_version)
        object.__setattr__(self, "context_hash", context_hash)
        object.__setattr__(self, "action_fence_hash", action_fence_hash)
        object.__setattr__(self, "exact_fence", exact_fence)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "subject_id", subject_id)
        object.__setattr__(self, "resource_owner_id", resource_owner_id)
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "binding_id", binding_id)
        object.__setattr__(self, "binding_version", binding_version)
        object.__setattr__(self, "runtime_profile_id", runtime_profile_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "session_epoch", session_epoch)
        object.__setattr__(self, "subject_revision", subject_revision)
        object.__setattr__(self, "generation_id", generation_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "tool_epoch", tool_epoch)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "obligations", obligations)
        object.__setattr__(self, "receipt", receipt)

    def is_expired(self, now: datetime) -> bool:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC) >= self.expires_at.astimezone(UTC)

    def matches_fence(self, fence: FrozenActionFence) -> bool:
        """Exact per-fence binding, defense in depth after verification."""

        return (
            self.session_id == fence.session_id
            and self.runtime_profile_id == fence.runtime_profile_id
            and self.session_epoch == fence.session_epoch
            and self.generation_id == fence.generation_id
            and self.turn_id == fence.turn_id
            and self.tool_epoch == fence.tool_epoch
            and self.actor_id == fence.actor_id
            and self.subject_id == fence.subject_id
            and self.resource_owner_id == fence.resource_owner_id
            and self.device_id == fence.device_id
            and self.binding_id == fence.binding_id
            and self.binding_version == fence.binding_version
            and self.subject_revision == fence.subject_revision
        )

    @property
    def forbids_persistence(self) -> bool:
        return "DO_NOT_PERSIST" in self.obligations

    def evidence_fence(self) -> dict[str, object]:
        """The canonical archive evidence fence for the authorized event.

        One exact, deterministic identity + authority record.  User events
        carry the fence produced by the same receipt; assistant child events
        must carry the exact same fence (see :func:`verify_assistant_child_fence`).
        """

        return {
            "schema": EVIDENCE_FENCE_SCHEMA,
            "session_id": self.session_id,
            "runtime_profile_id": self.runtime_profile_id,
            "session_epoch": self.session_epoch,
            "generation_id": self.generation_id,
            "turn_id": self.turn_id,
            "tool_epoch": self.tool_epoch,
            "actor_id": self.actor_id,
            "subject_id": self.subject_id,
            "resource_owner_id": self.resource_owner_id,
            "device_id": self.device_id,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "subject_revision": self.subject_revision,
            "capability": self.capability,
            "purpose": self.purpose,
            "policy_receipt_id": self.receipt_id,
            "action_fence_hash": self.action_fence_hash,
            "context_hash": self.context_hash,
            "exact_fence": True,
            "issued_at": _rfc3339(self.issued_at),
            "expires_at": _rfc3339(self.expires_at),
        }


def verify_action_receipt(
    receipt: PolicyReceiptV2,
    *,
    fence: FrozenActionFence,
    capability: str,
    now: datetime,
    reject_persistence_obligations: bool = True,
) -> VerifiedActionReceipt:
    """Locally pin one server-issued receipt to the frozen fence (fail closed).

    Raises ActionReceiptRejected on any mismatch: deny effect, optionally
    persistence-forbidding obligations, capability/purpose deviation, a
    non-exact evidence fence, any identity drift, action-fence drift, hash
    tampering or an out-of-window receipt.
    """

    if receipt.effect not in {"allow", "allow_with_obligations"}:
        raise ActionReceiptRejected("receipt effect is not allow")
    obligation_codes = {obligation.code for obligation in receipt.obligations}
    if reject_persistence_obligations and "DO_NOT_PERSIST" in obligation_codes:
        raise ActionReceiptRejected("receipt obligations forbid persistence")
    if receipt.capability != capability:
        raise ActionReceiptRejected("receipt capability does not match the request")
    expected_purpose = RECEIPT_PURPOSE_CONTRACT.get(capability)
    if expected_purpose is None:
        raise ActionReceiptRejected(
            f"capability {capability!r} has no archive purpose contract"
        )
    if receipt.purpose != expected_purpose:
        raise ActionReceiptRejected("receipt purpose does not match the capability contract")
    if receipt.exact_fence is not True:
        raise ActionReceiptRejected("archive requires an exact evidence fence")
    if receipt.binding_canonical_hash is None:
        raise ActionReceiptRejected("exact receipt requires the binding canonical hash")
    identity_pairs = (
        (receipt.actor_id, fence.actor_id),
        (receipt.subject_id, fence.subject_id),
        (receipt.resource_owner_id, fence.resource_owner_id),
        (receipt.device_id, fence.device_id),
        (receipt.binding_id, fence.binding_id),
        (receipt.binding_version, fence.binding_version),
        (receipt.runtime_profile_id, fence.runtime_profile_id),
        (receipt.session_id, fence.session_id),
        (receipt.session_epoch, fence.session_epoch),
        (receipt.subject_revision, fence.subject_revision),
    )
    if any(left != right for left, right in identity_pairs):
        raise ActionReceiptRejected("receipt identity does not match the frozen fence")
    if receipt.subject_id is None or receipt.resource_owner_id is None:
        raise ActionReceiptRejected("receipt must name the subject and resource owner")
    action_fence = receipt.action_resource_fence
    if receipt.action_fence_hash != action_fence.canonical_hash:
        raise ActionReceiptRejected("action fence hash is not self-consistent")
    if not verify_action_resource_fence(action_fence):
        raise ActionReceiptRejected("action resource fence hashes are forged")
    if (
        action_fence.generation_id != fence.generation_id
        or action_fence.turn_id != fence.turn_id
        or action_fence.tool_epoch != fence.tool_epoch
    ):
        raise ActionReceiptRejected("action fence generation/turn/tool drifted")
    if action_fence.capability != capability or action_fence.purpose != expected_purpose:
        raise ActionReceiptRejected("action fence capability/purpose drifted")
    if not action_fence.action_resource_id.strip() or action_fence.action_revision < 1:
        raise ActionReceiptRejected("action fence resource identity is invalid")
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now_utc = now.astimezone(UTC)
    if not receipt.created_at <= now_utc < receipt.expires_at:
        raise ActionReceiptRejected("receipt validity window does not cover now")
    if not action_fence.issued_at <= now_utc < action_fence.valid_until:
        raise ActionReceiptRejected("action fence validity window does not cover now")
    return VerifiedActionReceipt(
        receipt_id=receipt.receipt_id,
        capability=receipt.capability,
        purpose=receipt.purpose,
        effect=receipt.effect,
        reason_code=receipt.reason_code,
        policy_version=receipt.policy_version,
        context_hash=receipt.context_hash,
        action_fence_hash=receipt.action_fence_hash,
        exact_fence=receipt.exact_fence,
        actor_id=receipt.actor_id,
        subject_id=receipt.subject_id,
        resource_owner_id=receipt.resource_owner_id,
        device_id=receipt.device_id,
        binding_id=receipt.binding_id,
        binding_version=receipt.binding_version,
        runtime_profile_id=receipt.runtime_profile_id,
        session_id=receipt.session_id,
        session_epoch=receipt.session_epoch,
        subject_revision=receipt.subject_revision,
        generation_id=action_fence.generation_id,
        turn_id=action_fence.turn_id,
        tool_epoch=action_fence.tool_epoch,
        issued_at=action_fence.issued_at,
        expires_at=receipt.expires_at,
        obligations=tuple(sorted(obligation_codes)),
        receipt=receipt,
        _token=_RECEIPT_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class ActionPolicyUnavailable:
    """Fail-closed outcome: no authorization exists for this fence."""

    reason: str


ActionPolicyResult = VerifiedActionReceipt | ActionPolicyUnavailable


@dataclass(frozen=True, slots=True)
class ActionPolicyClientConfig:
    endpoint: str
    internal_token: str
    timeout_s: float = 0.4

    def __post_init__(self) -> None:
        url = httpx.URL(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("action policy endpoint must be HTTP(S)")
        if not self.endpoint.endswith("/action-policy"):
            raise ValueError("action policy endpoint must end with /action-policy")
        if not self.internal_token.strip():
            raise ValueError("action policy internal token must not be blank")
        if self.timeout_s <= 0:
            raise ValueError("action policy timeout must be positive")


class ActionPolicyClient:
    """Call the internal action-policy endpoint with one frozen fence.

    Fail-closed: any failure becomes ``ActionPolicyUnavailable``; a verified
    receipt is only returned after full local re-verification.
    """

    def __init__(
        self,
        config: ActionPolicyClientConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_s)
        self._owns_client = client is None

    async def authorize(
        self,
        *,
        profile: RuntimeProfile,
        fence: GenerationFence,
        capability: str,
        data_classification: str = "private",
        safety_state: str = "normal",
        accept_obligations: bool = False,
    ) -> ActionPolicyResult:
        """Authorize one deferred capability at one exact generation fence."""

        if not is_action_policy_capability(capability):
            return ActionPolicyUnavailable("capability_not_deferred")
        try:
            DataClassification(data_classification)
            SafetyState(safety_state)
        except ValueError:
            return ActionPolicyUnavailable("classification_invalid")
        try:
            frozen = FrozenActionFence.from_profile(profile, fence)
        except ActionFenceRejected:
            return ActionPolicyUnavailable("fence_unfreezable")
        try:
            response = await self._client.post(
                self._config.endpoint,
                headers={"X-Memoria-Internal-Token": self._config.internal_token},
                json=frozen.request_body(
                    capability=capability,
                    data_classification=data_classification,
                    safety_state=safety_state,
                ),
                timeout=self._config.timeout_s,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return ActionPolicyUnavailable("authority_unreachable")
        if response.status_code != 200:
            return ActionPolicyUnavailable(f"http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            return ActionPolicyUnavailable("response_not_json")
        if not isinstance(payload, dict):
            return ActionPolicyUnavailable("response_not_object")
        try:
            receipt = PolicyReceiptV2.model_validate(payload)
        except ValueError:
            return ActionPolicyUnavailable("receipt_invalid")
        try:
            verified = verify_action_receipt(
                receipt,
                fence=frozen,
                capability=capability,
                now=datetime.now(UTC),
                reject_persistence_obligations=not accept_obligations,
            )
        except ActionReceiptRejected:
            return ActionPolicyUnavailable("receipt_rejected")
        return verified

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def is_action_policy_capability(capability: object) -> bool:
    """Return whether a capability is intentionally deferred to action time."""
    return isinstance(capability, str) and capability in ACTION_POLICY_CAPABILITIES


class ActionPolicyPort(Protocol):
    """One action-time authorization decision; unavailable is the safe value."""

    async def authorize(
        self,
        *,
        profile: RuntimeProfile,
        fence: GenerationFence,
        capability: str,
        data_classification: str = "private",
        safety_state: str = "normal",
        accept_obligations: bool = False,
    ) -> ActionPolicyResult: ...


class DefaultDenyActionPolicy:
    """Fail-closed default until the action-policy client is wired."""

    async def authorize(
        self,
        *,
        profile: RuntimeProfile,
        fence: GenerationFence,
        capability: str,
        data_classification: str = "private",
        safety_state: str = "normal",
        accept_obligations: bool = False,
    ) -> ActionPolicyResult:
        del profile, fence, capability, data_classification, safety_state, accept_obligations
        return ActionPolicyUnavailable("default_deny")


def user_event_evidence_fence(receipt: VerifiedActionReceipt) -> dict[str, object]:
    """Canonical archive evidence fence for the user event being authorized."""

    return receipt.evidence_fence()


def assistant_child_evidence_fence(receipt: VerifiedActionReceipt) -> dict[str, object]:
    """The assistant child event must carry the exact parent fence.

    The assistant turn answering a user turn is archived under the same
    action receipt: same identity, same epoch and same generation/turn/tool
    fence.  A child fence built from a different receipt or a bumped fence
    will not match its parent and is rejected by
    :func:`verify_assistant_child_fence`.
    """

    return receipt.evidence_fence()


def verify_assistant_child_fence(
    parent: Mapping[str, object] | None,
    child: Mapping[str, object] | None,
) -> bool:
    """An assistant child event may be archived only with the exact parent fence."""

    if parent is None or child is None:
        return False
    return dict(parent) == dict(child)


def archive_fence_valid(
    fence: object,
    *,
    capability: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Structural archive gate for one evidence fence (fail closed).

    Accepts only the canonical schema with the exact key set, bounded
    identities, valid integers and sha256 authority hashes.  Expiry is
    checked only when ``now`` is supplied: spool replay of an already
    authorized event stays valid, while a fresh authorization at verify time
    must fall inside the window.
    """

    if not isinstance(fence, Mapping):
        return False
    if set(fence) != EVIDENCE_FENCE_KEYS:
        return False
    if fence.get("schema") != EVIDENCE_FENCE_SCHEMA:
        return False
    if fence.get("exact_fence") is not True:
        return False
    for key in _BOUNDED_ID_KEYS:
        value = fence.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            return False
    for key in ("capability", "purpose"):
        value = fence.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 64:
            return False
    if capability is not None and fence.get("capability") != capability:
        return False
    for key in _INTEGER_KEYS:
        value = fence.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
    if not isinstance(fence.get("binding_version"), int) or fence["binding_version"] < 1:
        return False
    if not isinstance(fence.get("session_epoch"), int) or fence["session_epoch"] < 1:
        return False
    for key in ("action_fence_hash", "context_hash"):
        value = fence.get(key)
        if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
            return False
    for key in ("issued_at", "expires_at"):
        value = fence.get(key)
        if not isinstance(value, str) or not _RFC3339_PATTERN.fullmatch(value):
            return False
    try:
        issued_at = _parse_rfc3339(str(fence["issued_at"]))
        expires_at = _parse_rfc3339(str(fence["expires_at"]))
    except ValueError:
        return False
    if not issued_at < expires_at:
        return False
    if now is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if now.astimezone(UTC) >= expires_at:
            return False
    return True


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must carry an offset")
    return parsed.astimezone(UTC)


__all__ = [
    "ACTION_POLICY_CAPABILITIES",
    "ActionFenceRejected",
    "ActionPolicyClient",
    "ActionPolicyClientConfig",
    "ActionPolicyPort",
    "ActionPolicyResult",
    "ActionPolicyUnavailable",
    "ActionReceiptRejected",
    "DefaultDenyActionPolicy",
    "EVIDENCE_FENCE_KEYS",
    "EVIDENCE_FENCE_SCHEMA",
    "FrozenActionFence",
    "VerifiedActionReceipt",
    "archive_fence_valid",
    "assistant_child_evidence_fence",
    "is_action_policy_capability",
    "user_event_evidence_fence",
    "verify_action_receipt",
    "verify_assistant_child_fence",
]
