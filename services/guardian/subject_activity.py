"""Idle-clock touches for the 180-day child_archive retention window."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal, cast

import asyncpg

ActivitySource = Literal["dialogue", "miniprogram"]


class SubjectActivityStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=1, max_size=2, command_timeout=30
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            await self.initialize()
        assert self._pool is not None
        return self._pool

    async def touch(
        self,
        *,
        subject_person_id: str,
        source: ActivitySource,
        now: datetime | None = None,
    ) -> datetime:
        stamped = now or datetime.now(UTC)
        async with (await self._require_pool()).acquire() as connection:
            await connection.execute(
                """
                INSERT INTO guardian_subject_activity (
                    subject_person_id, last_active_at, last_source, updated_at
                ) VALUES ($1, $2, $3, $2)
                ON CONFLICT (subject_person_id) DO UPDATE
                SET last_active_at = EXCLUDED.last_active_at,
                    last_source = EXCLUDED.last_source,
                    updated_at = EXCLUDED.updated_at
                """,
                subject_person_id,
                stamped,
                source,
            )
        return stamped

    async def last_active(self, *, subject_person_id: str) -> datetime | None:
        async with (await self._require_pool()).acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT last_active_at FROM guardian_subject_activity
                WHERE subject_person_id = $1
                """,
                subject_person_id,
            )
        return cast(datetime | None, value)

    async def last_active_for(
        self, subject_person_ids: Sequence[str]
    ) -> dict[str, datetime]:
        async with (await self._require_pool()).acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT subject_person_id, last_active_at
                FROM guardian_subject_activity
                WHERE subject_person_id = ANY($1::text[])
                """,
                list(subject_person_ids),
            )
        return {str(row["subject_person_id"]): row["last_active_at"] for row in rows}


__all__ = ["ActivitySource", "SubjectActivityStore"]
