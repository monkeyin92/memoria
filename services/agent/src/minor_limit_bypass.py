"""A startup-only, per-subject bypass for a test minor's time limits.

The whitelist is read once from ``MINOR_LIMIT_BYPASS_TEST_SUBJECTS`` when the
media registry starts. It never touches storage or global policy: admission
still accrues usage, and only the two signed limit checks consult it.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

MINOR_LIMIT_BYPASS_ENV: Final = "MINOR_LIMIT_BYPASS_TEST_SUBJECTS"
MINOR_DAILY_LIMIT_BYPASSED_REASON: Final = "minor_daily_limit_bypassed_test"
MINOR_SESSION_LIMIT_BYPASSED_REASON: Final = "minor_session_limit_bypassed_test"

logger = logging.getLogger(__name__)


def subject_id_hash(subject_id: str) -> str:
    """A log-safe tag for a subject ID: the first eight hex digits of its SHA-256."""

    return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class MinorLimitBypass:
    """At most one test subject whose minor limits are waived. Empty is off."""

    subject_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if len(self.subject_ids) > 1:
            logger.warning(
                "minor limit bypass disabled at=%s reason=multiple_test_subjects configured=%d",
                _now(),
                len(self.subject_ids),
            )
            object.__setattr__(self, "subject_ids", frozenset())

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> MinorLimitBypass:
        raw = (os.environ if environ is None else environ).get(MINOR_LIMIT_BYPASS_ENV, "")
        bypass = cls(
            frozenset(part.strip() for part in raw.split(",") if part.strip()),
        )
        if bypass.enabled:
            logger.warning(
                "minor limit bypass active at=%s subject_sha256_8=%s",
                _now(),
                ",".join(subject_id_hash(subject_id) for subject_id in sorted(bypass.subject_ids)),
            )
        return bypass

    @property
    def enabled(self) -> bool:
        return len(self.subject_ids) == 1

    def allows(self, subject_id: str | None, *, reason: str) -> bool:
        if not self.enabled or subject_id is None or subject_id not in self.subject_ids:
            return False
        logger.warning(
            "minor limit bypassed at=%s reason=%s subject_sha256_8=%s",
            _now(),
            reason,
            subject_id_hash(subject_id),
        )
        return True


def configured_minor_limit_bypass(owner: object) -> MinorLimitBypass:
    """The registry's startup config, disabled for test doubles without the field."""

    configured = getattr(owner, "minor_limit_bypass", None)
    return configured if isinstance(configured, MinorLimitBypass) else MinorLimitBypass()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
