"""Pure helpers shared by the identity application service."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from services.common.companions import COMPANION_IDS
from services.identity.domain import (
    IdempotencyRecord,
    IdentityConflictError,
    PersonSubject,
    canonical_persona_assignment_id,
)


def _now(value: datetime | None) -> datetime:
    return value.astimezone(UTC) if value is not None else datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


# The built-in companion catalogue is the only persona source today.  The
# frozen per-account custom personas of increment two widen this set; the
# decision record lives in
# ``outputs/design/persona-switch-20260911/decision-01-persona-catalogue-freeze.md``.
_KNOWN_PERSONA_IDS: frozenset[str] = COMPANION_IDS
_PERSONA_VERSION_PREFIX = ":v"


def _resolve_persona_selection(
    persona_selection: str,
    *,
    custom_persona_ids: frozenset[str] = frozenset(),
) -> tuple[str, int]:
    """Split ``"{persona_id}"`` / ``"{persona_id}:v{n}"`` into its two parts.

    A bare persona id is the first version (``v1``); the canonical assignment
    id is validated here so no caller can persist a second spelling.  The id
    must be a built-in catalogue id OR one of ``custom_persona_ids`` (the
    account's own custom personas); anything else -- including another
    account's custom persona -- is rejected.
    """
    persona_id, separator, raw_version = persona_selection.strip().partition(
        _PERSONA_VERSION_PREFIX
    )
    if persona_id not in _KNOWN_PERSONA_IDS and persona_id not in custom_persona_ids:
        raise ValueError(f"unknown persona_selection {persona_id!r}")
    if separator:
        if not raw_version.isdigit():
            raise ValueError("persona_selection version must be a positive integer")
        persona_version = int(raw_version)
    else:
        persona_version = 1
    canonical_persona_assignment_id(persona_id, persona_version)
    return persona_id, persona_version


def _request_hash(**fields: object) -> str:
    """Canonical digest of an idempotent command request."""
    return hashlib.sha256(
        json.dumps(
            fields,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _replay_or_conflict(
    existing: IdempotencyRecord,
    *,
    content_hash: str,
) -> None:
    if existing.content_hash != content_hash:
        raise IdentityConflictError(
            "idempotency key reused with different content"
        )


def _same_person_content(left: PersonSubject, right: PersonSubject) -> bool:
    """Authoritative registration fields; timestamps are not content."""
    return (
        left.person_id == right.person_id
        and left.display_name == right.display_name
        and left.subject_category == right.subject_category
        and left.age_band == right.age_band
        and left.age_evidence_status == right.age_evidence_status
        and left.locale == right.locale
        and left.timezone == right.timezone
        and left.status == right.status
    )


def _canonical_registration_result(
    stored: PersonSubject, requested: PersonSubject
) -> PersonSubject:
    """Idempotent registration replay: return the persisted row only.

    An exact replay returns the actually persisted object; any drift between
    the request and the persisted authoritative fields is an explicit
    conflict -- the caller never receives an unpersisted request object.
    """
    if not _same_person_content(stored, requested):
        raise IdentityConflictError(
            f"person {requested.person_id} already registered with different "
            "authoritative fields (display_name/age/category/locale/timezone)"
        )
    return stored
