"""Pure contracts for the multi-role notification state machine.

Implements PR-15 of the 2026-08-09 multi-subject remediation plan:

- ``NotificationIntent`` / ``RecipientBinding`` / ``DeliveryAttempt`` /
  ``DeliveryReceipt`` state machine covering minor guardian, adult emergency
  contact and senior delegate recipients;
- content-minimized templates: no raw conversation, diagnosis or unverified
  psychological labels ever enter a notification (section 6.5 / 10.6);
- decoupled crisis turn response from channel delivery: creating an intent
  never performs delivery and never raises because a channel is down;
- recipient lifecycle ``pending -> in_progress -> delivered | failed ->
  fallback | dead_lettered`` with lease + fencing tokens, injectable
  exponential backoff with jitter, maximum retries, manual replay/cancel;
- wrong-contact cancellation with audited reason that forbids any further
  delivery; relationship ``revoked / expired / disputed`` fails closed.

This module has no framework or persistence dependencies.  Role and
relationship enums are imported from the canonical generated contracts
(ADR-0033 / PR-01) instead of redefining same-name-different-value enums.
"""

from __future__ import annotations

import hashlib
import random
import re
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args

from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRoleValue,
    RelationshipStatus,
    RelationshipStatusValue,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyObligation as CanonicalPolicyObligation,
)

from services.policy.receipts import PolicyReceiptV2

# ---------------------------------------------------------------------------
# Canonical values (subset / reuse only, never redefinition)
# ---------------------------------------------------------------------------

type IntentKind = Literal["crisis_safety", "emergency", "care_alert"]
type TemplateKey = Literal[
    "crisis_safety_notice", "emergency_notice", "care_alert_notice"
]
type ReasonCode = Literal["safety_concern", "emergency_alert", "care_reminder"]
type Channel = Literal["wechat_subscription", "sms", "phone_call"]


def _literal_values(alias: object) -> frozenset[str]:
    """Literal values from a PEP-695 ``type`` alias or a plain Literal."""
    value = getattr(alias, "__value__", alias)
    return frozenset(get_args(value))


#: Subset of the canonical BindingRole (ADR-0033): only recipient roles.
type RecipientRole = Literal["guardian", "emergency_contact", "delegate"]
assert _literal_values(RecipientRole) <= _literal_values(BindingRoleValue)

type RecipientStatus = Literal[
    "pending", "in_progress", "delivered", "failed", "dead_lettered", "cancelled"
]
type AttemptStatus = Literal["leased", "delivered", "failed", "uncertain"]
type IntentStatus = Literal[
    "pending", "in_progress", "delivered", "dead_lettered", "cancelled"
]
type CancelReason = Literal[
    "wrong_contact",
    "relationship_revoked",
    "relationship_expired",
    "relationship_disputed",
    "operator_override",
    "user_request",
    #: Extension beyond the generated contracts CancelReasonValue (which
    #: only knows the six relationship/operator/user values): used by the
    #: worker fail-closed path when the POLICY AUTHORIZATION itself is no
    #: longer valid at send time (receipt expired / consent revoked /
    #: binding bumped).  The contract agent must fold these two values into
    #: the canonical CancelReason enum; until then they are module-local
    #: contract values validated by the storage CHECK constraints.
    "authorization_revoked",
    "authorization_expired",
]

#: Canonical policy obligation codes a notification intent must be backed by
#: (CONTRACT §5.3).  Reused from the generated contracts - never redefined.
NOTIFY_EMERGENCY_CONTACT_OBLIGATION = (
    CanonicalPolicyObligation.POLICY_OBLIGATION_NOTIFY_EMERGENCY_CONTACT.value
)
MINIMAL_NOTIFICATION_CONTENT_OBLIGATION = (
    CanonicalPolicyObligation.POLICY_OBLIGATION_MINIMAL_NOTIFICATION_CONTENT.value
)

#: Parameterized obligation keys (PolicyReceiptV2.obligations[].params.extras)
#: that bind a crisis receipt to exactly one recipient role and intent kind.
#: A receipt without these params cannot authorize ANY recipient (fail closed).
OBLIGATION_PARAM_RECIPIENT_ROLE = "recipient_role"
OBLIGATION_PARAM_INTENT_KIND = "intent_kind"


