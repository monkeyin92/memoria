"""Crisis outbox, notification reads, and WeChat push subscription ledger.

Mixin methods for ``PostgresGuardianStore``. Kept in a sibling module so the
main store stays under the module line budget. Behavior is unchanged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast

import asyncpg

from services.guardian.crisis import (
    CrisisNotificationReceipt,
    GuardianNotification,
    NotificationStatus,
    UnreadNotifications,
)
from services.guardian.domain import (
    GuardianAccessDeniedError,
    GuardianConflictError,
    GuardianNotFoundError,
)
from services.guardian.parent_alert import parse_alert_template
from services.guardian.push import (
    MAX_PUSH_SUBSCRIPTION_BALANCE,
    CrisisPushOutcome,
    CrisisReachBreakdown,
    PendingCrisisPush,
    PushSubscription,
    PushSubscriptionEvent,
    PushSubscriptionResult,
    crisis_reach_breakdown,
    push_identifier,
    push_openid,
    push_template_id,
    validate_crisis_push_claim,
    validate_crisis_push_outcome,
    validate_push_subscription_result,
)

_NOTIFICATION_SELECT = """
SELECT outbox.*, crisis.minor_user_id, crisis.possibly_story_retell,
       crisis.reply_template_id, reads.read_at
FROM guardian_notification_outbox outbox
JOIN guardian_crisis_events crisis
  ON crisis.crisis_event_id = outbox.crisis_event_id
LEFT JOIN guardian_notification_reads reads
  ON reads.notification_id = outbox.notification_id
