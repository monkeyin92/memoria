"""Shared account-write fence contracts.

The in-process gate and the durable evolution-store tombstone deliberately
share one error type.  Callers can therefore treat a deletion race as a
discardable write without importing the control API implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager


class AccountWriteBlockedError(RuntimeError):
    """Owner-private writes are rejected after account deletion begins."""


class AccountSubjectBlockedError(AccountWriteBlockedError):
    """Account-scoped evolution is forbidden by the account subject category."""


AccountWriteGuard = Callable[[str], AbstractContextManager[None]]
AccountReadGuard = Callable[[str], AbstractContextManager[None]]
AccountSubjectGuard = Callable[[str], None]
SubjectCategoryResolver = Callable[[str], str | None]


def require_account_evolution_subject(
    account_id: str,
    *,
    resolve_subject_category: SubjectCategoryResolver,
) -> None:
    """Evolution-side backstop for the Control API capability matrix.

    The resolver must read the authoritative account profile.  Missing,
    malformed, and minor categories all fail closed; only the explicit adult
    value can create or mutate owner-private learning state.
    """

    if not account_id.strip():
        raise AccountSubjectBlockedError("account subject category is unavailable")
    try:
        category = resolve_subject_category(account_id)
    except Exception as exc:
        raise AccountSubjectBlockedError("account subject category is unavailable") from exc
    if category != "adult":
        raise AccountSubjectBlockedError("minor accounts cannot use account-scoped evolution")


__all__ = [
    "AccountReadGuard",
    "AccountSubjectGuard",
    "AccountSubjectBlockedError",
    "AccountWriteBlockedError",
    "AccountWriteGuard",
    "SubjectCategoryResolver",
    "require_account_evolution_subject",
]