@dataclass(frozen=True, slots=True)
class NotificationFence:
    """Session/binding/profile fence every notification intent is issued
    under (P0-4).  The policy receipt must have been recorded for the exact
    context fingerprint; the caller can never self-attest the receipt."""

    session_id: str
    epoch: int
    binding_id: str
    #: Binding manifest version (canonical BindingManifest: integer >= 1);
    #: validated at construction.
    binding_version: int
    runtime_profile_id: str
    actor_person_id: str
    subject_person_id: str
    #: Fence authorization window (P0 / Contract): REQUIRED,
    #: timezone-aware and bounded - a sensitive notification fence is never
    #: unlimited.  It is part of the canonical fingerprint, persisted with
    #: the intent, and reconstructed exactly before an external send; once
    #: expired the worker fails closed with authorization_expired.
    valid_until: datetime
    device_id: str = ""
    subject_revision: int = 0

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.session_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(self.epoch).encode("ascii"))
        digest.update(b"\x00")
        digest.update(self.binding_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(self.binding_version).encode("ascii"))
        digest.update(b"\x00")
        digest.update(self.runtime_profile_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.actor_person_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.subject_person_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.device_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(self.subject_revision).encode("ascii"))
        digest.update(b"\x00")
        digest.update(
            (self.valid_until.isoformat() if self.valid_until else "").encode("utf-8")
        )
        return digest.hexdigest()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.epoch, int)
            or isinstance(self.epoch, bool)
            or self.epoch < 1
        ):
            raise ValueError("notification fence epoch must be an integer >= 1")
        if (
            not isinstance(self.binding_version, int)
            or isinstance(self.binding_version, bool)
            or self.binding_version < 1
        ):
            raise ValueError(
                "binding_version must be an integer >= 1 (canonical "
                "BindingManifest semantics)"
            )
        if (
            not isinstance(self.subject_revision, int)
            or isinstance(self.subject_revision, bool)
            or self.subject_revision < 0
        ):
            raise ValueError("notification fence subject_revision must be >= 0")
        for name in (
            "session_id",
            "binding_id",
            "runtime_profile_id",
            "actor_person_id",
            "subject_person_id",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"notification fence {name} must not be empty")
        if (
            self.valid_until is None
            or self.valid_until.tzinfo is None
            or self.valid_until.utcoffset() is None
        ):
            raise ValueError(
                "notification fence valid_until is required and must be "
                "timezone-aware (a sensitive fence is never unlimited)"
            )

    def is_expired(self, now: datetime) -> bool:
        return self.valid_until is not None and now >= self.valid_until


def verify_notification_receipt(
    receipt: PolicyReceiptV2,
    *,
    fence: NotificationFence,
    actor_person_id: str,
    subject_person_id: str,
    capability: str,
    purpose: str,
    required_obligations: frozenset[str],
    now: datetime,
) -> bool:
    """Pure PolicyReceiptV2-vs-fence match (P0 contract): every identity
    field, purpose, capability, exact-fence flag and required obligation
    must agree; a wrong device/purpose/subject_revision, stale epoch,
    mismatched binding/profile or expired receipt fails closed.  The Policy
    V2 ``context_hash`` covers the FULL PolicyContext and is never compared
    to the local fence fingerprint: the authoritative verifier runs
    :func:`services.policy.receipts.receipt_fence_valid` /
    ``exact_evidence_fence_valid`` against the current evidence and the
    consumer requires that verified result (plus this field check)."""
    if receipt.effect == "deny":
        return False
    if receipt.capability != capability:
        return False
    if receipt.purpose != purpose:
        return False
    if receipt.actor_id != actor_person_id:
        return False
    if receipt.subject_id != subject_person_id:
        return False
    if receipt.resource_owner_id != subject_person_id:
        return False
    if receipt.device_id != fence.device_id:
        return False
    if receipt.session_id != fence.session_id:
        return False
    if receipt.session_epoch != fence.epoch:
        return False
    if receipt.runtime_profile_id != fence.runtime_profile_id:
        return False
    if receipt.binding_id != fence.binding_id:
        return False
    if receipt.binding_version != fence.binding_version:
        return False
    if receipt.subject_revision != fence.subject_revision:
        return False
    if not receipt.exact_fence:
        return False
    obligation_codes = {obligation.code for obligation in receipt.obligations}
    if not required_obligations.issubset(obligation_codes):
        return False
    if not receipt.created_at <= now < receipt.expires_at:
        return False
    return True


