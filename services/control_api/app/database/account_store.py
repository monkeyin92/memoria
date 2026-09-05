"""Account identity, external identities, avatars, and deletion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from services.guardian.domain import (
    AgeEvidenceStatus,
    BirthYearBand,
    SubjectCategory,
    validate_subject_transition,
)


class ExternalIdentityConflictError(ValueError):
    pass


class AccountStoreMixin:
    """Accounts, external identities, avatars, and account deletion."""

    if TYPE_CHECKING:
        from contextlib import AbstractContextManager

        def _connection(self) -> AbstractContextManager[sqlite3.Connection]: ...

        @staticmethod
        def _ensure_profile(
            connection: sqlite3.Connection, user_id: str, now: str
        ) -> None: ...

        def revoke_all_auth_sessions(self, *, user_id: str, now: str) -> int: ...

        def get_profile(self, *, user_id: str, now: str) -> dict[str, Any]: ...

    def register_account(
        self,
        *,
        user_id: str,
        username: str,
        username_normalized: str,
        password_hash: str,
        display_name: str | None = None,
        now: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, now)
            if display_name:
                connection.execute(
                    """
                    UPDATE profiles
                    SET display_name = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (display_name, now, user_id),
                )
            connection.execute(
                """
                INSERT INTO accounts (
                    user_id, username, username_normalized, password_hash,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, username_normalized, password_hash, now, now),
            )
            row = connection.execute(
                """
                SELECT user_id, username, username_normalized, password_hash,
                       created_at, updated_at
                FROM accounts WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - inserted in the same transaction
            raise RuntimeError("account insert failed")
        return dict(row)

    def get_account_by_username(self, *, username_normalized: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT user_id, username, username_normalized, password_hash,
                       created_at, updated_at
                FROM accounts WHERE username_normalized = ?
                """,
                (username_normalized,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_account(self, *, user_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT user_id, username, username_normalized, password_hash,
                       created_at, updated_at
                FROM accounts WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT profiles.user_id, NULL AS username,
                           NULL AS username_normalized, '' AS password_hash,
                           MIN(external_identities.created_at) AS created_at,
                           MAX(external_identities.updated_at) AS updated_at
                    FROM profiles
                    JOIN external_identities
                      ON external_identities.user_id = profiles.user_id
                    WHERE profiles.user_id = ?
                    GROUP BY profiles.user_id
                    """,
                    (user_id,),
                ).fetchone()
        return dict(row) if row is not None else None

    def external_identity_user(
        self,
        *,
        provider: str,
        subject_hash: str,
    ) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT user_id FROM external_identities
                WHERE provider = ? AND subject_hash = ?
                """,
                (provider, subject_hash),
            ).fetchone()
        return str(row["user_id"]) if row is not None else None

    def has_external_identity(self, *, user_id: str, provider: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM external_identities
                WHERE user_id = ? AND provider = ?
                """,
                (user_id, provider),
            ).fetchone()
        return row is not None

    def external_identity_subject_hash(self, *, user_id: str, provider: str) -> str | None:
        """Server-only receipt input; never return this from a client route."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT subject_hash FROM external_identities WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            ).fetchone()
        return str(row["subject_hash"]) if row is not None else None

    def bind_external_identities(
        self,
        *,
        preferred_user_id: str,
        identities: Mapping[str, str],
        now: str,
    ) -> tuple[str, bool]:
        clean = {
            str(provider): str(subject_hash)
            for provider, subject_hash in identities.items()
            if str(provider) and str(subject_hash)
        }
        if not clean:
            raise ValueError("at least one external identity is required")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("(?, ?)" for _ in clean)
            parameters: list[str] = []
            for provider, subject_hash in clean.items():
                parameters.extend((provider, subject_hash))
            rows = connection.execute(
                f"""
                SELECT provider, subject_hash, user_id
                FROM external_identities
                WHERE (provider, subject_hash) IN ({placeholders})
                """,
                parameters,
            ).fetchall()
            resolved_users = {str(row["user_id"]) for row in rows}
            if len(resolved_users) > 1:
                raise ExternalIdentityConflictError("external identities belong to different users")
            user_id = next(iter(resolved_users), preferred_user_id)
            self._ensure_profile(connection, user_id, now)
            changed = False
            for provider, subject_hash in clean.items():
                existing_for_user = connection.execute(
                    """
                    SELECT subject_hash FROM external_identities
                    WHERE provider = ? AND user_id = ?
                    """,
                    (provider, user_id),
                ).fetchone()
                if (
                    existing_for_user is not None
                    and str(existing_for_user["subject_hash"]) != subject_hash
                ):
                    raise ExternalIdentityConflictError(
                        f"user already has a different {provider} identity"
                    )
                existing_subject = connection.execute(
                    """
                    SELECT user_id FROM external_identities
                    WHERE provider = ? AND subject_hash = ?
                    """,
                    (provider, subject_hash),
                ).fetchone()
                if existing_subject is not None:
                    if str(existing_subject["user_id"]) != user_id:
                        raise ExternalIdentityConflictError(
                            f"{provider} identity belongs to another user"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO external_identities (
                        provider, subject_hash, user_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (provider, subject_hash, user_id, now, now),
                )
                changed = True
        return user_id, changed

    def update_external_profile(
        self,
        *,
        user_id: str,
        display_name: str | None,
        phone_number_masked: str | None,
        now: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, now)
            current = connection.execute(
                """
                SELECT display_name, phone_number_masked
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if current is None:  # pragma: no cover
                raise RuntimeError("profile initialization failed")
            next_display_name = (
                display_name.strip()
                if display_name is not None and display_name.strip()
                else str(current["display_name"])
            )
            next_phone = (
                phone_number_masked.strip()
                if phone_number_masked is not None and phone_number_masked.strip()
                else str(current["phone_number_masked"])
            )
            connection.execute(
                """
                UPDATE profiles
                SET display_name = ?, phone_number_masked = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (next_display_name, next_phone, now, user_id),
            )
        return self.get_profile(user_id=user_id, now=now)

    def save_profile_avatar(
        self,
        *,
        user_id: str,
        public_id: str,
        content_type: str,
        content: bytes,
        sha256: str,
        avatar_url: str,
        now: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_profile(connection, user_id, now)
            connection.execute(
                """
                INSERT INTO profile_avatars (
                    user_id, public_id, content_type, content, sha256, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    public_id = excluded.public_id,
                    content_type = excluded.content_type,
                    content = excluded.content,
                    sha256 = excluded.sha256,
                    updated_at = excluded.updated_at
                """,
                (user_id, public_id, content_type, content, sha256, now),
            )
            connection.execute(
                "UPDATE profiles SET avatar_url = ?, updated_at = ? WHERE user_id = ?",
                (avatar_url, now, user_id),
            )
        avatar = self.get_profile_avatar(public_id=public_id)
        if avatar is None:  # pragma: no cover
            raise RuntimeError("profile avatar persistence failed")
        return avatar

    def get_profile_avatar(self, *, public_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT user_id, public_id, content_type, content, sha256, updated_at
                FROM profile_avatars WHERE public_id = ?
                """,
                (public_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _user_id_hash(user_id: str) -> str:
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()

    def is_account_deleted(self, *, user_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM account_deletions
                WHERE user_id_hash = ? AND status = 'completed'
                """,
                (self._user_id_hash(user_id),),
            ).fetchone()
        return row is not None

    def is_account_unavailable(self, *, user_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM account_deletions WHERE user_id_hash = ?",
                (self._user_id_hash(user_id),),
            ).fetchone()
        return row is not None

    def get_account_deletion(self, *, user_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT request_id, status, step, started_at, updated_at,
                       completed_at, progress_json, last_error,
                       deleted_counts_json
                FROM account_deletions WHERE user_id_hash = ?
                """,
                (self._user_id_hash(user_id),),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["progress"] = json.loads(str(result.pop("progress_json")))
        result["deleted_counts"] = json.loads(str(result.pop("deleted_counts_json")))
        return result

    def begin_account_deletion(self, *, user_id: str, started_at: str) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_id FROM account_deletions WHERE user_id_hash = ?",
                (self._user_id_hash(user_id),),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO account_deletions (
                        user_id_hash, user_id, request_id, status, step,
                        started_at, updated_at
                    ) VALUES (?, ?, ?, 'deleting', 'started', ?, ?)
                    """,
                    (
                        self._user_id_hash(user_id),
                        user_id,
                        str(uuid.uuid4()),
                        started_at,
                        started_at,
                    ),
                )
        deletion = self.get_account_deletion(user_id=user_id)
        if deletion is None:  # pragma: no cover - inserted/read in one local store
            raise RuntimeError("account deletion initialization failed")
        return deletion

    def update_account_deletion(
        self,
        *,
        user_id: str,
        request_id: str,
        step: str,
        updated_at: str,
        progress: Mapping[str, int] | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE account_deletions
                SET step = ?, updated_at = ?, progress_json = ?, last_error = ?
                WHERE user_id_hash = ? AND request_id = ? AND status = 'deleting'
                """,
                (
                    step,
                    updated_at,
                    json.dumps(progress or {}, sort_keys=True, separators=(",", ":")),
                    last_error,
                    self._user_id_hash(user_id),
                    request_id,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("account deletion request is not active")
        deletion = self.get_account_deletion(user_id=user_id)
        if deletion is None:  # pragma: no cover - updated/read in one local store
            raise RuntimeError("account deletion update failed")
        return deletion

    def pending_account_deletions(self, *, limit: int = 100) -> tuple[str, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("pending deletion limit must be between 1 and 1000")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT user_id FROM account_deletions
                WHERE status = 'deleting' AND user_id IS NOT NULL
                ORDER BY started_at LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(str(row["user_id"]) for row in rows)

    def export_account_data(self, *, user_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            profile = connection.execute(
                """
                SELECT user_id, display_name, bio, avatar_url, phone_number_masked,
                       companion_id, timezone,
                       auto_summary, voice_reply, gentle_reminders, reject_non_owner_voice,
                       subject_category, birth_year_band, age_evidence_status, subject_revision,
                       created_at, updated_at
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            account = connection.execute(
                """
                SELECT user_id, username, created_at, updated_at
                FROM accounts WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            messages = connection.execute(
                """
                SELECT id, role, text, emotion, local_date, created_at
                FROM messages WHERE user_id = ? ORDER BY id
                """,
                (user_id,),
            ).fetchall()
            summaries = connection.execute(
                """
                SELECT summary_date, content_json, source, message_count, generated_at
                FROM daily_summaries WHERE user_id = ? ORDER BY summary_date
                """,
                (user_id,),
            ).fetchall()
            sessions = connection.execute(
                """
                SELECT session_id, voice_backend, interaction_mode, session_focus,
                       mode_policy_version,
                       resource_owner_account_id,
                       digital_self_version_id, digital_self_manifest_sha256,
                       preview_grant_id, self_preview_perspective,
                       relationship_profile_id, relationship_profile_version,
                       legacy_grant_id, legacy_actor_role,
                       legacy_grantee_account_id, legacy_shell_id,
                       legacy_grant_snapshot_sha256, legacy_scope_sha256,
                       legacy_voice_allowed, legacy_expires_at,
                       companion_style_id, companion_style_version,
                       voice_profile_id, voice_profile_version, voice_provider,
                       voice_model, voice_resource_id, voice_provider_expires_at,
                       voice_speaker_sha256, fallback_voice_profile_id,
                       fallback_voice_provider, fallback_voice_model,
                       fallback_voice_resource_id,
                       learning_task_id, created_at
                FROM voice_sessions
                WHERE user_id = ? OR resource_owner_account_id = ?
                   OR legacy_grantee_account_id = ?
                ORDER BY created_at, session_id
                """,
                (user_id, user_id, user_id),
            ).fetchall()
            preview: dict[str, list[dict[str, Any]]] = {
                "grants": [],
                "feedback": [],
                "fidelity_evaluations": [],
                "fidelity_trials": [],
            }
            preview_tables = {
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name IN (
                        'digital_self_preview_grants',
                        'digital_self_preview_feedback',
                        'digital_self_fidelity_evaluations',
                        'digital_self_fidelity_trials'
                    )
                    """
                ).fetchall()
            }
            if "digital_self_preview_grants" in preview_tables:
                preview["grants"] = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT grant_id, version_id, manifest_sha256, perspective,
                               status, expires_at, created_at, used_at, revoked_at
                        FROM digital_self_preview_grants
                        WHERE account_id = ? ORDER BY created_at, grant_id
                        """,
                        (user_id,),
                    ).fetchall()
                ]
            if "digital_self_preview_feedback" in preview_tables:
                for row in connection.execute(
                    """
                    SELECT feedback_id, session_id, turn_id, generation_id,
                           tool_epoch, version_id, manifest_sha256, action,
                           target_source_event_ids_json, correction_text,
                           evidence_event_id, created_at
                    FROM digital_self_preview_feedback
                    WHERE account_id = ? ORDER BY created_at, feedback_id
                    """,
                    (user_id,),
                ).fetchall():
                    item = dict(row)
                    item["target_source_event_ids"] = json.loads(
                        str(item.pop("target_source_event_ids_json"))
                    )
                    preview["feedback"].append(item)
            if "digital_self_fidelity_evaluations" in preview_tables:
                preview["fidelity_evaluations"] = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT evaluation_id, version_id, manifest_sha256,
                               status, verdict, verdict_rationale,
                               created_at, completed_at
                        FROM digital_self_fidelity_evaluations
                        WHERE account_id = ? ORDER BY created_at, evaluation_id
                        """,
                        (user_id,),
                    ).fetchall()
                ]
            if "digital_self_fidelity_trials" in preview_tables:
                for row in connection.execute(
                    """
                    SELECT trial_id, evaluation_id, category, prompt,
                           generic_answer, digital_self_answer, digital_self_slot,
                           available, coverage_gap, epistemic_status, has_source,
                           unsupported_fact, decision_inference_disclosed,
                           privacy_refused, identity_disclosed, preferred_slot,
                           rationale, answered_at
                    FROM digital_self_fidelity_trials
                    WHERE account_id = ?
                    ORDER BY evaluation_id, category, trial_id
                    """,
                    (user_id,),
                ).fetchall():
                    item = dict(row)
                    digital_slot = str(item.pop("digital_self_slot"))
                    generic_answer = str(item.pop("generic_answer"))
                    digital_answer = str(item.pop("digital_self_answer"))
                    item["slot_a"] = digital_answer if digital_slot == "a" else generic_answer
                    item["slot_b"] = digital_answer if digital_slot == "b" else generic_answer
                    for key in (
                        "available",
                        "has_source",
                        "unsupported_fact",
                        "decision_inference_disclosed",
                        "privacy_refused",
                        "identity_disclosed",
                    ):
                        item[key] = bool(item[key])
                    preview["fidelity_trials"].append(item)
        profile_data = dict(profile) if profile is not None else None
        if profile_data is not None:
            for key in (
                "auto_summary",
                "voice_reply",
                "gentle_reminders",
                "reject_non_owner_voice",
            ):
                profile_data[key] = bool(profile_data[key])
        summary_data = []
        for row in summaries:
            item = dict(row)
            item["content"] = json.loads(str(item.pop("content_json")))
            summary_data.append(item)
        return {
            "profile": profile_data,
            "account": dict(account) if account is not None else None,
            "messages": [dict(row) for row in messages],
            "daily_summaries": summary_data,
            "voice_sessions": [dict(row) for row in sessions],
            "digital_self_preview": preview,
        }

    def finalize_account_deletion(
        self,
        *,
        user_id: str,
        request_id: str,
        completed_at: str,
        deleted_counts: Mapping[str, int],
    ) -> dict[str, int]:
        with self._connection() as connection:
            deletion = connection.execute(
                """
                SELECT request_id, status, deleted_counts_json
                FROM account_deletions WHERE user_id_hash = ?
                """,
                (self._user_id_hash(user_id),),
            ).fetchone()
            if deletion is None or str(deletion["request_id"]) != request_id:
                raise RuntimeError("account deletion request is not active")
            if str(deletion["status"]) == "completed":
                return {
                    str(key): int(value)
                    for key, value in json.loads(str(deletion["deleted_counts_json"])).items()
                }
            preview_delete_order = (
                "digital_self_fidelity_trials",
                "digital_self_fidelity_evaluations",
                "digital_self_preview_feedback",
                "digital_self_preview_grants",
            )
            existing_preview_tables = {
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name IN (
                        'digital_self_preview_grants',
                        'digital_self_preview_feedback',
                        'digital_self_fidelity_evaluations',
                        'digital_self_fidelity_trials'
                    )
                    """
                ).fetchall()
            }
            preview_counts: dict[str, int] = {}
            for table in preview_delete_order:
                if table not in existing_preview_tables:
                    continue
                preview_counts[table] = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE account_id = ?",
                        (user_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    f"DELETE FROM {table} WHERE account_id = ?",
                    (user_id,),
                )
            counts = {
                "messages": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM messages WHERE user_id = ?", (user_id,)
                    ).fetchone()[0]
                ),
                "daily_summaries": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM daily_summaries WHERE user_id = ?", (user_id,)
                    ).fetchone()[0]
                ),
                "voice_sessions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM voice_sessions WHERE user_id = ?", (user_id,)
                    ).fetchone()[0]
                ),
                "media_reply_delivery_events": int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM media_reply_delivery_events
                        WHERE session_id IN (
                            SELECT session_id FROM voice_sessions
                            WHERE user_id = ? OR resource_owner_account_id = ?
                               OR legacy_grantee_account_id = ?
                        )
                        """,
                        (user_id, user_id, user_id),
                    ).fetchone()[0]
                ),
                "auth_sessions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM auth_sessions WHERE user_id = ?", (user_id,)
                    ).fetchone()[0]
                ),
                "device_identities": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM device_identities WHERE account_id = ?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                "device_settings": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM device_settings WHERE updated_by = ?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                "device_runtime_profile_acks": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM device_runtime_profile_acks WHERE acked_by = ?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                "device_control_intents": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM device_control_intents WHERE requested_by = ?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                "device_media_sessions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM device_media_sessions WHERE subject_id = ?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                **preview_counts,
            }
            connection.execute("DELETE FROM device_identities WHERE account_id = ?", (user_id,))
            connection.execute("DELETE FROM device_settings WHERE updated_by = ?", (user_id,))
            connection.execute(
                "DELETE FROM device_runtime_profile_acks WHERE acked_by = ?", (user_id,)
            )
            connection.execute(
                "DELETE FROM device_control_intents WHERE requested_by = ?", (user_id,)
            )
            connection.execute(
                """
                DELETE FROM media_reply_delivery_events
                WHERE session_id IN (
                    SELECT session_id FROM voice_sessions
                    WHERE user_id = ? OR resource_owner_account_id = ?
                       OR legacy_grantee_account_id = ?
                )
                """,
                (user_id, user_id, user_id),
            )
            connection.execute("DELETE FROM device_media_sessions WHERE subject_id = ?", (user_id,))
            connection.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
            combined = {**deleted_counts, **counts}
            connection.execute(
                """
                UPDATE account_deletions
                SET user_id = NULL, status = 'completed', step = 'completed',
                    updated_at = ?, completed_at = ?, progress_json = '{}',
                    last_error = NULL, deleted_counts_json = ?
                WHERE user_id_hash = ? AND request_id = ? AND status = 'deleting'
                """,
                (
                    completed_at,
                    completed_at,
                    json.dumps(combined, sort_keys=True, separators=(",", ":")),
                    self._user_id_hash(user_id),
                    request_id,
                ),
            )
        return combined

    def update_subject_profile(
        self,
        *,
        user_id: str,
        subject_category: SubjectCategory,
        birth_year_band: BirthYearBand,
        age_evidence_status: AgeEvidenceStatus = "unverified",
        now: str,
        age_eligible: bool = False,
        guardian_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Apply the subject ratchet and invalidate every existing auth capability.

        Capability routes always re-read the profile, while revoking all auth
        sessions makes already-minted access and refresh tokens unusable on
        their next request.  The category, age band, revision, and revocation
        are committed in one SQLite transaction.
        """

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_profile(connection, user_id, now)
            current = connection.execute(
                """
                SELECT subject_category, birth_year_band, age_evidence_status, subject_revision
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if current is None:  # pragma: no cover
                raise RuntimeError("profile initialization failed")
            current_category = cast(SubjectCategory, str(current["subject_category"]))
            current_band = cast(BirthYearBand, str(current["birth_year_band"]))
            current_evidence = cast(
                AgeEvidenceStatus,
                str(current["age_evidence_status"]),
            )
            validate_subject_transition(
                current_category=current_category,
                current_birth_year_band=current_band,
                current_age_evidence_status=current_evidence,
                target_category=subject_category,
                target_birth_year_band=birth_year_band,
                target_age_evidence_status=age_evidence_status,
                age_eligible=age_eligible,
                guardian_confirmed=guardian_confirmed,
            )
            changed = (
                current_category != subject_category
                or current_band != birth_year_band
                or current_evidence != age_evidence_status
            )
            if changed:
                connection.execute(
                    """
                    UPDATE profiles
                    SET subject_category = ?, birth_year_band = ?, age_evidence_status = ?,
                        subject_revision = subject_revision + 1, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        subject_category,
                        birth_year_band,
                        age_evidence_status,
                        now,
                        user_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE auth_sessions
                    SET revoked_at = COALESCE(revoked_at, ?), updated_at = ?
                    WHERE user_id = ?
                    """,
                    (now, now, user_id),
                )
            row = connection.execute(
                """
                SELECT user_id, display_name, bio, avatar_url, phone_number_masked,
                       companion_id, timezone,
                       auto_summary, voice_reply, gentle_reminders, reject_non_owner_voice,
                       subject_category, birth_year_band, age_evidence_status, subject_revision,
                       created_at, updated_at
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:  # pragma: no cover
            raise RuntimeError("subject profile update failed")
        return dict(row)
