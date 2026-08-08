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


AccountWriteGuard = Callable[[str], AbstractContextManager[None]]
AccountReadGuard = Callable[[str], AbstractContextManager[None]]


__all__ = ["AccountReadGuard", "AccountWriteBlockedError", "AccountWriteGuard"]