@dataclass(frozen=True, slots=True)
class RelationshipSnapshot:
    """Authoritative relationship snapshot from the identity/binding service
    (P0-5/P0-6).  Recipients are resolved from these snapshots, never from
    caller-claimed status strings.  ``snapshot_id`` + ``revision`` are the
    stable evidence persisted with every recipient so the worker can
    re-verify against the authoritative source right before an external
    send (TOCTOU, P0)."""

    relationship_id: str
    subject_person_id: str
    person_id: str
    role: RecipientRole
    status: RelationshipStatusValue
    snapshot_id: str = ""
    revision: int = 1
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    binding_version: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.revision, int) or isinstance(
            self.revision, bool
        ) or self.revision < 1:
            raise ValueError("relationship revision must be an integer >= 1")
        if self.binding_version is not None and (
            not isinstance(self.binding_version, int)
            or isinstance(self.binding_version, bool)
            or self.binding_version < 1
        ):
            raise ValueError("relationship binding_version must be an integer >= 1")
        for name in ("valid_from", "valid_until"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"relationship {name} must be timezone-aware")
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("relationship valid_until must be after valid_from")

    def is_active(self, now: datetime) -> bool:
        if self.status != RelationshipStatus.RELATIONSHIP_STATUS_ACTIVE.value:
            return False
        if self.valid_from is not None and now < self.valid_from:
            return False
        if self.valid_until is not None and now >= self.valid_until:
            return False
        return True


def recipient_evidence_ok(
    receipt: PolicyReceiptV2,
    *,
    snapshot: RelationshipSnapshot,
    intent_kind: IntentKind,
) -> bool:
    """Policy-crisis rule coupling (red team 12 / Policy V2 alignment):

    A recipient may only be created when the resolved relationship is part
    of the PolicyReceiptV2 evidence set AND the receipt's parameterized
    NOTIFY obligation names exactly this role and intent kind.

    - the exact ``(snapshot.snapshot_id, snapshot.revision)`` pair must
      appear in the receipt's aligned relationship evidence
      (``relationship_snapshot_ids`` + ``relationship_snapshot_revisions``);
      the revision is also proven against the authoritative resolver at
      enqueue time and persisted on the binding for the worker's pre-send
      re-check;
    - the receipt must carry the canonical ``NOTIFY_EMERGENCY_CONTACT``
      obligation whose ``params.extras`` contains
      ``recipient_role == snapshot.role`` and ``intent_kind == intent_kind``.

    Any other active relationship of the same subject can NOT ride the same
    crisis receipt; a receipt without the parameterized obligation fails
    closed.
    """
    evidence_pairs = set(
        zip(
            receipt.relationship_snapshot_ids,
            receipt.relationship_snapshot_revisions,
            strict=True,
        )
    )
    if (snapshot.snapshot_id, snapshot.revision) not in evidence_pairs:
        return False
    for obligation in receipt.obligations:
        if obligation.code != NOTIFY_EMERGENCY_CONTACT_OBLIGATION:
            continue
        extras = {key: value for key, value in obligation.params.extras}
        if (
            extras.get(OBLIGATION_PARAM_RECIPIENT_ROLE) == snapshot.role
            and extras.get(OBLIGATION_PARAM_INTENT_KIND) == intent_kind
        ):
            return True
    return False

ALL_CHANNELS = _literal_values(Channel)
ALL_RECIPIENT_ROLES = _literal_values(RecipientRole)
ALL_INTENT_KINDS = _literal_values(IntentKind)
ALL_CANCEL_REASONS = _literal_values(CancelReason)
ALL_RECIPIENT_STATUSES = _literal_values(RecipientStatus)

