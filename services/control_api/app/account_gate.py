"""Account lifecycle and subject-capability gates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from threading import Condition
from types import MappingProxyType
from typing import Annotated, Any, Literal, Protocol, cast

from fastapi import Depends, HTTPException, Request

from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.evolution.account_fence import AccountWriteBlockedError

SubjectCapability = Literal[
    "companion_chat",
    "memory_ledger",
    "emotion_expression",
    "tutor",
    "voice_clone",
    "digital_self",
    "self_preview",
    "legacy_grant",
    "legacy_receive",
    "account_evolution",
    "speaker_enrollment",
    "raw_voice_archive",
    "guardian_manage",
    "guardian_weekly_report",
    "device_settings_read",
    "device_settings_manage",
    "device_runtime_profile_sync",
    "conversation_review",
]

SUBJECT_CAPABILITY_RULES = MappingProxyType(
    {
        "companion_chat": frozenset({"unknown", "adult", "minor"}),
        "memory_ledger": frozenset({"adult", "minor"}),
        "emotion_expression": frozenset({"unknown", "adult", "minor"}),
        "tutor": frozenset({"unknown", "adult", "minor"}),
        "voice_clone": frozenset({"adult"}),
        "digital_self": frozenset({"adult"}),
        "self_preview": frozenset({"adult"}),
        "legacy_grant": frozenset({"adult"}),
        "legacy_receive": frozenset({"adult"}),
        "account_evolution": frozenset({"adult"}),
        "speaker_enrollment": frozenset({"adult"}),
        "raw_voice_archive": frozenset({"adult"}),
        "guardian_manage": frozenset({"adult"}),
        "guardian_weekly_report": frozenset({"minor"}),
        "device_settings_read": frozenset({"adult", "minor"}),
        "device_settings_manage": frozenset({"adult"}),
        # Unknown subjects still need the signed unknown-safe runtime profile
        # so the hardware can fail closed. This does not grant private memory
        # or adult-only settings capabilities.
        "device_runtime_profile_sync": frozenset({"unknown", "adult", "minor"}),
        "conversation_review": frozenset({"adult", "minor"}),
    }
)


class SubjectProfileStore(Protocol):
    def get_subject_profile(self, *, user_id: str) -> dict[str, Any] | None: ...


def require_capability_for_subject_category(
    subject_category: object,
    capability: SubjectCapability,
) -> None:
    """Enforce one capability matrix for any authoritative category source.

    Most legacy routes resolve the category from ``MemoryStore``. Newer
    multi-subject routes resolve it from Identity/PostgreSQL. Both paths must
    share this exact decision point instead of growing parallel rule tables.
    """

    allowed_categories = SUBJECT_CAPABILITY_RULES.get(capability)
    if allowed_categories is None:
        raise HTTPException(
            status_code=403,
            detail={"code": "subject_capability_unconfigured", "capability": capability},
        )
    if subject_category is None:
        raise HTTPException(
            status_code=403,
            detail={"code": "subject_category_unavailable", "capability": capability},
        )
    if subject_category not in allowed_categories:
        code = "minor_forbidden" if subject_category == "minor" else "subject_capability_forbidden"
        raise HTTPException(
            status_code=403,
            detail={"code": code, "capability": capability},
        )


def require_capability_for_account_id(
    account_id: str,
    capability: SubjectCapability,
    *,
    store: SubjectProfileStore,
) -> dict[str, Any]:
    """Resolve the authoritative profile and enforce the single capability matrix."""

    try:
        profile = store.get_subject_profile(user_id=account_id)
    except Exception as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "subject_category_unavailable", "capability": capability},
        ) from exc
    if profile is None:
        raise HTTPException(
            status_code=403,
            detail={"code": "subject_category_unavailable", "capability": capability},
        )
    require_capability_for_subject_category(
        profile.get("subject_category"),
        capability,
    )
    if capability == "speaker_enrollment":
        # A speaker profile is biometric identity authority.  Category alone
        # is not sufficient: historical/default adult rows and accounts whose
        # age evidence is still pending must remain fail-closed until the
        # verified subject profile is committed by the trusted identity path.
        if (
            profile.get("subject_category") != "adult"
            or profile.get("birth_year_band") != "adult"
            or profile.get("age_evidence_status") != "verified"
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "subject_capability_forbidden", "capability": capability},
            )
    return profile


def require_capability_for_subject(
    user: AuthenticatedUser,
    capability: SubjectCapability,
    *,
    store: SubjectProfileStore,
) -> AuthenticatedUser:
    require_capability_for_account_id(user.user_id, capability, store=store)
    return user


class AccountDeletingError(AccountWriteBlockedError):
    pass


class AccountOperationGate:
    def __init__(self) -> None:
        # The deletion worker and evolution sleep worker may run in different
        # threads.  A threading condition keeps the lease valid across both
        # async request tasks and synchronous worker calls.
        self._condition = Condition()
        self._blocked: set[str] = set()
        self._active_writes: dict[str, int] = {}
        self._active_reads: dict[str, int] = {}

    @contextmanager
    def sync_write(self, account_id: str) -> Iterator[None]:
        """Acquire an in-process write lease for a sync worker."""

        with self._condition:
            if account_id in self._blocked:
                raise AccountDeletingError("account deletion is in progress")
            self._active_writes[account_id] = self._active_writes.get(account_id, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                remaining = self._active_writes.get(account_id, 1) - 1
                if remaining > 0:
                    self._active_writes[account_id] = remaining
                else:
                    self._active_writes.pop(account_id, None)
                self._condition.notify_all()

    @asynccontextmanager
    async def write(self, account_id: str) -> AsyncIterator[None]:
        with self.sync_write(account_id):
            yield

    @contextmanager
    def sync_read(self, account_id: str) -> Iterator[None]:
        """Acquire a short read lease that deletion waits to drain.

        This is intentionally opt-in for privacy-sensitive projections such
        as evolution resolution; ordinary account reads keep their existing
        behavior. A durable tombstone remains the cross-process backstop.
        """

        with self._condition:
            if account_id in self._blocked:
                raise AccountDeletingError("account deletion is in progress")
            self._active_reads[account_id] = self._active_reads.get(account_id, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                remaining = self._active_reads.get(account_id, 1) - 1
                if remaining > 0:
                    self._active_reads[account_id] = remaining
                else:
                    self._active_reads.pop(account_id, None)
                self._condition.notify_all()

    @asynccontextmanager
    async def read(self, account_id: str) -> AsyncIterator[None]:
        with self.sync_read(account_id):
            yield

    async def block_account(self, account_id: str) -> None:
        await asyncio.to_thread(self._block_account, account_id)

    def _block_account(self, account_id: str) -> None:
        with self._condition:
            self._blocked.add(account_id)
            while (
                self._active_writes.get(account_id, 0) > 0
                or self._active_reads.get(account_id, 0) > 0
            ):
                self._condition.wait()


async def require_writable_account(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> AsyncIterator[AuthenticatedUser]:
    gate = cast(AccountOperationGate, request.app.state.account_operations)
    try:
        async with gate.write(user.user_id):
            yield user
    except AccountDeletingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
