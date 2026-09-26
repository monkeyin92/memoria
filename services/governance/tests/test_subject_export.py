"""Subject-scoped export reduction (export privacy batch).

``export_account`` keeps the account-wide snapshot for internal callers and
fail-closes on an explicit subject export: only evidence rows whose
``subject_id`` proves attribution reach the subject, and every omitted section
is declared.  The guardian audience never receives verbatim content, even with
an active guardian link and a weekly-report consent on file.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import hash_password
from services.evolution.account_repository import SqliteEvolutionAccountRepository
from services.governance.account_data import (
    AccountDataGovernance,
    SqliteAccountRepository,
)
from services.governance.subject_export import (
    ACCOUNT_EXPORT_FIELDS,
    GUARDIAN_EVENT_BUCKET_SCHEMA,
    PROFILE_EXPORT_FIELDS,
    SUBJECT_PERSON_CONSENT_FIELDS,
    build_subject_export,
)
from services.guardian.domain import ConsentRecord, PersonConsentRecord
from services.guardian.sqlite_store import SqliteGuardianStore

ACCOUNT_ID = "account-subject-export"
GUARDIAN_ACCOUNT = "account-guardian"
FOREIGN_ACCOUNT = "account-foreign"
CHILD_SUBJECT = "child-subject"
RAW_CONVERSATION_TEXT = "这是账号里的原始对话。"
GUARDIAN_LINK_ID = "00000000-0000-0000-0000-000000000201"
BINDING_CODE_HASH = hashlib.sha256(b"subject-export-binding-code").hexdigest()
_NOW = datetime(2026, 9, 18, 4, 0, tzinfo=UTC)


class _UnusedVoiceProfiles:
    """The export path never reads voice profiles; the port only has to exist."""

    async def profiles(self, *, account_id: str) -> tuple[Any, ...]:  # pragma: no cover
        raise AssertionError("subject export must not read voice profiles")


def _events() -> tuple[EvidenceEvent, ...]:
    return (
        EvidenceEvent(
            event_id="evidence-owner",
            account_id=ACCOUNT_ID,
            event_type="speech.utterance_finalized",
            occurred_at=_NOW,
            speaker_class="owner",
            source="subject-export-test",
            subject_id=ACCOUNT_ID,
            payload={"text": "主人自己的话。"},
        ),
        EvidenceEvent(
            event_id="evidence-assistant",
            account_id=ACCOUNT_ID,
            event_type="assistant.reply_delivered",
            occurred_at=_NOW + timedelta(seconds=1),
            speaker_class="assistant",
            source="subject-export-test",
            subject_id=ACCOUNT_ID,
            payload={"text": "模型的回复。"},
        ),
        EvidenceEvent(
            event_id="evidence-child",
            account_id=ACCOUNT_ID,
            event_type="speech.utterance_finalized",
            occurred_at=_NOW + timedelta(seconds=2),
            speaker_class="owner",
            source="subject-export-test",
            subject_id=CHILD_SUBJECT,
            payload={"text": "孩子说的这句话。"},
        ),
        EvidenceEvent(
            event_id="evidence-legacy",
            account_id=ACCOUNT_ID,
            event_type="speech.utterance_finalized",
            occurred_at=_NOW + timedelta(seconds=3),
            speaker_class="owner",
            source="subject-export-test",
            subject_id=None,
            payload={"text": "没有主体归属的旧话轮。"},
        ),
        EvidenceEvent(
            event_id="evidence-foreign-account",
            account_id=FOREIGN_ACCOUNT,
            event_type="speech.utterance_finalized",
            occurred_at=_NOW + timedelta(seconds=4),
            speaker_class="owner",
            source="subject-export-test",
            subject_id=ACCOUNT_ID,
            payload={"text": "另一个账号的话轮。"},
        ),
        EvidenceEvent(
            event_id="evidence-ephemeral",
            account_id=ACCOUNT_ID,
            event_type="speech.utterance_finalized",
            occurred_at=_NOW + timedelta(seconds=5),
            speaker_class="owner",
            source="subject-export-test",
            subject_id=ACCOUNT_ID,
            payload={
                "text": "早该收回的逐字文本。",
                "history_eligible": False,
                "owner_projection_eligible": False,
                "memory_retention": "ephemeral_only",
            },
        ),
        EvidenceEvent(
            event_id="evidence-nested-ephemeral",
            account_id=ACCOUNT_ID,
            event_type="assistant.reply_delivered",
            occurred_at=_NOW + timedelta(seconds=6),
            speaker_class="assistant",
            source="subject-export-test",
            subject_id=ACCOUNT_ID,
            payload={
                "text": "嵌套收回的文本。",
                "interaction": {
                    "history_eligible": False,
                    "memory_retention": "ephemeral_only",
                },
            },
        ),
        EvidenceEvent(
            event_id="evidence-session-unflagged",
            account_id=ACCOUNT_ID,
            event_type="speech.utterance_finalized",
            occurred_at=_NOW + timedelta(seconds=8),
            speaker_class="owner",
            source="subject-export-test",
            subject_id=ACCOUNT_ID,
            session_id="legacy-session-without-flag",
            turn_id=1,
            generation_id=1,
            payload={"text": "旧会话行没写 history_eligible。"},
        ),
        EvidenceEvent(
            event_id="evidence-internal-unflagged",
            account_id=ACCOUNT_ID,
            event_type="guardian.consent_granted",
            occurred_at=_NOW + timedelta(seconds=9),
            speaker_class="system",
            source="subject-export-test",
            subject_id=ACCOUNT_ID,
            payload={"text": "内部事件不按历史标志判定。", "purpose": "account_lifetime"},
        ),
    )


async def _governance(
    tmp_path: Path,
    *,
    guardian: SqliteGuardianStore | None = None,
) -> AccountDataGovernance:
    database_path = tmp_path / "memoria.sqlite3"
    store = MemoryStore(str(database_path))
    store.register_account(
        user_id=ACCOUNT_ID,
        username="subject-export-owner",
        username_normalized="subject-export-owner",
        password_hash=hash_password("safe-passphrase"),
        now=_NOW.isoformat(),
    )
    archive = LifeArchive.sqlite(database_path)
    for event in _events():
        await archive.record(event)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO messages (
                user_id, client_message_id, request_fingerprint, role, text,
                emotion, local_date, created_at
            ) VALUES (?, ?, ?, 'user', ?, NULL, ?, ?)
            """,
            (
                ACCOUNT_ID,
                "client-message-1",
                "0" * 64,
                RAW_CONVERSATION_TEXT,
                "2026-09-18",
                _NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO consent_grants (
                consent_grant_id, account_id, purpose, policy_version,
                retention_policy, granted_at, expires_at, revoked_at,
                evidence_event_id
            ) VALUES ('consent-archive', ?, 'raw_voice', 'voice-consent-v1',
                      'account_lifetime', ?, NULL, NULL, 'evidence-owner')
            """,
            (ACCOUNT_ID, _NOW.isoformat()),
        )
        connection.execute(
            """
            UPDATE profiles
            SET bio = ?, avatar_url = ?, phone_number_masked = ?
            WHERE user_id = ?
            """,
            (
                "简介里可能引用别人的内容。",
                "https://cdn.test/avatar/subject-export.png",
                "138****0000",
                ACCOUNT_ID,
            ),
        )
        # A payload that cannot be parsed cannot prove its text was retained
        # under the current ceiling, so the export has to drop it as well.
        connection.execute(
            """
            INSERT INTO evidence_events (
                event_id, account_id, session_id, turn_id, generation_id,
                event_type, schema_version, occurred_at, recorded_at,
                subject_id, speaker_identity_id, speaker_class, source,
                consent_grant_id, payload_json, content_sha256,
                supersedes_event_id
            ) VALUES ('evidence-unparsed', ?, NULL, NULL, NULL,
                      'speech.utterance_finalized', 1, ?, ?,
                      ?, NULL, 'owner', 'subject-export-test',
                      NULL, '{"text": "未解析文本。",', ?, NULL)
            """,
            (
                ACCOUNT_ID,
                (_NOW + timedelta(seconds=7)).isoformat(),
                _NOW.isoformat(),
                ACCOUNT_ID,
                "0" * 64,
            ),
        )
    return AccountDataGovernance(
        memory_store=store,
        archive_repository=SqliteAccountRepository.archive(database_path),
        speaker_repository=SqliteAccountRepository.speaker(tmp_path / "speakers.sqlite3"),
        evolution_repository=SqliteEvolutionAccountRepository(
            tmp_path / "evolution.sqlite3"
        ),
        voice_profiles=_UnusedVoiceProfiles(),  # type: ignore[arg-type]
        guardian_repository=guardian,
    )


def _canonical_manifest(body: dict[str, Any]) -> str:
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _serialized(exported: dict[str, Any]) -> str:
    return json.dumps(exported, ensure_ascii=False, sort_keys=True)


def _contains_key(node: Any, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(_contains_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(_contains_key(item, key) for item in node)
    return False


@pytest.mark.asyncio
async def test_self_export_returns_only_evidence_attributed_to_the_subject(
    tmp_path: Path,
) -> None:
    governance = await _governance(tmp_path)

    exported = await governance.export_account(
        ACCOUNT_ID,
        subject_id=ACCOUNT_ID,
        audience="self",
    )

    assert exported["format_version"] == 2
    evidence = exported["sections"]["evidence"]
    assert sorted(item["event_id"] for item in evidence["items"]) == [
        "evidence-assistant",
        "evidence-internal-unflagged",
        "evidence-owner",
    ]
    assert evidence["included_count"] == 3
    assert evidence["excluded_counts"] == {
        "unattributed": 1,
        "other_subject": 1,
        "other_account": 0,
        "retention_withheld": 3,
        "unparsed_payload": 1,
    }
    assert evidence["withheld_reason"]
    annotation = evidence["annotation"]
    assert annotation["content_id"] == "event_id"
    assert annotation["service_provider"] == "Memoria"
    assert annotation["ai_generated_speaker_classes"] == ["assistant"]
    labelled = {item["event_id"]: item for item in evidence["items"]}
    assert labelled["evidence-owner"]["content_id"] == "evidence-owner"
    assert labelled["evidence-owner"]["ai_generated"] is False
    assert labelled["evidence-owner"]["service_provider"] == "Memoria"
    assert labelled["evidence-assistant"]["ai_generated"] is True
    serialized = _serialized(exported)
    assert "孩子说的这句话。" not in serialized
    assert "没有主体归属的旧话轮。" not in serialized
    assert "另一个账号的话轮。" not in serialized
    assert "早该收回的逐字文本。" not in serialized
    assert "嵌套收回的文本。" not in serialized
    assert "未解析文本。" not in serialized
    # A session row without history_eligible=true is withheld like the review
    # exits require; an internal row without a session is not held to it.
    assert "旧会话行没写 history_eligible。" not in serialized
    assert "内部事件不按历史标志判定。" in serialized
    assert "payload_json" not in serialized


@pytest.mark.asyncio
async def test_self_export_declares_partial_scope_and_omits_unscoped_sections(
    tmp_path: Path,
) -> None:
    governance = await _governance(tmp_path)

    exported = await governance.export_account(ACCOUNT_ID, subject_id=ACCOUNT_ID)

    scope = exported["scope"]
    assert scope["export"] == "subject"
    assert scope["account_id"] == ACCOUNT_ID
    assert scope["subject_id"] == ACCOUNT_ID
    assert scope["audience"] == "self"
    assert scope["partial"] is True
    assert scope["partial_reason"]
    assert scope["included_sections"] == ["account", "consent", "evidence", "persona"]

    omissions = {
        entry["name"]: entry["reason_code"] for entry in exported["omitted_sections"]
    }
    assert omissions == {
        "conversation": "no_subject_lineage",
        "archive": "no_subject_lineage",
        "speaker": "no_subject_lineage",
        "evolution": "no_subject_lineage",
        "legacy": "no_subject_lineage",
        "guardian": "no_subject_lineage",
    }
    assert all(entry["reason"] for entry in exported["omitted_sections"])
    assert set(exported["sections"]) == {"account", "consent", "evidence", "persona"}
    account_section = exported["sections"]["account"]
    assert account_section["profile"]["user_id"] == ACCOUNT_ID
    assert account_section["account"]["username"] == "subject-export-owner"
    assert account_section["field_whitelist"] == {
        "profile": list(PROFILE_EXPORT_FIELDS),
        "account": list(ACCOUNT_EXPORT_FIELDS),
    }
    assert [
        row["consent_grant_id"]
        for row in exported["sections"]["consent"]["consent_grants"]
    ] == ["consent-archive"]
    serialized = _serialized(exported)
    assert RAW_CONVERSATION_TEXT not in serialized
    assert "简介里可能引用别人的内容。" not in serialized
    assert "https://cdn.test/avatar/subject-export.png" not in serialized
    assert "138****0000" not in serialized
    assert "digital_self_preview" not in serialized
    assert "daily_summaries" not in serialized
    assert "speaker_identities" not in serialized


@pytest.mark.asyncio
async def test_subject_export_never_falls_back_to_account_wide_content(
    tmp_path: Path,
) -> None:
    governance = await _governance(tmp_path)

    exported = await governance.export_account(ACCOUNT_ID, subject_id=CHILD_SUBJECT)

    assert [
        item["event_id"] for item in exported["sections"]["evidence"]["items"]
    ] == ["evidence-child"]
    assert exported["sections"]["evidence"]["excluded_counts"] == {
        "unattributed": 1,
        "other_subject": 7,
        "other_account": 0,
        "retention_withheld": 0,
        "unparsed_payload": 0,
    }
    assert "account" not in exported["sections"]
    omissions = {entry["name"] for entry in exported["omitted_sections"]}
    assert {"account", "account_consents"} <= omissions
    consent = exported["sections"]["consent"]
    assert "consent_grants" not in consent
    assert "persona_learning_consents" not in consent
    assert "voice_clone_consents" not in consent
    assert "consent-archive" not in _serialized(exported)
    serialized = _serialized(exported)
    assert "主人自己的话。" not in serialized
    assert "模型的回复。" not in serialized
    assert "没有主体归属的旧话轮。" not in serialized

    unknown = await governance.export_account(ACCOUNT_ID, subject_id="unknown-subject")
    assert unknown["sections"]["evidence"]["items"] == []
    assert unknown["sections"]["evidence"]["included_count"] == 0
    assert unknown["sections"]["evidence"]["excluded_counts"] == {
        "unattributed": 1,
        "other_subject": 8,
        "other_account": 0,
        "retention_withheld": 0,
        "unparsed_payload": 0,
    }
    assert RAW_CONVERSATION_TEXT not in _serialized(unknown)


@pytest.mark.asyncio
async def test_guardian_export_withholds_verbatim_content_and_reports_metadata(
    tmp_path: Path,
) -> None:
    guardian = SqliteGuardianStore(tmp_path / "guardian.sqlite3")
    guardian.initialize()
    await guardian.grant_person_consent(
        _person_consent(consent_id="consent-child", subject=CHILD_SUBJECT),
        actor_person_id=ACCOUNT_ID,
    )
    await guardian.grant_person_consent(
        _person_consent(consent_id="consent-other", subject="other-person"),
        actor_person_id=ACCOUNT_ID,
    )
    governance = await _governance(tmp_path, guardian=guardian)

    exported = await governance.export_account(
        ACCOUNT_ID,
        subject_id=CHILD_SUBJECT,
        audience="guardian",
    )

    assert exported["scope"]["audience"] == "guardian"
    assert set(exported["sections"]) == {"consent", "evidence_metadata", "persona_metadata"}
    metadata = exported["sections"]["evidence_metadata"]
    assert metadata["subject_id"] == CHILD_SUBJECT
    assert metadata["evidence_count"] == 1
    # Fixed bucket vocabulary only: raw event_type strings never leave.
    assert metadata["event_type_buckets"] == {"speech": 1}
    assert metadata["event_type_bucket_schema"] == list(GUARDIAN_EVENT_BUCKET_SCHEMA)
    assert metadata["first_occurred_at"] == (_NOW + timedelta(seconds=2)).isoformat()
    assert metadata["last_occurred_at"] == (_NOW + timedelta(seconds=2)).isoformat()
    assert metadata["verbatim_content_included"] is False
    assert metadata["excluded_counts"] == {
        "unattributed": 1,
        "other_subject": 7,
        "other_account": 0,
        "retention_withheld": 0,
        "unparsed_payload": 0,
    }
    assert metadata["withheld_reason"]
    consent = exported["sections"]["consent"]
    assert [row["consent_id"] for row in consent["subject_person_consents"]] == [
        "consent-child"
    ]
    assert list(consent["subject_person_consents"][0]) == list(
        SUBJECT_PERSON_CONSENT_FIELDS
    )
    assert "consent_grants" not in consent
    assert "consent-archive" not in _serialized(exported)
    serialized = _serialized(exported)
    for text in (
        "孩子说的这句话。",
        "主人自己的话。",
        "模型的回复。",
        "没有主体归属的旧话轮。",
        RAW_CONVERSATION_TEXT,
    ):
        assert text not in serialized
    assert not _contains_key(exported, "payload")
    assert not _contains_key(exported, "payload_json")
    assert "evidence_events" not in serialized
    omissions = {
        entry["name"]: entry["reason_code"] for entry in exported["omitted_sections"]
    }
    assert omissions["evidence"] == "audience_forbidden"
    assert omissions["conversation"] == "audience_forbidden"
    assert set(omissions) == {
        "evidence",
        "conversation",
        "archive",
        "speaker",
        "evolution",
        "legacy",
        "guardian",
        "account",
        "account_consents",
        "persona",
    }


@pytest.mark.asyncio
async def test_active_link_and_weekly_report_consent_do_not_authorize_verbatim(
    tmp_path: Path,
) -> None:
    guardian = SqliteGuardianStore(tmp_path / "guardian.sqlite3")
    guardian.initialize()
    await guardian.create_link(
        link_id=GUARDIAN_LINK_ID,
        guardian_user_id=GUARDIAN_ACCOUNT,
        minor_user_id=ACCOUNT_ID,
        relation="parent",
        verified_via="wechat_identity",
        binding_code_hash=BINDING_CODE_HASH,
        binding_expires_at=_NOW + timedelta(days=1),
        now=_NOW,
    )
    await guardian.confirm_link(
        link_id=GUARDIAN_LINK_ID,
        minor_user_id=ACCOUNT_ID,
        binding_code_hash=BINDING_CODE_HASH,
        now=_NOW,
    )
    assert (
        await guardian.active_link(
            guardian_user_id=GUARDIAN_ACCOUNT,
            minor_user_id=ACCOUNT_ID,
        )
        is not None
    )
    await guardian.grant_consent(
        ConsentRecord(
            consent_id="consent-weekly-report",
            link_id=GUARDIAN_LINK_ID,
            consent_kind="weekly_report",
            policy_version="guardian-weekly-v1",
            granted_at=_NOW,
            evidence_event_id="evidence-owner",
        ),
        actor_user_id=GUARDIAN_ACCOUNT,
    )
    governance = await _governance(tmp_path, guardian=guardian)

    exported = await governance.export_account(
        ACCOUNT_ID,
        subject_id=CHILD_SUBJECT,
        audience="guardian",
    )

    serialized = _serialized(exported)
    for text in ("孩子说的这句话。", RAW_CONVERSATION_TEXT):
        assert text not in serialized
    assert GUARDIAN_LINK_ID not in serialized
    assert "items" not in exported["sections"].get("evidence", {})
    assert "links" not in exported["sections"]
    omissions = {
        entry["name"]: entry["reason_code"] for entry in exported["omitted_sections"]
    }
    assert omissions["guardian"] == "audience_forbidden"


@pytest.mark.asyncio
async def test_subject_export_manifest_covers_the_reduced_body(tmp_path: Path) -> None:
    governance = await _governance(tmp_path)

    guardian_export = await governance.export_account(
        ACCOUNT_ID,
        subject_id=ACCOUNT_ID,
        audience="guardian",
    )
    guardian_manifest = guardian_export.pop("manifest_sha256")
    assert guardian_manifest == _canonical_manifest(guardian_export)

    self_export = await governance.export_account(
        ACCOUNT_ID,
        subject_id=ACCOUNT_ID,
        audience="self",
    )
    self_manifest = self_export.pop("manifest_sha256")
    assert self_manifest == _canonical_manifest(self_export)
    assert self_manifest != guardian_manifest


@pytest.mark.asyncio
async def test_unscoped_export_keeps_the_internal_account_snapshot(tmp_path: Path) -> None:
    governance = await _governance(tmp_path)

    exported = await governance.export_account(ACCOUNT_ID)

    assert exported["format_version"] == 1
    assert "scope" not in exported
    assert "omitted_sections" not in exported
    assert set(exported["sections"]) == {
        "conversation",
        "archive",
        "speaker",
        "evolution",
        "legacy",
        "guardian",
    }
    evidence_rows = exported["sections"]["archive"]["evidence_events"]
    assert {row["event_id"] for row in evidence_rows} == {
        "evidence-owner",
        "evidence-assistant",
        "evidence-child",
        "evidence-legacy",
        "evidence-ephemeral",
        "evidence-nested-ephemeral",
        "evidence-unparsed",
        "evidence-session-unflagged",
        "evidence-internal-unflagged",
    }
    assert exported["sections"]["conversation"]["messages"][0]["text"] == (
        RAW_CONVERSATION_TEXT
    )
    manifest = exported.pop("manifest_sha256")
    assert manifest == _canonical_manifest(exported)


@pytest.mark.asyncio
async def test_export_fails_closed_without_an_explicit_subject(tmp_path: Path) -> None:
    governance = await _governance(tmp_path)

    with pytest.raises(ValueError, match="explicit subject_id"):
        await governance.export_account(ACCOUNT_ID, audience="guardian")
    with pytest.raises(ValueError, match="must not be blank"):
        await governance.export_account(ACCOUNT_ID, subject_id="   ")
    with pytest.raises(ValueError, match="unsupported export audience"):
        await governance.export_account(
            ACCOUNT_ID,
            subject_id=ACCOUNT_ID,
            audience="public",  # type: ignore[arg-type]
        )


def test_postgres_archive_key_spellings_are_reduced_the_same_way() -> None:
    snapshot: dict[str, Any] = {
        "format_version": 1,
        "generated_at": "2026-09-18T04:00:00+00:00",
        "account_id": ACCOUNT_ID,
        "sections": {
            "conversation": {
                "profile": {
                    "user_id": ACCOUNT_ID,
                    "display_name": "本人",
                    "bio": "简介里可能引用别人的内容。",
                    "avatar_url": "https://cdn.test/avatar.png",
                    "phone_number_masked": "138****0000",
                    "internal_flag": True,
                },
                "account": {"user_id": ACCOUNT_ID, "username": "pg-owner"},
                "messages": [{"text": RAW_CONVERSATION_TEXT}],
                "daily_summaries": [],
            },
            "archive": {
                "archive_evidence_events": [
                    {
                        "event_id": "pg-owner",
                        "subject_id": ACCOUNT_ID,
                        "speaker_class": "owner",
                        "event_type": "speech.utterance_finalized",
                        "occurred_at": "2026-09-18T04:00:00+00:00",
                        "payload": {"text": "PG 主人"},
                    },
                    {
                        "event_id": "pg-null",
                        "subject_id": None,
                        "speaker_class": "owner",
                        "event_type": "speech.utterance_finalized",
                        "occurred_at": "2026-09-18T04:00:01+00:00",
                        "payload": {"text": "PG 无主体"},
                    },
                    {
                        "event_id": "pg-foreign-account",
                        "account_id": "account-elsewhere",
                        "subject_id": ACCOUNT_ID,
                        "speaker_class": "owner",
                        "event_type": "speech.utterance_finalized",
                        "occurred_at": "2026-09-18T04:00:02+00:00",
                        "payload": {"text": "PG 其他账户"},
                    },
                ],
                "archive_consent_grants": [
                    {
                        "consent_grant_id": "pg-consent",
                        "account_id": ACCOUNT_ID,
                        "purpose": "raw_voice",
                        "granted_at": "2026-09-18T04:00:00+00:00",
                        "internal_note": "不该导出",
                    }
                ],
            },
            "speaker": {"speaker_identities": [{"label": "owner"}]},
            "evolution": {"evolution_signals": [{"diagnosis": "secret"}]},
            "legacy": {"grants": [{"grant_id": "legacy-1"}]},
            "guardian": {"links": [{"link_id": "guardian-link-1"}]},
        },
        "manifest_sha256": "0" * 64,
    }

    exported = build_subject_export(
        snapshot=snapshot,
        account_id=ACCOUNT_ID,
        subject_id=ACCOUNT_ID,
        audience="self",
    )

    assert [item["event_id"] for item in exported["sections"]["evidence"]["items"]] == [
        "pg-owner"
    ]
    assert exported["sections"]["evidence"]["excluded_counts"] == {
        "unattributed": 1,
        "other_subject": 0,
        "other_account": 1,
        "retention_withheld": 0,
        "unparsed_payload": 0,
    }
    assert exported["sections"]["consent"]["consent_grants"] == [
        {
            "consent_grant_id": "pg-consent",
            "purpose": "raw_voice",
            "granted_at": "2026-09-18T04:00:00+00:00",
        }
    ]
    assert exported["sections"]["consent"]["subject_person_consents"] == []
    assert set(exported["sections"]) == {"account", "consent", "evidence", "persona"}
    assert exported["sections"]["account"]["profile"] == {
        "user_id": ACCOUNT_ID,
        "display_name": "本人",
    }
    assert exported["sections"]["account"]["account"] == {
        "user_id": ACCOUNT_ID,
        "username": "pg-owner",
    }
    serialized = _serialized(exported)
    for text in (
        RAW_CONVERSATION_TEXT,
        "PG 无主体",
        "secret",
        "legacy-1",
        "guardian-link-1",
        "简介里可能引用别人的内容。",
        "https://cdn.test/avatar.png",
        "138****0000",
        "internal_flag",
        "internal_note",
        "不该导出",
        "PG 其他账户",
    ):
        assert text not in serialized
    assert exported["manifest_sha256"] != "0" * 64
    assert exported["manifest_sha256"] == _canonical_manifest(
        {key: value for key, value in exported.items() if key != "manifest_sha256"}
    )


def _person_consent(*, consent_id: str, subject: str) -> PersonConsentRecord:
    return PersonConsentRecord(
        consent_id=consent_id,
        subject_person_id=subject,
        grantor_person_id=ACCOUNT_ID,
        consent_kind="memory_retention",
        policy_version="minor-memory-v1",
        granted_at=_NOW,
        evidence_event_id=f"evidence-{consent_id}",
    )


def _persona_snapshot() -> dict[str, Any]:
    trait = {
        "account_id": ACCOUNT_ID,
        "category": "verbal_tic",
        "context": "conversation",
        "counterexample": "",
        "confidence": 0.9,
        "observation_count": 3,
        "normalized_key": "wo-jue-de",
        "created_at": "2026-09-20T00:00:00+00:00",
        "updated_at": "2026-09-21T00:00:00+00:00",
        "review_event_id": "internal-review",
    }
    return {
        "format_version": 1,
        "generated_at": "2026-09-26T00:00:00+00:00",
        "account_id": ACCOUNT_ID,
        "sections": {
            "conversation": {"profile": {"user_id": ACCOUNT_ID}, "account": {}},
            "archive": {
                "persona_traits": [
                    {**trait, "trait_id": "holder-trait", "subject_id": ACCOUNT_ID,
                     "description": "本人习惯先说“我觉得”", "status": "confirmed"},
                    # Learned before the per-person persona: the account holder's.
                    {**trait, "trait_id": "legacy-trait", "description": "本人旧特征",
                     "status": "candidate"},
                    {**trait, "trait_id": "child-trait", "subject_id": "child-person",
                     "description": "孩子习惯说“我们再想想”", "status": "confirmed"},
                    {**trait, "trait_id": "foreign-trait", "account_id": "elsewhere",
                     "subject_id": ACCOUNT_ID, "description": "别的账号", "status": "confirmed"},
                ],
                "persona_versions": [
                    {"version_id": "holder-v1", "account_id": ACCOUNT_ID,
                     "subject_id": ACCOUNT_ID, "version_number": 1, "status": "active",
                     "reason": "auto", "snapshot": [{"trait_id": "holder-trait"}],
                     "created_at": "2026-09-21T00:00:00+00:00"},
                    {"version_id": "child-v2", "account_id": ACCOUNT_ID,
                     "subject_id": "child-person", "version_number": 2, "status": "active",
                     "reason": "auto", "snapshot": [{"trait_id": "child-trait"}],
                     "created_at": "2026-09-22T00:00:00+00:00"},
                ],
                "speech_style_stats": [
                    {"account_id": ACCOUNT_ID, "subject_id": "child-person",
                     "scene": "conversation", "utterance_count": 12, "tic_counts": {"嗯": 3}},
                ],
            },
        },
    }


def test_self_export_releases_only_the_subjects_own_persona() -> None:
    exported = build_subject_export(
        snapshot=_persona_snapshot(),
        account_id=ACCOUNT_ID,
        subject_id=ACCOUNT_ID,
        audience="self",
    )

    persona = exported["sections"]["persona"]
    assert {row["trait_id"] for row in persona["traits"]} == {"holder-trait", "legacy-trait"}
    assert [row["version_id"] for row in persona["versions"]] == ["holder-v1"]
    assert persona["style_stats"] == []
    assert persona["annotation"]["ai_generated"] is True
    # Internal columns stay behind the whitelist.
    assert all("review_event_id" not in row and "normalized_key" not in row
               for row in persona["traits"])
    serialized = json.dumps(exported, ensure_ascii=False)
    assert "孩子习惯说" not in serialized
    assert "别的账号" not in serialized


def test_guardian_export_reports_the_childs_persona_without_descriptions() -> None:
    exported = build_subject_export(
        snapshot=_persona_snapshot(),
        account_id=ACCOUNT_ID,
        subject_id="child-person",
        audience="guardian",
    )

    assert "persona" not in exported["sections"]
    assert exported["sections"]["persona_metadata"] == {
        "subject_id": "child-person",
        "trait_count": 1,
        "trait_status_counts": {"confirmed": 1},
        "version_count": 1,
        "active_version_number": 2,
        "descriptions_included": False,
    }
    serialized = json.dumps(exported, ensure_ascii=False)
    assert "我们再想想" not in serialized
    assert "我觉得" not in serialized
    omissions = {entry["name"]: entry["reason_code"] for entry in exported["omitted_sections"]}
    assert omissions["persona"] == "audience_forbidden"