#: Recipient roles allowed per intent kind (fail closed: anything else is
#: rejected at enqueue time).
INTENT_KIND_RECIPIENT_ROLES: dict[IntentKind, frozenset[RecipientRole]] = {
    "crisis_safety": frozenset({"guardian"}),       # minor guardian
    "emergency": frozenset({"emergency_contact"}),  # adult emergency contact
    "care_alert": frozenset({"delegate"}),          # senior delegate
}

#: Canonical template per intent kind (fail closed on mismatch).
INTENT_KIND_TEMPLATES: dict[IntentKind, TemplateKey] = {
    "crisis_safety": "crisis_safety_notice",
    "emergency": "emergency_notice",
    "care_alert": "care_alert_notice",
}

#: Default ordered channels per recipient role (primary first).  Callers may
#: override per recipient.  Channel names are logical ids only: this package
#: ships no credentials and never fabricates a delivery.
DEFAULT_CHANNEL_ORDER: dict[RecipientRole, tuple[Channel, ...]] = {
    "guardian": ("wechat_subscription", "sms"),
    "emergency_contact": ("phone_call", "sms"),
    "delegate": ("wechat_subscription", "phone_call"),
}

#: Canonical relationship statuses (ADR-0033, section 5.2) - only ``active``
#: relationships may carry delivery; every other status fails closed.
RELATIONSHIP_ACTIVE = RelationshipStatus.RELATIONSHIP_STATUS_ACTIVE.value
INACTIVE_RELATIONSHIP_STATUSES: frozenset[str] = frozenset(
    status.value
    for status in RelationshipStatus
    if status
    not in (
        RelationshipStatus.RELATIONSHIP_STATUS_ACTIVE,
        RelationshipStatus.RELATIONSHIP_STATUS_PENDING,
        RelationshipStatus.RELATIONSHIP_STATUS_SUSPENDED,
    )
)
CANCEL_REASON_BY_RELATIONSHIP_STATUS: dict[str, CancelReason] = {
    RelationshipStatus.RELATIONSHIP_STATUS_REVOKED.value: "relationship_revoked",
    RelationshipStatus.RELATIONSHIP_STATUS_EXPIRED.value: "relationship_expired",
    RelationshipStatus.RELATIONSHIP_STATUS_DISPUTED.value: "relationship_disputed",
}

#: Terminal recipient statuses: no further delivery attempts.
RECIPIENT_TERMINAL = frozenset({"delivered", "dead_lettered", "cancelled"})

# ---------------------------------------------------------------------------
# Content minimization (section 6.5 / 10.6)
# ---------------------------------------------------------------------------

#: The only fields a template may consume.  Any other key is rejected.
TEMPLATE_FIELDS: dict[TemplateKey, frozenset[str]] = {
    "crisis_safety_notice": frozenset(
        {"role_label", "reason_code", "occurred_at", "script_version", "action_hint"}
    ),
    "emergency_notice": frozenset(
        {"role_label", "reason_code", "occurred_at", "script_version", "action_hint"}
    ),
    "care_alert_notice": frozenset(
        {"role_label", "reason_code", "occurred_at", "script_version", "action_hint"}
    ),
}

#: Keys that must never appear anywhere in notification content, even if a
#: template field list accidentally grows (defense in depth).
FORBIDDEN_CONTENT_KEYS = frozenset(
    {
        "transcript",
        "diagnosis",
        "severity",
        "emotional_label",
        "quoted_memory",
        "raw_name",
        "user_text",
        "assistant_text",
        "session_text",
        "private_memory",
        "persona_text",
        "permission_flags",
    }
)

