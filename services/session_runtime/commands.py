"""Command and result types crossing the Session Runtime application boundary.

Control and the device media path construct these frozen value types, and the
service validates them at construction and never mutates them.  The module is
free of PostgreSQL and of the orchestration object, so a caller can build a
command before any service exists.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from packages.contracts.generated.python.multi_subject_contracts import (
    CapabilityValue,
    PolicyReceiptV2,
    RuntimeProfileSignedV2,
)

from services.session_runtime.subject_resolver import SubjectCandidate
from services.session_runtime.wire import _canonical_json

_MAX_PROFILE_TTL = timedelta(seconds=86_400)


@dataclass(frozen=True, slots=True)
class StartPersistentSessionCommand:
    session_id: str
    actor_id: str
    device_id: str
    expected_binding_version: int
    idempotency_key: str
    now: datetime
    requested_capabilities: tuple[CapabilityValue, ...] = ("chat",)
    candidates: tuple[SubjectCandidate, ...] = ()
    # The app/control path confirms the binding's sole subject (one-to-one
    # bindings only; see ``sole_bound_subject_id``).  Never set by the
    # device media path, which names its subject explicitly.
    confirm_sole_bound_subject: bool = False
    # Only the device's own authenticated media session sets this: it names
    # the binding primary subject the device serves (see SubjectResolver).
    device_bound_subject_id: str | None = None
    multiple_speakers: bool = False
    offline: bool = False
    # Per-session profile lifetime override. Only the direct hardware media
    # path sets this (device_runtime_profile_ttl_s); every other caller keeps
    # the service default so H5 and mini-program profiles are never widened.
    profile_ttl: timedelta | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.session_id, "session_id"),
            (self.actor_id, "actor_id"),
            (self.device_id, "device_id"),
            (self.idempotency_key, "idempotency_key"),
        ):
            if not value.strip():
                raise ValueError(f"{field} must not be empty")
        if self.expected_binding_version < 1:
            raise ValueError("expected_binding_version must be positive")
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not self.requested_capabilities:
            raise ValueError("requested_capabilities must not be empty")
        if len(set(self.requested_capabilities)) != len(self.requested_capabilities):
            raise ValueError("requested_capabilities must be unique")
        if self.profile_ttl is not None:
            if not isinstance(self.profile_ttl, timedelta):
                raise ValueError("profile_ttl must be a timedelta")
            if self.profile_ttl <= timedelta(0):
                raise ValueError("profile_ttl must be positive")
            if self.profile_ttl > _MAX_PROFILE_TTL:
                raise ValueError("profile_ttl exceeds the one-day bound")


@dataclass(frozen=True, slots=True)
class CommitToolEffectCommand:
    """One Agent-prepared effect submitted to the Session authority."""

    actor_id: str
    session_id: str
    runtime_profile: RuntimeProfileSignedV2
    policy_receipt: PolicyReceiptV2
    session_epoch: int
    generation_id: int
    turn_id: int
    tool_epoch: int
    capability: str
    purpose: str
    resource_id: str
    evidence_refs: tuple[str, ...]
    idempotency_key: str
    intent: str
    payload: dict[str, object]
    payload_sha256: str
    fence_fingerprint: str
    now: datetime

    def __post_init__(self) -> None:
        for value, name, maximum in (
            (self.actor_id, "actor_id", 128),
            (self.session_id, "session_id", 128),
            (self.capability, "capability", 128),
            (self.purpose, "purpose", 128),
            (self.resource_id, "resource_id", 128),
            (self.idempotency_key, "idempotency_key", 512),
            (self.intent, "intent", 512),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{name} must be a bounded non-blank string")
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        for numeric_value, name, minimum in (
            (self.session_epoch, "session_epoch", 1),
            (self.generation_id, "generation_id", 0),
            (self.turn_id, "turn_id", 0),
            (self.tool_epoch, "tool_epoch", 0),
        ):
            if type(numeric_value) is not int or numeric_value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if not self.evidence_refs or any(
            not isinstance(item, str) or not item.strip() or len(item) > 192
            for item in self.evidence_refs
        ):
            raise ValueError("evidence_refs must contain bounded ids")
        if not re.fullmatch(r"^[a-f0-9]{64}$", self.payload_sha256):
            raise ValueError("payload_sha256 must be a sha256 digest")
        if not re.fullmatch(r"^[a-f0-9]{64}$", self.fence_fingerprint):
            raise ValueError("fence_fingerprint must be a sha256 digest")
        encoded = _canonical_json(self.payload, allow_nan=False).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != self.payload_sha256:
            raise ValueError("payload_sha256 does not match the canonical payload")


@dataclass(frozen=True, slots=True)
class SwitchPersistentSubjectCommand:
    session_id: str
    actor_id: str
    subject_id: str | None
    now: datetime
    requested_capabilities: tuple[CapabilityValue, ...]
    claimed_subject_id: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.actor_id.strip():
            raise ValueError("session_id and actor_id must not be empty")
        if self.subject_id is not None and not self.subject_id.strip():
            raise ValueError("subject_id must not be empty")
        if self.claimed_subject_id is not None and not self.claimed_subject_id.strip():
            raise ValueError("claimed_subject_id must not be empty")
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not self.requested_capabilities:
            raise ValueError("requested_capabilities must not be empty")


@dataclass(frozen=True, slots=True)
class SessionCloseResult:
    """Outcome of a terminal session_closed authority transition."""

    applied: bool
    already_closed: bool
