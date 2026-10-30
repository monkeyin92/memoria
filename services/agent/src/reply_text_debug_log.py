"""A test-period only INFO line carrying each turn's reply text for a test subject.

The switch is ``REPLY_TEXT_DEBUG_LOG``, off unless explicitly enabled, and it
only opens the gate: the session must also carry the one subject listed in
``MINOR_LIMIT_BYPASS_TEST_SUBJECTS``. A real child session therefore never
writes its reply text, whatever the switch says.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from services.agent.src.minor_limit_bypass import MinorLimitBypass, subject_id_hash
from services.common.redaction import redact_pii

REPLY_TEXT_DEBUG_LOG_ENV: Final = "REPLY_TEXT_DEBUG_LOG"
REPLY_TEXT_DEBUG_LOG_PREFIX: Final = "reply_text_debug"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReplyTextDebugLog:
    """The switch. Disabled is the default."""

    enabled: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ReplyTextDebugLog:
        raw = (os.environ if environ is None else environ).get(REPLY_TEXT_DEBUG_LOG_ENV, "")
        switch = cls(raw.strip().lower() in ("1", "true", "yes", "on"))
        if switch.enabled:
            logger.warning("reply text debug log active at=%s", _now())
        return switch


def reply_text_debug_enabled(owner: object, subject_id: object) -> bool:
    """Whether this session may log its reply text: the switch on and the test subject.

    ``owner`` is the registry that opened the session. Decided once at session
    open, so a turn costs one boolean check and a real child's subject can never
    match.
    """

    switch = getattr(owner, "reply_text_debug_log", None)
    bypass = getattr(owner, "minor_limit_bypass", None)
    return (
        isinstance(switch, ReplyTextDebugLog)
        and switch.enabled
        and isinstance(bypass, MinorLimitBypass)
        and bypass.enabled
        and isinstance(subject_id, str)
        and subject_id in bypass.subject_ids
    )


def log_reply_text(context: object, fence: object, *, aigc_marker: bool = False) -> None:
    """Log this reply's full text when the session allowed it and the reply was audible text.

    A marker fence is not a reply, so it never supplies the text. The text goes
    through the same redaction as every other outgoing text before it is written.
    """

    if not getattr(context, "reply_text_debug", False) or aigc_marker:
        return
    text = getattr(getattr(context, "output", None), "assistant_text", None)
    if not isinstance(text, str) or not text.strip():
        return
    identity = getattr(context, "identity", None)
    subject = getattr(identity, "subject_id", None)
    logger.info(
        "%s turn_id=%s generation_id=%s subject_sha256_8=%s text_len=%d text=%s",
        REPLY_TEXT_DEBUG_LOG_PREFIX,
        getattr(fence, "turn_id", None),
        getattr(fence, "generation_id", None),
        subject_id_hash(subject) if isinstance(subject, str) else "-",
        len(text),
        json.dumps(redact_pii(text), ensure_ascii=False),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = [
    "REPLY_TEXT_DEBUG_LOG_ENV",
    "REPLY_TEXT_DEBUG_LOG_PREFIX",
    "ReplyTextDebugLog",
    "log_reply_text",
    "reply_text_debug_enabled",
]