ALLOWED_ROLE_LABELS = frozenset({"孩子", "家人", "父母", "老人"})
ALLOWED_ACTION_HINTS = frozenset(
    {"联系监护人", "联系紧急联系人", "陪伴安抚并关注变化", "拨打紧急电话"}
)
SCRIPT_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_TEMPLATE_TEXTS: dict[TemplateKey, str] = {
    "crisis_safety_notice": (
        "【Memoria 安全提醒】您的{role_label}可能需要关心。"
        "原因代码：{reason_code}。发生时间：{occurred_at}。"
        "话术版本：{script_version}。建议：{action_hint}。"
    ),
    "emergency_notice": (
        "【Memoria 紧急提醒】您的{role_label}可能有紧急情况。"
        "原因代码：{reason_code}。发生时间：{occurred_at}。"
        "话术版本：{script_version}。建议：{action_hint}。"
    ),
    "care_alert_notice": (
        "【Memoria 关怀提醒】您照顾的{role_label}需要关注。"
        "原因代码：{reason_code}。发生时间：{occurred_at}。"
        "话术版本：{script_version}。建议：{action_hint}。"
    ),
}


class ContentPolicyError(ValueError):
    """Notification content violates the minimization policy."""


def render_notification_content(
    template_key: TemplateKey,
    template_params: dict[str, str],
) -> str:
    """Render the frozen, minimal notification text for ``template_key``.

    Fails closed on: unknown template keys, unknown or forbidden params,
    missing required params and out-of-vocabulary values.  The output is
    deterministic and never contains raw conversation or diagnostic data.
    """
    allowed = TEMPLATE_FIELDS.get(template_key)
    if allowed is None:
        raise ContentPolicyError(f"unknown template key: {template_key}")
    unknown = set(template_params) - allowed
    if unknown:
        raise ContentPolicyError(
            f"template {template_key} does not allow params: {sorted(unknown)}"
        )
    forbidden = set(template_params) & FORBIDDEN_CONTENT_KEYS
    if forbidden:
        raise ContentPolicyError(
            f"forbidden content keys must never reach notifications: {sorted(forbidden)}"
        )
    missing = allowed - set(template_params)
    if missing:
        raise ContentPolicyError(
            f"template {template_key} requires params: {sorted(missing)}"
        )
    reason_code = template_params["reason_code"]
    if reason_code not in _literal_values(ReasonCode):
        raise ContentPolicyError(f"unknown reason code: {reason_code}")
    role_label = template_params["role_label"]
    if role_label not in ALLOWED_ROLE_LABELS:
        raise ContentPolicyError(f"role label not in vocabulary: {role_label!r}")
    action_hint = template_params["action_hint"]
    if action_hint not in ALLOWED_ACTION_HINTS:
        raise ContentPolicyError(f"action hint not in vocabulary: {action_hint!r}")
    script_version = template_params["script_version"]
    if SCRIPT_VERSION_PATTERN.fullmatch(script_version) is None:
        raise ContentPolicyError(f"invalid script version: {script_version!r}")
    occurred_at = template_params["occurred_at"]
    try:
        datetime.fromisoformat(occurred_at)
    except ValueError as exc:
        raise ContentPolicyError(f"occurred_at is not ISO-8601: {occurred_at!r}") from exc
    return _TEMPLATE_TEXTS[template_key].format(**template_params)


def validate_template_params(
    template_key: TemplateKey,
    template_params: dict[str, str],
) -> None:
    """Validate params without rendering (enqueue-time gate)."""
    render_notification_content(template_key, template_params)


# ---------------------------------------------------------------------------
# Delivery failure classification
# ---------------------------------------------------------------------------

RETRYABLE_ERROR_CODES = frozenset({"channel_unavailable", "timeout", "rate_limited"})
#: Permanent failures dead-letter immediately (a retry cannot help).
PERMANENT_ERROR_CODES = frozenset(
    {"channel_invalid", "recipient_contact_unavailable", "policy_denied"}
)


def classify_failure(error_code: str) -> str:
    """Return ``retryable``, ``permanent`` or ``unknown``.

    Unknown codes are treated as permanent so an unreviewed failure lands in
    the dead letter for human inspection instead of burning retries.
    """
    if error_code in RETRYABLE_ERROR_CODES:
        return "retryable"
    if error_code in PERMANENT_ERROR_CODES:
        return "permanent"
    return "unknown"


