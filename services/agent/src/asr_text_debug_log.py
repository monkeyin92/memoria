"""A test-period only INFO line carrying the ASR transcript for whitelisted sessions.

FunASR sometimes returns an empty final for a one-character 「停」, and the
playback-stop path then logs only lengths.  Reading the recognized text is the
only way to tell a mishearing from a dropped final, so the line exists while
testing and stays off in production.

Every line starts with ``asr_text_debug`` so one grep finds them.  They are
plain log records: nothing here reaches evidence, the spool, the archive or
#282's metadata-only turn.  Only a session listed in
``ASR_TEXT_DEBUG_LOG_SUBJECTS``, or every session while
``MEMORIA_ASR_TEXT_TRACE`` is set, has its text logged.  Both are off by
default.

The subject list is parsed and matched like
``MINOR_LIMIT_BYPASS_TEST_SUBJECTS`` in ``minor_limit_bypass``, except that any
number of subjects may be listed (there is no single-owner invariant here).
``MEMORIA_ASR_TEXT_TRACE`` is parsed like ``MEMORIA_UPLINK_PCM_TRACE``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from services.agent.src.minor_limit_bypass import subject_id_hash
from services.common.redaction import redact_pii

ASR_TEXT_DEBUG_LOG_ENV: Final = "ASR_TEXT_DEBUG_LOG_SUBJECTS"
ASR_TEXT_TRACE_ENV: Final = "MEMORIA_ASR_TEXT_TRACE"
ASR_TEXT_DEBUG_LOG_PREFIX: Final = "asr_text_debug"

logger = logging.getLogger(__name__)


def asr_text_trace_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """The global probe, parsed like ``MEMORIA_UPLINK_PCM_TRACE``."""

    raw = (os.environ if environ is None else environ).get(ASR_TEXT_TRACE_ENV, "")
    return raw.strip().lower() not in {"", "0", "false", "off"}


@dataclass(frozen=True, slots=True)
class AsrTextDebugSubjects:
    """The subjects whose ASR text may be logged. Empty is off."""

    subject_ids: frozenset[str] = frozenset()
    all_subjects: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AsrTextDebugSubjects:
        env = os.environ if environ is None else environ
        raw = env.get(ASR_TEXT_DEBUG_LOG_ENV, "")
        subjects = cls(
            frozenset(part.strip() for part in raw.split(",") if part.strip()),
            asr_text_trace_enabled(env),
        )
        if subjects.enabled:
            logger.warning(
                "asr text debug log active at=%s configured=%d trace=%s",
                _now(),
                len(subjects.subject_ids),
                subjects.all_subjects,
            )
        return subjects

    def for_session(self, *subject_ids: object) -> AsrTextDebugSubjects:
        """This switch narrowed to the ids the session carries.

        A provider session has no media session around it, so the ids are
        decided once here instead of looked up again per final.
        """

        if self.all_subjects:
            return self
        listed = frozenset(
            candidate
            for candidate in (subject_id(value) for value in subject_ids)
            if candidate and self.logs(candidate)
        )
        return AsrTextDebugSubjects(listed)

    @property
    def enabled(self) -> bool:
        return self.all_subjects or bool(self.subject_ids)

    def logs(self, *subject_ids: str | None) -> bool:
        """True when the probe is global, or any of the session's ids is whitelisted."""

        if self.all_subjects:
            return True
        return any(
            subject_id is not None and subject_id in self.subject_ids for subject_id in subject_ids
        )


def subject_id(candidate: object) -> str:
    """A stripped subject / device id, ``""`` when absent."""

    return candidate.strip() if isinstance(candidate, str) else ""


def configured_asr_text_debug_subjects(owner: object) -> AsrTextDebugSubjects:
    """The registry's startup config, disabled for test doubles without the field."""

    configured = getattr(owner, "asr_text_debug_subjects", None)
    return configured if isinstance(configured, AsrTextDebugSubjects) else AsrTextDebugSubjects()


def session_asr_text_debug_subjects(
    session: object, *, identity: object | None = None
) -> AsrTextDebugSubjects:
    """The registry config a media session inherits, filtered to the session's ids.

    The bridge hands the provider a list already narrowed this way, so the
    provider's own final logging reads the same answer as this module's.
    """

    configured = AsrTextDebugSubjects()
    for source in (
        getattr(session, "registry", None),
        getattr(getattr(session, "runtime", None), "registry", None),
        None if identity is None else getattr(identity, "registry", None),
    ):
        if isinstance(getattr(source, "asr_text_debug_subjects", None), AsrTextDebugSubjects):
            configured = configured_asr_text_debug_subjects(source)
            break
    if not configured.enabled:
        return configured
    session_identity = identity if identity is not None else getattr(session, "identity", None)
    return configured.for_session(
        getattr(session_identity, "subject_id", None),
        getattr(session_identity, "device_id", None),
    )


def _listed(subjects: AsrTextDebugSubjects | None, candidates: tuple[object, ...]) -> bool:
    """Whether the switch names one of the ids the session carries."""

    if subjects is None:
        return False
    return subjects.logs(*(subject_id(value) for value in candidates))


def asr_text_debug_line(
    subjects: AsrTextDebugSubjects | None, *subject_ids: object, **fields: object
) -> str | None:
    """The ``asr_text_debug`` line for a switched-on session, ``None`` for everyone else.

    ``subject_ids`` are the subject and device ids the session carries; they
    select, and are not printed. What is printed is the log-safe
    ``*_sha256_8`` tag beside any other ``fields``, in order as
    ``key=value``. A ``None`` value becomes ``-`` so the line keeps its shape
    when a timestamp is unknown. A line carries the text only when the caller
    passes one, so a decision raised later cannot name an unlisted id and then
    print it. The raw text is redacted the same way every other outgoing text
    is: the line aids a test, it never carries an address or a phone number.
    """

    if not _listed(subjects, subject_ids):
        return None
    parts = [ASR_TEXT_DEBUG_LOG_PREFIX]
    for key, value in fields.items():
        if isinstance(value, str) and key.endswith("text"):
            value = redact_pii(value)
        parts.append(f"{key}={'-' if value is None else value}")
    return " ".join(parts)


def subject_tag(value: object) -> str:
    """The eight-hex-digit subject tag other logs also print, ``-`` when absent."""

    stripped = subject_id(value)
    return subject_id_hash(stripped) if stripped else "-"


def excerpt(text: str, *, limit: int = 200) -> str:
    """``text`` in one quoted field, bounded so one final cannot flood the log."""

    compact = " ".join(text.split())
    if len(compact) > limit:
        compact = compact[:limit] + "…"
    return f"{len(text)}:{compact}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = [
    "ASR_TEXT_DEBUG_LOG_ENV",
    "ASR_TEXT_DEBUG_LOG_PREFIX",
    "ASR_TEXT_TRACE_ENV",
    "AsrTextDebugSubjects",
    "asr_text_debug_line",
    "asr_text_trace_enabled",
    "configured_asr_text_debug_subjects",
    "excerpt",
    "session_asr_text_debug_subjects",
    "subject_id",
    "subject_tag",
]
