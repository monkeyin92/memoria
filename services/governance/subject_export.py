"""Subject-scoped reduction of the account export snapshot.

``AccountDataGovernance.export_account`` assembles its snapshot from
repositories that are scoped by ``account_id`` only.  Inside that snapshot,
``evidence_events.subject_id`` is the one column that proves which speaking
subject a row belongs to; conversation, archive projections, speaker,
evolution, legacy and guardian sections are account-scoped.  A subject export
therefore returns only evidence rows whose ``subject_id`` equals the
requested subject (a NULL or foreign ``subject_id`` is never attributed to
that subject), adds the AI-content annotations required of an export, and
declares every omitted section with a stable reason code so a partial export
can never be mistaken for a complete one.

The guardian audience is narrower still: it receives authorization (consent)
records plus isolated, content-free metadata, never verbatim evidence or
conversation.  This module only reduces an already-authorized snapshot; it
makes no authorization decision of its own and adds no new authority.

Two further guards keep the reduction from re-releasing what the runtime
already withdrew.  Consent rows are keyed by ``account_id`` unless they name a
subject, so account-level consents are released only when the requested
subject *is* the account, and every row is cut down to an explicit field
whitelist.  Evidence whose payload withdrew its text from retention
(``history_eligible=false``, ``owner_projection_eligible=false`` or
``memory_retention=ephemeral_only``) is dropped wholesale instead of trusting
a payload written before the ceiling existed, a session row must prove
``history_eligible=true`` exactly like the review exits require, and guardian
metadata reports a fixed event-bucket vocabulary instead of raw event types.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

SUBJECT_EXPORT_FORMAT_VERSION: Final[int] = 2
SERVICE_PROVIDER: Final[str] = "Memoria"
SELF_AUDIENCE: Final[str] = "self"
GUARDIAN_AUDIENCE: Final[str] = "guardian"
AUDIENCES: Final[frozenset[str]] = frozenset({SELF_AUDIENCE, GUARDIAN_AUDIENCE})

# Only model-authored evidence is labelled AI generated.  owner/guest are
# human speech, uncertain is unattributed speech, and system rows are
# operational records rather than model output.
AI_GENERATED_SPEAKER_CLASSES: Final[frozenset[str]] = frozenset({"assistant"})

# Explicit field whitelists.  Rows are never exported whole: ``bio`` (free
# text that may quote other people), ``avatar_url`` (external object reference)
# and ``phone_number_masked`` (personal identifier) stay behind, and every
# other snapshot column has to be named here before it can be released.
PROFILE_EXPORT_FIELDS: Final[tuple[str, ...]] = (
    "user_id",
    "display_name",
    "timezone",
    "companion_id",
    "subject_category",
    "birth_year_band",
    "age_evidence_status",
    "subject_revision",
    "auto_summary",
    "voice_reply",
    "gentle_reminders",
    "reject_non_owner_voice",
    "created_at",
    "updated_at",
)
ACCOUNT_EXPORT_FIELDS: Final[tuple[str, ...]] = (
    "user_id",
    "username",
    "created_at",
    "updated_at",
)

_EVIDENCE_KEYS: Final[tuple[str, ...]] = (
    "evidence_events",
    "archive_evidence_events",
)
# Export field name, archive table spellings and the released fields.  SQLite
# exports name the archive consent table ``consent_grants`` while the
# PostgreSQL adapter prefixes it, so both spellings are accepted for the same
# field.  ``account_id`` is deliberately absent: the scope block already names
# the account, so the row adds no information.
_ARCHIVE_CONSENT_SECTIONS: Final[tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]] = (
    (
        "consent_grants",
        ("consent_grants", "archive_consent_grants"),
        (
            "consent_grant_id",
            "purpose",
            "policy_version",
            "retention_policy",
            "granted_at",
            "expires_at",
            "revoked_at",
            "evidence_event_id",
        ),
    ),
    (
        "persona_learning_consents",
        ("persona_learning_consents",),
        (
            "policy_version",
            "granted_at",
            "revoked_at",
            "grant_event_id",
            "revoke_event_id",
        ),
    ),
    (
        "voice_clone_consents",
        ("voice_clone_consents",),
        (
            "policy_version",
            "granted_at",
            "revoked_at",
            "grant_event_id",
            "revoke_event_id",
        ),
    ),
)
SUBJECT_PERSON_CONSENT_FIELDS: Final[tuple[str, ...]] = (
    "consent_id",
    "subject_person_id",
    "consent_kind",
    "policy_version",
    "granted_at",
    "expires_at",
    "revoked_at",
    "evidence_event_id",
    "revocation_evidence_event_id",
)
ACCOUNT_CONSENT_SECTIONS: Final[tuple[str, ...]] = tuple(
    section[0] for section in _ARCHIVE_CONSENT_SECTIONS
)

# Payload markers that withdraw the text from replay/retention.  A legacy row
# written before the ceiling existed can still carry verbatim text, so such
# rows are dropped wholesale instead of being re-released by the export.
_RETENTION_WITHHELD_KEYS: Final[tuple[str, ...]] = (
    "history_eligible",
    "owner_projection_eligible",
)
_EPHEMERAL_RETENTION: Final[str] = "ephemeral_only"

# The guardian audience gets a fixed bucket vocabulary instead of raw
# ``event_type`` strings: the column is free text, and governance metadata must
# not become a channel for whatever a writer stored there.
_GUARDIAN_EVENT_BUCKETS: Final[dict[str, str]] = {
    "speech.utterance_finalized": "speech",
    "assistant.playout_stopped": "assistant_delivery",
    "assistant.playout_progressed": "assistant_delivery",
    "assistant.reply_delivered": "assistant_delivery",
    "speaker.classified": "speaker_classification",
    "emotion_observation": "emotion_observation",
    "topic.observation": "topic_observation",
    "tutor.practice_completed": "tutor_practice",
    "tutor.practice_turn_recorded": "tutor_practice",
    "study.progress_updated": "tutor_practice",
    "memory.claim_reviewed": "memory_review",
    "persona.trait_reviewed": "persona_review",
    "owner.action_recorded": "owner_action",
    "guardian.crisis_event": "guardian_safety",
    "guardian.consent_granted": "guardian_consent",
    "guardian.consent_revoked": "guardian_consent",
}
GUARDIAN_EVENT_BUCKET_SCHEMA: Final[tuple[str, ...]] = (
    *sorted(set(_GUARDIAN_EVENT_BUCKETS.values())),
    "other",
)
_NO_SUBJECT_LINEAGE: Final[str] = "no_subject_lineage"
_AUDIENCE_FORBIDDEN: Final[str] = "audience_forbidden"


@dataclass(frozen=True, slots=True)
class OmittedSection:
    """One snapshot section a subject export deliberately drops."""

    name: str
    reason_code: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }


_SELF_OMISSIONS: Final[tuple[OmittedSection, ...]] = (
    OmittedSection(
        name="conversation",
        reason_code=_NO_SUBJECT_LINEAGE,
        reason=(
            "messages, daily summaries, voice sessions and digital-self preview rows "
            "carry no subject_id; only profile/account metadata is included"
        ),
    ),
    OmittedSection(
        name="archive",
        reason_code=_NO_SUBJECT_LINEAGE,
        reason=(
            "claims, episodes, transcripts, skills, persona and voice projections "
            "carry no subject_id; only matching evidence rows are included"
        ),
    ),
    OmittedSection(
        name="speaker",
        reason_code=_NO_SUBJECT_LINEAGE,
        reason="speaker identities and profiles carry no subject_id",
    ),
    OmittedSection(
        name="evolution",
        reason_code=_NO_SUBJECT_LINEAGE,
        reason="evolution signals carry no subject_id",
    ),
    OmittedSection(
        name="legacy",
        reason_code=_NO_SUBJECT_LINEAGE,
        reason="legacy grants, shells and shell turns carry no subject_id",
    ),
    OmittedSection(
        name="guardian",
        reason_code=_NO_SUBJECT_LINEAGE,
        reason=(
            "guardian links, weekly reports, notifications and crisis events carry no "
            "subject_id; only consent records scoped to the subject are included"
        ),
    ),
)

_GUARDIAN_OMISSIONS: Final[tuple[OmittedSection, ...]] = (
    OmittedSection(
        name="evidence",
        reason_code=_AUDIENCE_FORBIDDEN,
        reason=(
            "the guardian audience receives metadata only; verbatim evidence, "
            "transcripts and conversation content are withheld"
        ),
    ),
    OmittedSection(
        name="conversation",
        reason_code=_AUDIENCE_FORBIDDEN,
        reason="the guardian audience receives no conversation content",
    ),
    OmittedSection(
        name="archive",
        reason_code=_AUDIENCE_FORBIDDEN,
        reason="the guardian audience receives no archive projections or transcripts",
    ),
    OmittedSection(
        name="speaker",
        reason_code=_AUDIENCE_FORBIDDEN,
        reason="the guardian audience receives no speaker or voiceprint data",
    ),
    OmittedSection(
        name="evolution",
        reason_code=_AUDIENCE_FORBIDDEN,
        reason="the guardian audience receives no learned projections",
    ),
    OmittedSection(
        name="legacy",
        reason_code=_AUDIENCE_FORBIDDEN,
        reason="the guardian audience receives no legacy conversation content",
    ),
    OmittedSection(
        name="guardian",
        reason_code=_AUDIENCE_FORBIDDEN,
        reason=(
            "links, weekly reports and crisis notifications are delivered through "
            "their own consent channel, not through this export"
        ),
    ),
    OmittedSection(
        name="account",
        reason_code=_AUDIENCE_FORBIDDEN,
        reason="profile and account metadata are withheld from the guardian audience",
    ),
    OmittedSection(
        name="account_consents",
        reason_code=_AUDIENCE_FORBIDDEN,
        reason=(
            "account-level authorization records are withheld from the guardian "
            "audience; only consent records naming the subject are released"
        ),
    ),
)


def manifest_sha256(payload: Mapping[str, Any]) -> str:
    """Canonical digest of an export body, which must exclude this field."""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_subject_export_request(*, subject_id: str | None, audience: str) -> None:
    """Fail closed before any repository read, and never guess a subject."""
    if audience not in AUDIENCES:
        raise ValueError(f"unsupported export audience: {audience}")
    if subject_id is None:
        if audience != SELF_AUDIENCE:
            raise ValueError("guardian export requires an explicit subject_id")
        return
    if not subject_id.strip():
        raise ValueError("subject_id must not be blank when provided")


def build_subject_export(
    *,
    snapshot: Mapping[str, Any],
    account_id: str,
    subject_id: str,
    audience: str,
) -> dict[str, Any]:
    """Reduce an account snapshot to one provably attributed subject scope."""
    validate_subject_export_request(subject_id=subject_id, audience=audience)
    archive = _section(snapshot, "archive")
    control = _section(snapshot, "conversation")
    guardian = _section(snapshot, "guardian")
    evidence = _scoped_evidence(
        archive,
        account_id=account_id,
        subject_id=subject_id,
    )
    subject_is_account = subject_id == account_id
    consent = _consent_section(
        archive=archive,
        guardian=guardian,
        subject_id=subject_id,
        include_account_consents=audience == SELF_AUDIENCE and subject_is_account,
    )
    sections: dict[str, Any] = {}
    omissions: list[OmittedSection] = []
    if audience == GUARDIAN_AUDIENCE:
        sections["consent"] = consent
        sections["evidence_metadata"] = _evidence_metadata(
            evidence=evidence,
            subject_id=subject_id,
        )
        omissions.extend(_GUARDIAN_OMISSIONS)
    else:
        if subject_is_account:
            sections["account"] = _account_section(control)
        else:
            omissions.append(_foreign_subject_account_omission())
        if not subject_is_account:
            omissions.append(_account_consent_omission())
        sections["evidence"] = _evidence_section(
            evidence=evidence,
            subject_id=subject_id,
        )
        sections["consent"] = consent
        omissions.extend(_SELF_OMISSIONS)
    body: dict[str, Any] = {
        "format_version": SUBJECT_EXPORT_FORMAT_VERSION,
        "generated_at": snapshot.get("generated_at"),
        "account_id": account_id,
        "scope": {
            "export": "subject",
            "account_id": account_id,
            "subject_id": subject_id,
            "audience": audience,
            "partial": True,
            "partial_reason": (
                "subject-scoped export: only provably subject-attributed rows are "
                "returned, and every omitted section is declared in omitted_sections"
            ),
            "included_sections": sorted(sections),
        },
        "sections": sections,
        "omitted_sections": [omission.as_dict() for omission in omissions],
    }
    # The manifest covers the reduced body, so it never attests data that the
    # subject-scoped export did not actually release.
    body["manifest_sha256"] = manifest_sha256(body)
    return body


def _foreign_subject_account_omission() -> OmittedSection:
    return OmittedSection(
        name="account",
        reason_code=_NO_SUBJECT_LINEAGE,
        reason=(
            "profile and account rows are keyed by account_id while the requested "
            "subject_id differs, so the account profile is not the subject's"
        ),
    )


def _account_consent_omission() -> OmittedSection:
    return OmittedSection(
        name="account_consents",
        reason_code=_NO_SUBJECT_LINEAGE,
        reason=(
            "account-level authorization records are keyed by account_id while the "
            "requested subject_id differs; only consent records naming the subject "
            "are released"
        ),
    )


def _section(snapshot: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    sections = snapshot.get("sections")
    if not isinstance(sections, Mapping):
        return {}
    section = sections.get(name)
    if not isinstance(section, Mapping):
        return {}
    return dict(section)


def _rows(source: Mapping[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


@dataclass(frozen=True, slots=True)
class ScopedEvidence:
    """Evidence rows that survived subject and retention scoping."""

    items: list[dict[str, Any]]
    unattributed: int
    other_subject: int
    other_account: int
    retention_withheld: int
    unparsed_payload: int

    @property
    def excluded_counts(self) -> dict[str, int]:
        return {
            "unattributed": self.unattributed,
            "other_subject": self.other_subject,
            "other_account": self.other_account,
            "retention_withheld": self.retention_withheld,
            "unparsed_payload": self.unparsed_payload,
        }


def _scoped_evidence(
    archive: Mapping[str, Any],
    *,
    account_id: str,
    subject_id: str,
) -> ScopedEvidence:
    items: list[dict[str, Any]] = []
    unattributed = 0
    other_subject = 0
    other_account = 0
    retention_withheld = 0
    unparsed_payload = 0
    for row in _rows(archive, *_EVIDENCE_KEYS):
        # The snapshot is account-scoped already; re-checking the row keeps a
        # mis-scoped row out of the subject scope instead of trusting the query.
        row_account = row.get("account_id")
        if row_account is not None and str(row_account) != account_id:
            other_account += 1
            continue
        row_subject = row.get("subject_id")
        if row_subject is None:
            unattributed += 1
            continue
        if str(row_subject) != subject_id:
            other_subject += 1
            continue
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            # An unparsed payload cannot prove that its text was retained under
            # the current ceiling, so the row is dropped instead of re-released.
            unparsed_payload += 1
            continue
        session_id = row.get("session_id")
        session_scoped = isinstance(session_id, str) and bool(session_id.strip())
        if _retention_withheld(payload, session_scoped=session_scoped):
            retention_withheld += 1
            continue
        items.append(_annotated_evidence(row))
    return ScopedEvidence(
        items=items,
        unattributed=unattributed,
        other_subject=other_subject,
        other_account=other_account,
        retention_withheld=retention_withheld,
        unparsed_payload=unparsed_payload,
    )


def _retention_withheld(payload: Mapping[str, Any], *, session_scoped: bool) -> bool:
    """True when the payload withdrew the text from replay or retention.

    A session row must prove eligibility with ``history_eligible is True``,
    which is exactly what the HTTP review exits require.  Legacy session rows
    written before the flag existed therefore stay withheld instead of
    re-releasing text that no review exit would serve.  Account-level internal
    events without a session are not held to that flag.
    """

    def _withdraws(source: Mapping[str, Any]) -> bool:
        for key in _RETENTION_WITHHELD_KEYS:
            if source.get(key) is False:
                return True
        return source.get("memory_retention") == _EPHEMERAL_RETENTION

    if _withdraws(payload):
        return True
    interaction = payload.get("interaction")
    if isinstance(interaction, Mapping) and _withdraws(interaction):
        return True
    return session_scoped and payload.get("history_eligible") is not True


def _annotated_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["content_id"] = str(item.get("event_id") or "")
    item["ai_generated"] = str(item.get("speaker_class") or "") in AI_GENERATED_SPEAKER_CLASSES
    item["service_provider"] = SERVICE_PROVIDER
    return item


def _evidence_section(
    *,
    evidence: ScopedEvidence,
    subject_id: str,
) -> dict[str, Any]:
    items = evidence.items
    return {
        "subject_id": subject_id,
        "annotation": {
            "content_id": "event_id",
            "service_provider": SERVICE_PROVIDER,
            "ai_generated_speaker_classes": sorted(AI_GENERATED_SPEAKER_CLASSES),
        },
        "items": items,
        "included_count": len(items),
        "excluded_counts": evidence.excluded_counts,
        "withheld_reason": (
            "evidence rows without a proven subject, from another account, without "
            "a parseable payload, or whose payload withdrew the text from "
            "retention (history_eligible=false / owner_projection_eligible=false / "
            "memory_retention=ephemeral_only) are omitted; a session row must also "
            "prove history_eligible=true like the review exits require"
        ),
    }


def _evidence_metadata(
    *,
    evidence: ScopedEvidence,
    subject_id: str,
) -> dict[str, Any]:
    items = evidence.items
    buckets: dict[str, int] = {}
    for item in items:
        bucket = _GUARDIAN_EVENT_BUCKETS.get(
            str(item.get("event_type") or ""),
            "other",
        )
        buckets[bucket] = buckets.get(bucket, 0) + 1
    first_occurred_at, last_occurred_at = _occurred_bounds(items)
    return {
        "subject_id": subject_id,
        "evidence_count": len(items),
        # Fixed vocabulary: raw event_type strings are never released, so the
        # metadata cannot carry whatever text a writer stored in the column.
        "event_type_buckets": dict(sorted(buckets.items())),
        "event_type_bucket_schema": list(GUARDIAN_EVENT_BUCKET_SCHEMA),
        "first_occurred_at": first_occurred_at,
        "last_occurred_at": last_occurred_at,
        "verbatim_content_included": False,
        "excluded_counts": evidence.excluded_counts,
        "withheld_reason": (
            "rows without a proven subject, from another account, without a "
            "parseable payload, whose payload withdrew the text from retention, or "
            "session rows without history_eligible=true stay excluded from the count"
        ),
    }


def _occurred_bounds(items: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    parsed: list[tuple[datetime, str]] = []
    for item in items:
        raw = item.get("occurred_at")
        if not isinstance(raw, str):
            continue
        try:
            parsed.append((datetime.fromisoformat(raw), raw))
        except ValueError:
            continue
    if not parsed:
        return None, None
    parsed.sort(key=lambda entry: entry[0])
    return parsed[0][1], parsed[-1][1]


def _consent_section(
    *,
    archive: Mapping[str, Any],
    guardian: Mapping[str, Any],
    subject_id: str,
    include_account_consents: bool,
) -> dict[str, Any]:
    section: dict[str, Any] = {
        "note": "authorization governance records only; no conversation content",
    }
    # Person-scoped consents name their subject directly, so they are the only
    # consent rows provable for a subject other than the account itself.
    section["subject_person_consents"] = [
        _whitelisted(row, SUBJECT_PERSON_CONSENT_FIELDS)
        for row in _rows(guardian, "person_consents")
        if str(row.get("subject_person_id")) == subject_id
    ]
    if include_account_consents:
        for field, keys, fields in _ARCHIVE_CONSENT_SECTIONS:
            section[field] = [_whitelisted(row, fields) for row in _rows(archive, *keys)]
    return section


def _account_section(control: Mapping[str, Any]) -> dict[str, Any]:
    """Release the account's own profile through an explicit field whitelist."""
    return {
        "profile": _whitelisted(control.get("profile"), PROFILE_EXPORT_FIELDS),
        "account": _whitelisted(control.get("account"), ACCOUNT_EXPORT_FIELDS),
        "field_whitelist": {
            "profile": list(PROFILE_EXPORT_FIELDS),
            "account": list(ACCOUNT_EXPORT_FIELDS),
        },
    }


def _whitelisted(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {}
    return {field: row[field] for field in fields if field in row}