# ---------------------------------------------------------------------------
# Backoff policy (injectable, deterministic with a seeded RNG)
# ---------------------------------------------------------------------------


class BackoffPolicy:
    """Exponential backoff with jitter for delivery retries."""

    def __init__(
        self,
        *,
        base_seconds: float = 30.0,
        factor: float = 2.0,
        max_seconds: float = 3600.0,
        jitter_ratio: float = 0.2,
        rng: random.Random | None = None,
    ) -> None:
        if base_seconds <= 0 or factor <= 1 or max_seconds < base_seconds:
            raise ValueError("invalid backoff parameters")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be within [0, 1]")
        self._base_seconds = base_seconds
        self._factor = factor
        self._max_seconds = max_seconds
        self._jitter_ratio = jitter_ratio
        self._rng = rng if rng is not None else random.Random()

    def delay_seconds(self, attempts_completed: int) -> float:
        """Delay after ``attempts_completed`` failed attempts (>= 1)."""
        if attempts_completed < 1:
            raise ValueError("attempts_completed must be >= 1")
        raw = min(
            self._base_seconds * self._factor ** max(0, attempts_completed - 1),
            self._max_seconds,
        )
        jitter = raw * self._rng.uniform(0.0, self._jitter_ratio)
        return raw + jitter

    def next_attempt_at(self, attempts_completed: int, now: datetime) -> datetime:
        return now + timedelta(seconds=self.delay_seconds(attempts_completed))


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NotificationIntent:
    intent_id: str
    idempotency_key: str
    intent_kind: IntentKind
    subject_person_id: str
    source_event_id: str
    policy_receipt_id: str
    #: Immutable fence snapshot the intent was issued under (P0-4 / third
    #: review): audit, replay and cancellation can prove the epoch / binding
    #: / profile / context hash the policy decision was made for.
    session_id: str
    epoch: int
    binding_id: str
    binding_version: int
    runtime_profile_id: str
    actor_person_id: str
    fence_context_hash: str
    template_key: TemplateKey
    template_params: dict[str, str]
    reason_code: ReasonCode
    script_version: str
    occurred_at: datetime
    #: Persisted fence authorization window (P0 / Contract): REQUIRED and
    #: timezone-aware; the worker reconstructs the exact fence (including
    #: this window) before an external send.
    valid_until: datetime
    status: IntentStatus
    created_at: datetime
    updated_at: datetime
    cancelled_reason: CancelReason | None = None
    cancelled_at: datetime | None = None
    delivered_at: datetime | None = None
    #: Full fence snapshot persisted so the worker can re-verify the policy
    #: receipt (and its exact evidence fence) right before an external send
    #: (TOCTOU, red team 12): device + subject revision are part of the
    #: PolicyReceiptV2 identity contract.
    device_id: str = ""
    subject_revision: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "idempotency_key": self.idempotency_key,
            "intent_kind": self.intent_kind,
            "subject_person_id": self.subject_person_id,
            "source_event_id": self.source_event_id,
            "policy_receipt_id": self.policy_receipt_id,
            "session_id": self.session_id,
            "epoch": self.epoch,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "runtime_profile_id": self.runtime_profile_id,
            "actor_person_id": self.actor_person_id,
            "fence_context_hash": self.fence_context_hash,
            "device_id": self.device_id,
            "subject_revision": self.subject_revision,
            "valid_until": (
                self.valid_until.isoformat() if self.valid_until else None
            ),
            "template_key": self.template_key,
            "template_params": dict(self.template_params),
            "reason_code": self.reason_code,
            "script_version": self.script_version,
            "occurred_at": self.occurred_at.isoformat(),
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "cancelled_reason": self.cancelled_reason,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
        }