WHERE outbox.guardian_user_id = $1
"""

_UNREAD_SELECT = (
    "SELECT unread.*, count(*) OVER () AS unread_total FROM ("
    + _NOTIFICATION_SELECT
    + """  AND reads.notification_id IS NULL
) unread
ORDER BY unread.created_at DESC, unread.notification_id DESC
LIMIT 1
"""
)


def _timestamp(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _uuid(value: str, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


class PostgresGuardianCrisisMixin:
    """PostgreSQL persistence for guardian crisis alerts and push subscribe.

    Pool and RLS helpers are implemented on ``PostgresGuardianStore``.
    """

    _maintenance_dsn: str | None
    _worker_dsn: str | None

    async def _ready_pool(self) -> asyncpg.Pool:
        raise NotImplementedError

    async def _ready_role_pool(
        self,
        *,
        dsn: str | None,
        pool_attr: str,
        expected_role: str,
        operation: str,
    ) -> asyncpg.Pool:
        raise NotImplementedError

    @staticmethod
    async def _set_api_context(
        connection: asyncpg.Connection,
        *,
        actor_user_id: str,
        subject_user_id: str,
    ) -> None:
        raise NotImplementedError

    @staticmethod
    def _notification(row: asyncpg.Record) -> GuardianNotification:
        return GuardianNotification(
            notification_id=str(row["notification_id"]),
            crisis_event_id=str(row["crisis_event_id"]),
            guardian_user_id=str(row["guardian_user_id"]),
            minor_user_id=str(row["minor_user_id"]),
            channel="wechat_subscription",
            status=cast(NotificationStatus, str(row["status"])),
            attempts=int(row["attempts"]),
            created_at=cast(datetime, row["created_at"]),
            reply_template_id=parse_alert_template(str(row["reply_template_id"])),
            read_at=cast(datetime | None, row["read_at"]),
            delivered_at=cast(datetime | None, row["delivered_at"]),
            last_error_code=(
                str(row["last_error_code"])
                if row["last_error_code"] is not None
                else None
            ),
            possibly_story_retell=bool(row["possibly_story_retell"])
            if "possibly_story_retell" in row.keys()
            else False,
        )

    async def enqueue_crisis_event(
        self,
        *,
        crisis_event_id: str,
        evidence_event_id: str,
        minor_user_id: str,
        occurred_at: datetime,
        script_version: str,
        reply_template_id: str,
        declared_guardian_ids: tuple[str, ...] = (),
        attempt_push: bool = True,
        possibly_story_retell: bool = False,
    ) -> CrisisNotificationReceipt:
        event_uuid = _uuid(crisis_event_id, field="crisis_event_id")
        occurred = _timestamp(occurred_at, field="occurred_at")
        created = datetime.now(UTC)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_api_context(
                connection,
                actor_user_id=minor_user_id,
                subject_user_id=minor_user_id,
            )
            row = await connection.fetchrow(
                """
                INSERT INTO guardian_crisis_events(
                    crisis_event_id, evidence_event_id, minor_user_id,
                    occurred_at, script_version, reply_template_id,
                    created_at, attempt_push, possibly_story_retell
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT(crisis_event_id) DO NOTHING
                RETURNING *
                """,
                event_uuid,
                evidence_event_id,
                minor_user_id,
                occurred,
                script_version,
                reply_template_id,
                created,
                attempt_push,
                possibly_story_retell,
            )
            if row is None:
                row = await connection.fetchrow(
                    "SELECT * FROM guardian_crisis_events WHERE crisis_event_id = $1",
                    event_uuid,
                )
            if row is None:  # pragma: no cover
                raise RuntimeError("guardian crisis event disappeared")
            if (
                str(row["evidence_event_id"]) != evidence_event_id
                or str(row["minor_user_id"]) != minor_user_id
                or str(row["script_version"]) != script_version
                or str(row["reply_template_id"]) != reply_template_id
                or bool(row["attempt_push"]) is not attempt_push
                or bool(row["possibly_story_retell"]) is not possibly_story_retell
            ):
                raise GuardianConflictError("crisis event id is immutable")
            guardians = await connection.fetch(
                """
                SELECT DISTINCT guardian_user_id FROM guardian_links
                WHERE minor_user_id = $1 AND status = 'active'
                """,
                minor_user_id,
            )
            linked_guardians = {
                str(guardian["guardian_user_id"]) for guardian in guardians
            }
            targets = set(linked_guardians)
            # Declared guardians are a separate, honestly labelled basis: the
            # caller resolved them from the Identity authority because the
            # subject has no account to confirm a guardian link. They never
            # become an active link and never unlock consent-gated capability,
            # and the database re-validates the declaration itself instead of
            # trusting the caller's id.
            targets.update(
                guardian_id
                for guardian_id in declared_guardian_ids
                if guardian_id and guardian_id != minor_user_id
            )
            for guardian_user_id in sorted(targets):
                notification_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"memoria:guardian-crisis:{event_uuid}:{guardian_user_id}",
                )
                if guardian_user_id in linked_guardians:
                    await connection.execute(
                        """
                        SELECT guardian_enqueue_notification($1, $2, $3, $4)
                        """,
                        notification_id,
                        event_uuid,
                        guardian_user_id,
                        created,
                    )
                else:
                    await connection.execute(
                        """
                        SELECT guardian_enqueue_declared_notification($1, $2, $3, $4)
                        """,
                        notification_id,
                        event_uuid,
                        guardian_user_id,
                        created,
                    )
            notification_count = int(
                await connection.fetchval(
                    """
                    SELECT count(*) FROM guardian_notification_outbox
                    WHERE crisis_event_id = $1
                    """,
                    event_uuid,
                )
            )
        return CrisisNotificationReceipt(
            crisis_event_id=str(event_uuid),
            evidence_event_id=evidence_event_id,
            minor_user_id=minor_user_id,
            occurred_at=cast(datetime, row["occurred_at"]),
            script_version=script_version,
            reply_template_id=reply_template_id,
            notification_count=notification_count,
        )

    async def guardian_notifications(
        self,
        *,
        guardian_user_id: str,
        limit: int = 50,
    ) -> tuple[GuardianNotification, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("notification limit must be between 1 and 100")
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=guardian_user_id,
                    subject_user_id=guardian_user_id,
                )
                rows = await connection.fetch(
                    _NOTIFICATION_SELECT
                    + "ORDER BY outbox.created_at DESC, outbox.notification_id DESC LIMIT $2",
                    guardian_user_id,
                    limit,
                )
        return tuple(self._notification(row) for row in rows)

    async def guardian_notification(
        self,
        *,
        guardian_user_id: str,
        notification_id: str,
    ) -> GuardianNotification:
        notification_uuid = _uuid(notification_id, field="notification_id")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_api_context(
                connection,
                actor_user_id=guardian_user_id,
                subject_user_id=guardian_user_id,
            )
            row = await connection.fetchrow(
                _NOTIFICATION_SELECT + "AND outbox.notification_id = $2",
                guardian_user_id,
                notification_uuid,
            )
        if row is None:
            raise GuardianNotFoundError("guardian notification not found")
        return self._notification(row)

    async def unread_guardian_notifications(
        self,
        *,
        guardian_user_id: str,
    ) -> UnreadNotifications | None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_api_context(
                connection,
                actor_user_id=guardian_user_id,
                subject_user_id=guardian_user_id,
            )
            row = await connection.fetchrow(_UNREAD_SELECT, guardian_user_id)
        if row is None:
            return None
        return UnreadNotifications(
            newest=self._notification(row),
            others_count=int(row["unread_total"]) - 1,
        )

    async def mark_guardian_notification_read(
        self,
        *,
        guardian_user_id: str,
        notification_id: str,
        now: datetime,
    ) -> datetime:
        notification_uuid = _uuid(notification_id, field="notification_id")
        timestamp = _timestamp(now, field="now")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_api_context(
                connection,
                actor_user_id=guardian_user_id,
                subject_user_id=guardian_user_id,
            )
            read_at = await connection.fetchval(
                """
                INSERT INTO guardian_notification_reads(
                    notification_id, guardian_user_id, read_at
                )
                SELECT outbox.notification_id, outbox.guardian_user_id, $3
                FROM guardian_notification_outbox outbox
                WHERE outbox.notification_id = $1 AND outbox.guardian_user_id = $2
                ON CONFLICT(notification_id) DO NOTHING
                RETURNING read_at
                """,
                notification_uuid,
                guardian_user_id,
                timestamp,
            )
            if read_at is None:
                # A new statement sees a receipt a concurrent tap just committed.
                read_at = await connection.fetchval(
                    """
                    SELECT reads.read_at
                    FROM guardian_notification_reads reads
                    JOIN guardian_notification_outbox outbox
                      USING (notification_id, guardian_user_id)
                    WHERE reads.notification_id = $1 AND reads.guardian_user_id = $2
                    """,
                    notification_uuid,
                    guardian_user_id,
                )
        if read_at is None:
            raise GuardianNotFoundError("guardian notification not found")
        return cast(datetime, read_at)

    @staticmethod
    async def _export_guardian_notifications(
        connection: asyncpg.Connection,
        *,
        account_id: str,
    ) -> dict[str, list[dict[str, object]]]:
        notifications = await connection.fetch(
            """
            SELECT outbox.* FROM guardian_notification_outbox outbox
            JOIN guardian_crisis_events crisis
              ON crisis.crisis_event_id = outbox.crisis_event_id
            WHERE outbox.guardian_user_id = $1 OR crisis.minor_user_id = $1
            ORDER BY outbox.created_at, outbox.notification_id
            """,
            account_id,
        )
        reads = await connection.fetch(
            """
            SELECT notification_id, guardian_user_id, read_at
            FROM guardian_notification_reads
            WHERE guardian_user_id = $1
            ORDER BY read_at, notification_id
            """,
            account_id,
        )
        return {
            "guardian_notifications": [dict(row) for row in notifications],
            "guardian_notification_reads": [dict(row) for row in reads],
        }

    @staticmethod
    def _push_subscription(row: asyncpg.Record) -> PushSubscription:
        return PushSubscription(
            guardian_user_id=str(row["guardian_user_id"]),
            template_id=str(row["template_id"]),
            remaining=int(row["remaining"]),
            last_result=cast(PushSubscriptionResult, str(row["last_result"])),
            updated_at=cast(datetime, row["updated_at"]),
        )

    async def record_push_subscription(
        self,
        *,
        guardian_user_id: str,
        template_id: str,
        result: PushSubscriptionResult,
        openid: str | None,
        now: datetime,
    ) -> PushSubscription:
        validate_push_subscription_result(result)
        guardian = push_identifier(guardian_user_id, field="guardian_user_id")
        template = push_template_id(template_id)
        clean_openid = push_openid(openid) if openid is not None else None
        if result == "accept" and clean_openid is None:
            raise ValueError("an accepted push subscription requires the guardian openid")
        timestamp = _timestamp(now, field="now")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_api_context(
                connection,
                actor_user_id=guardian,
                subject_user_id=guardian,
            )
            row = await connection.fetchrow(
                """
                INSERT INTO guardian_push_subscriptions(
                    guardian_user_id, template_id, openid, remaining,
                    last_result, created_at, updated_at
                ) VALUES (
                    $1, $2, $3, CASE WHEN $4 = 'accept' THEN 1 ELSE 0 END, $4, $5, $5
                )
                ON CONFLICT(guardian_user_id, template_id) DO UPDATE SET
                    openid = COALESCE(EXCLUDED.openid, guardian_push_subscriptions.openid),
                    remaining = CASE EXCLUDED.last_result
                        WHEN 'accept'
                        THEN LEAST(guardian_push_subscriptions.remaining + 1, $6)
                        WHEN 'ban' THEN 0
                        WHEN 'opt_out' THEN 0
                        ELSE guardian_push_subscriptions.remaining
                    END,
                    last_result = EXCLUDED.last_result,
                    updated_at = EXCLUDED.updated_at
                RETURNING guardian_user_id, template_id, remaining, last_result, updated_at
                """,
                guardian,
                template,
                clean_openid,
                result,
                timestamp,
                MAX_PUSH_SUBSCRIPTION_BALANCE,
            )
            if row is None:  # pragma: no cover
                raise RuntimeError("guardian push subscription disappeared")
            await connection.execute(
                """
                INSERT INTO guardian_push_subscription_events(
                    event_id, guardian_user_id, template_id, result,
                    remaining_after, recorded_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
                """,
                uuid.uuid4(),
                guardian,
                template,
                result,
                int(row["remaining"]),
                timestamp,
            )
        return self._push_subscription(row)

    async def push_subscription_events(
        self,
        *,
        guardian_user_id: str,
        template_id: str | None = None,
        limit: int = 50,
    ) -> tuple[PushSubscriptionEvent, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("push subscription event limit must be between 1 and 100")
        guardian = push_identifier(guardian_user_id, field="guardian_user_id")
        template = push_template_id(template_id) if template_id is not None else None
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_api_context(
                connection,
                actor_user_id=guardian,
                subject_user_id=guardian,
            )
            if template is None:
                rows = await connection.fetch(
                    """
                    SELECT event_id, guardian_user_id, template_id, result,
                           remaining_after, recorded_at
                    FROM guardian_push_subscription_events
                    WHERE guardian_user_id = $1
                    ORDER BY recorded_at DESC, event_id DESC
                    LIMIT $2
                    """,
                    guardian,
                    limit,
                )
            else:
                rows = await connection.fetch(
                    """
                    SELECT event_id, guardian_user_id, template_id, result,
                           remaining_after, recorded_at
                    FROM guardian_push_subscription_events
                    WHERE guardian_user_id = $1 AND template_id = $2
                    ORDER BY recorded_at DESC, event_id DESC
                    LIMIT $3
                    """,
                    guardian,
                    template,
                    limit,
                )
        return tuple(
            PushSubscriptionEvent(
                event_id=str(row["event_id"]),
                guardian_user_id=str(row["guardian_user_id"]),
                template_id=str(row["template_id"]),
                result=cast(PushSubscriptionResult, str(row["result"])),
                remaining_after=int(row["remaining_after"]),
                recorded_at=cast(datetime, row["recorded_at"]),
            )
            for row in rows
        )

    async def push_subscription(
        self,
        *,
        guardian_user_id: str,
        template_id: str,
    ) -> PushSubscription | None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_api_context(
                connection,
                actor_user_id=guardian_user_id,
                subject_user_id=guardian_user_id,
            )
            row = await connection.fetchrow(
                """
                SELECT guardian_user_id, template_id, remaining, last_result, updated_at
                FROM guardian_push_subscriptions
                WHERE guardian_user_id = $1 AND template_id = $2
                """,
                guardian_user_id,
                template_id,
            )
        return self._push_subscription(row) if row is not None else None

    async def crisis_reach_for_window(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> CrisisReachBreakdown:
        """Count outbox statuses created in ``[since, until)`` for recruit gates."""

        start = _timestamp(since, field="since")
        end = _timestamp(until, field="until")
        if end <= start:
            raise ValueError("crisis reach window requires until > since")
        maintenance_pool = await self._ready_role_pool(
            dsn=self._maintenance_dsn,
            pool_attr="_maintenance_pool",
            expected_role="memoria_guardian_maintenance",
            operation="crisis reach accounting",
        )
        async with maintenance_pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT set_config('memoria.guardian_maintenance_scope', '1', true)"
            )
            rows = await connection.fetch(
                """
                SELECT status
                FROM guardian_notification_outbox
                WHERE created_at >= $1 AND created_at < $2
                ORDER BY created_at, notification_id
                """,
                start,
                end,
            )
        return crisis_reach_breakdown(tuple(str(row["status"]) for row in rows))

    async def _crisis_push_pool(self) -> asyncpg.Pool:
        return await self._ready_role_pool(
            dsn=self._worker_dsn,
            pool_attr="_worker_pool",
            expected_role="memoria_guardian_worker",
            operation="crisis push worker",
        )

    async def claim_crisis_pushes(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 20,
        lease_s: int = 60,
        max_attempts: int = 5,
        max_age_s: int = 86_400,
    ) -> tuple[PendingCrisisPush, ...]:
        validate_crisis_push_claim(
            worker_id=worker_id,
            limit=limit,
            lease_s=lease_s,
            max_attempts=max_attempts,
            max_age_s=max_age_s,
        )
        pool = await self._crisis_push_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT * FROM guardian_crisis_push_claim($1, $2, $3, $4, $5, $6)",
                worker_id,
                _timestamp(now, field="now"),
                limit,
                lease_s,
                max_attempts,
                max_age_s,
            )
        return tuple(
            PendingCrisisPush(
                notification_id=str(row["notification_id"]),
                crisis_event_id=str(row["crisis_event_id"]),
                guardian_user_id=str(row["guardian_user_id"]),
                minor_user_id=str(row["minor_user_id"]),
                occurred_at=cast(datetime, row["occurred_at"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        )

    async def reserve_crisis_push_subscription(
        self,
        *,
        notification_id: str,
        worker_id: str,
        template_id: str,
        now: datetime,
    ) -> str | None:
        pool = await self._crisis_push_pool()
        async with pool.acquire() as connection:
            try:
                openid = await connection.fetchval(
                    "SELECT guardian_crisis_push_reserve($1, $2, $3, $4)",
                    _uuid(notification_id, field="notification_id"),
                    worker_id,
                    push_template_id(template_id),
                    _timestamp(now, field="now"),
                )
            except asyncpg.RaiseError as exc:
                raise GuardianAccessDeniedError("crisis push claim is not held") from exc
        return str(openid) if openid is not None else None

    async def complete_crisis_push(
        self,
        *,
        notification_id: str,
        worker_id: str,
        outcome: CrisisPushOutcome,
        error_code: str | None,
        retry_delay_s: int | None,
        exhaust_subscription: bool,
        now: datetime,
    ) -> bool:
        validate_crisis_push_outcome(
            outcome,
            error_code=error_code,
            retry_delay_s=retry_delay_s,
        )
        pool = await self._crisis_push_pool()
        async with pool.acquire() as connection:
            settled = await connection.fetchval(
                "SELECT guardian_crisis_push_complete($1, $2, $3, $4, $5, $6, $7)",
                _uuid(notification_id, field="notification_id"),
                worker_id,
                outcome,
                error_code,
                retry_delay_s if outcome == "retry" else None,
                exhaust_subscription,
                _timestamp(now, field="now"),
            )
        return bool(settled)