@dataclass(frozen=True, slots=True)
class RecipientBinding:
    recipient_id: str
    intent_id: str
    person_id: str
    role: RecipientRole
    relationship_id: str
    relationship_status: RelationshipStatusValue
    channels: tuple[Channel, ...]
    channel_index: int
    status: RecipientStatus
    attempts: int
    max_retries: int
    next_attempt_at: datetime | None
    leased_until: datetime | None
    fencing_token: str | None
    last_error_code: str | None
    valid_from: datetime
    valid_until: datetime | None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None = None
    delivered_channel: Channel | None = None
    cancelled_reason: CancelReason | None = None
    cancelled_at: datetime | None = None
    #: Authoritative evidence of the relationship snapshot the recipient
    #: was resolved from (P0 TOCTOU): the worker re-verifies these against
    #: the authoritative resolver immediately before an external send.
    relationship_snapshot_id: str = ""
    relationship_revision: int = 1

    def current_channel(self) -> Channel:
        return self.channels[min(self.channel_index, len(self.channels) - 1)]

    def is_due(self, now: datetime) -> bool:
        """Whether a delivery attempt may be claimed right now."""
        if self.status in RECIPIENT_TERMINAL:
            return False
        if self.relationship_status != RELATIONSHIP_ACTIVE:
            return False
        if self.valid_until is not None and self.valid_until <= now:
            return False
        if now < self.valid_from:
            return False
        if self.next_attempt_at is not None and self.next_attempt_at > now:
            return False
        if self.status in ("pending", "failed"):
            return True
        if self.status == "in_progress":
            return self.leased_until is not None and self.leased_until <= now
        return False

    def to_dict(self) -> dict[str, object]:
        return {
            "recipient_id": self.recipient_id,
            "intent_id": self.intent_id,
            "person_id": self.person_id,
            "role": self.role,
            "relationship_id": self.relationship_id,
            "relationship_status": self.relationship_status,
            "relationship_snapshot_id": self.relationship_snapshot_id,
            "relationship_revision": self.relationship_revision,
            "channels": list(self.channels),
            "channel_index": self.channel_index,
            "status": self.status,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "next_attempt_at": (
                self.next_attempt_at.isoformat() if self.next_attempt_at else None
            ),
            "leased_until": self.leased_until.isoformat() if self.leased_until else None,
            "fencing_token": self.fencing_token,
            "last_error_code": self.last_error_code,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "delivered_channel": self.delivered_channel,
            "cancelled_reason": self.cancelled_reason,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
        }


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    attempt_id: str
    intent_id: str
    recipient_id: str
    attempt_number: int
    channel: Channel
    #: STABLE logical delivery identity across retries
    #: ``{intent_id}:{recipient_id}:{channel}`` - NEVER contains the
    #: attempt_number or a fencing token.  Every retry of the same
    #: recipient+channel reuses this key, so a late worker (attempt N)
    #: and a re-claiming worker (attempt N+1) deduplicate on the SAME
    #: provider-side key and cannot double-deliver.
    logical_delivery_key: str
    status: AttemptStatus
    fencing_token: str
    leased_until: datetime
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "intent_id": self.intent_id,
            "recipient_id": self.recipient_id,
            "attempt_number": self.attempt_number,
            "channel": self.channel,
            "logical_delivery_key": self.logical_delivery_key,
            "status": self.status,
            "fencing_token": self.fencing_token,
            "leased_until": self.leased_until.isoformat(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Channel-returned delivery proof; never fabricated by this package."""

    receipt_id: str
    intent_id: str
    recipient_id: str
    attempt_id: str
    channel: Channel
    channel_receipt_id: str
    delivered_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "intent_id": self.intent_id,
            "recipient_id": self.recipient_id,
            "attempt_id": self.attempt_id,
            "channel": self.channel,
            "channel_receipt_id": self.channel_receipt_id,
            "delivered_at": self.delivered_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class RecipientSpec:
    """Enqueue-time recipient request (third review): the caller may only
    request a relationship and channel preferences.  Identity fields
    (person/role/status/validity) come exclusively from the authoritative
    relationship snapshot resolved by the service."""

    relationship_id: str
    channels: tuple[Channel, ...] | None = None


# ---------------------------------------------------------------------------
# State machine rules
# ---------------------------------------------------------------------------


def derive_intent_status(recipients: tuple[RecipientBinding, ...]) -> IntentStatus:
    """Deterministic aggregate status from the recipient set."""
    statuses = {recipient.status for recipient in recipients}
    if not statuses:
        return "pending"
    if statuses == {"cancelled"}:
        return "cancelled"
    if statuses <= {"delivered", "cancelled", "dead_lettered"}:
        if "delivered" in statuses:
            return "delivered"
        return "dead_lettered"
    if statuses == {"pending"}:
        return "pending"
    return "in_progress"


def cancelled_recipient(
    recipient: RecipientBinding,
    *,
    reason: CancelReason,
    now: datetime,
) -> RecipientBinding:
    """Terminal cancel: audit reason recorded, no further delivery allowed."""
    return replace_recipient(
        recipient,
        status="cancelled",
        cancelled_reason=reason,
        cancelled_at=now,
        fencing_token=None,
        leased_until=None,
        next_attempt_at=None,
        updated_at=now,
    )


def replace_recipient(
    recipient: RecipientBinding,
    **changes: object,
) -> RecipientBinding:
    """Type-safe ``dataclasses.replace`` for ``RecipientBinding``."""
    return replace(recipient, **changes)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NotificationError(RuntimeError):
    """Base error for the notification domain."""


class NotificationNotFoundError(NotificationError):
    """Intent / recipient / attempt does not exist."""


class NotificationConflictError(NotificationError):
    """Idempotency key reused with different content."""


class NotificationPolicyError(NotificationError):
    """Intent kind / recipient role / relationship combination rejected."""


class RelationshipInactiveError(NotificationPolicyError):
    """Recipient relationship is not active; delivery fails closed."""


class NotificationFencingError(NotificationError):
    """A stale worker tried to complete an attempt it no longer owns."""


class NotificationStateError(NotificationError):
    """Invalid state transition (for example replay of a cancelled recipient)."""


class ReceiptNotVerifiedError(NotificationPolicyError):
    """The policy receipt could not be verified against the authoritative
    receipt store (missing / mismatch / expired / wrong capability)."""


class RelationshipNotVerifiedError(NotificationPolicyError):
    """The recipient's relationship could not be verified against the
    authoritative relationship/binding service."""


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "ALL_CANCEL_REASONS",
    "ALL_CHANNELS",
    "ALL_INTENT_KINDS",
    "ALL_RECIPIENT_ROLES",
    "ALL_RECIPIENT_STATUSES",
    "ALLOWED_ACTION_HINTS",
    "ALLOWED_ROLE_LABELS",
    "AttemptStatus",
    "BackoffPolicy",
    "CANCEL_REASON_BY_RELATIONSHIP_STATUS",
    "CancelReason",
    "Channel",
    "ContentPolicyError",
    "DEFAULT_CHANNEL_ORDER",
    "DeliveryAttempt",
    "DeliveryReceipt",
    "FORBIDDEN_CONTENT_KEYS",
    "INACTIVE_RELATIONSHIP_STATUSES",
    "INTENT_KIND_RECIPIENT_ROLES",
    "INTENT_KIND_TEMPLATES",
    "IntentKind",
    "IntentStatus",
    "NotificationConflictError",
    "NotificationError",
    "NotificationFence",
    "NotificationFencingError",
    "NotificationIntent",
    "NotificationNotFoundError",
    "NotificationPolicyError",
    "NotificationStateError",
    "PERMANENT_ERROR_CODES",
    "RECIPIENT_TERMINAL",
    "RELATIONSHIP_ACTIVE",
    "RETRYABLE_ERROR_CODES",
    "RecipientBinding",
    "RecipientRole",
    "RecipientSpec",
    "RecipientStatus",
    "ReceiptNotVerifiedError",
    "ReasonCode",
    "RelationshipNotVerifiedError",
    "RelationshipInactiveError",
    "RelationshipStatusValue",
    "RelationshipSnapshot",
    "SCRIPT_VERSION_PATTERN",
    "TEMPLATE_FIELDS",
    "TemplateKey",
    "cancelled_recipient",
    "classify_failure",
    "derive_intent_status",
    "new_id",
    "render_notification_content",
    "replace_recipient",
    "utcnow",
    "validate_template_params",
    "verify_notification_receipt",
]
