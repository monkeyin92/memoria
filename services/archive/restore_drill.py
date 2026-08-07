"""Joint-restore checks for encrypted archive objects."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import asyncpg

from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.object_store import EncryptedLocalObjectStore, ObjectRef
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.archive.postgres_skill_catalog import PostgresSkillCatalog
from services.governance.lifecycle_tables import (
    POSTGRES_AUTHORITATIVE_ACCOUNT_TABLES,
    POSTGRES_PROJECTION_ACCOUNT_TABLES,
)

_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_AUTHORITATIVE_TABLES = POSTGRES_AUTHORITATIVE_ACCOUNT_TABLES
_PROJECTION_TABLES = POSTGRES_PROJECTION_ACCOUNT_TABLES
_ORPHAN_QUERIES: Mapping[str, tuple[tuple[str, ...], str]] = {
    "archive_blobs_without_event": (
        ("archive_evidence_blobs", "archive_evidence_events"),
        """
        SELECT count(*) FROM archive_evidence_blobs child
        LEFT JOIN archive_evidence_events parent
          ON parent.event_id = child.evidence_event_id
        WHERE parent.event_id IS NULL
        """,
    ),
    "memory_claims_without_event": (
        ("memory_claims", "archive_evidence_events"),
        """
        SELECT count(*) FROM memory_claims child
        LEFT JOIN archive_evidence_events parent
          ON parent.event_id = child.source_event_id
        WHERE parent.event_id IS NULL
        """,
    ),
    "relationships_without_person_or_event": (
        ("relationships", "person_entities", "archive_evidence_events"),
        """
        SELECT count(*) FROM relationships child
        LEFT JOIN person_entities person ON person.person_id = child.person_id
        LEFT JOIN archive_evidence_events event
          ON event.event_id = child.source_event_id
        WHERE person.person_id IS NULL OR event.event_id IS NULL
        """,
    ),
    "timeline_without_episode_or_event": (
        ("timeline_entries", "life_episodes", "archive_evidence_events"),
        """
        SELECT count(*) FROM timeline_entries child
        LEFT JOIN life_episodes episode ON episode.episode_id = child.episode_id
        LEFT JOIN archive_evidence_events event
          ON event.event_id = child.source_event_id
        WHERE episode.episode_id IS NULL OR event.event_id IS NULL
        """,
    ),
    "skill_versions_without_definition": (
        ("skill_versions", "skill_definitions"),
        """
        SELECT count(*) FROM skill_versions child
        LEFT JOIN skill_definitions parent ON parent.skill_id = child.skill_id
        WHERE parent.skill_id IS NULL
        """,
    ),
    "skill_evidence_without_version_or_event": (
        (
            "skill_version_evidence",
            "skill_versions",
            "archive_evidence_events",
        ),
        """
        SELECT count(*) FROM skill_version_evidence child
        LEFT JOIN skill_versions version
          ON version.skill_id = child.skill_id
         AND version.version = child.version
        LEFT JOIN archive_evidence_events event
          ON event.event_id = child.source_event_id
        WHERE version.skill_id IS NULL OR event.event_id IS NULL
        """,
    ),
    "skill_runs_without_version_or_confirmation": (
        ("skill_runs", "skill_versions", "archive_evidence_events"),
        """
        SELECT count(*) FROM skill_runs child
        LEFT JOIN skill_versions version
          ON version.skill_id = child.skill_id
         AND version.version = child.skill_version
        LEFT JOIN archive_evidence_events event
          ON event.event_id = child.confirmation_event_id
        WHERE version.skill_id IS NULL OR event.event_id IS NULL
        """,
    ),
    "skill_steps_without_run": (
        ("skill_run_steps", "skill_runs"),
        """
        SELECT count(*) FROM skill_run_steps child
        LEFT JOIN skill_runs parent ON parent.run_id = child.run_id
        WHERE parent.run_id IS NULL
        """,
    ),
    "persona_evidence_without_trait_or_event": (
        ("persona_evidence", "persona_traits", "archive_evidence_events"),
        """
        SELECT count(*) FROM persona_evidence child
        LEFT JOIN persona_traits trait ON trait.trait_id = child.trait_id
        LEFT JOIN archive_evidence_events event
          ON event.event_id = child.source_event_id
        WHERE trait.trait_id IS NULL OR event.event_id IS NULL
        """,
    ),
    "speaker_profiles_without_identity": (
        ("speaker_profiles", "speaker_identities"),
        """
        SELECT count(*) FROM speaker_profiles child
        LEFT JOIN speaker_identities parent
          ON parent.identity_id = child.identity_id
        WHERE parent.identity_id IS NULL
        """,
    ),
    "speaker_samples_without_profile": (
        ("speaker_enrollment_samples", "speaker_profiles"),
        """
        SELECT count(*) FROM speaker_enrollment_samples child
        LEFT JOIN speaker_profiles parent ON parent.profile_id = child.profile_id
        WHERE parent.profile_id IS NULL
        """,
    ),
    "voice_samples_without_consent": (
        ("voice_samples", "voice_clone_consents"),
        """
        SELECT count(*) FROM voice_samples child
        LEFT JOIN voice_clone_consents parent
          ON parent.account_id = child.account_id
        WHERE parent.account_id IS NULL
        """,
    ),
    "voice_profiles_without_sample": (
        ("voice_profiles", "voice_samples"),
        """
        SELECT count(*) FROM voice_profiles child
        LEFT JOIN voice_samples parent
          ON parent.sample_id = child.sample_id
         AND parent.account_id = child.account_id
        WHERE parent.sample_id IS NULL
        """,
    ),
    "voice_enrollment_operations_without_consent": (
        ("voice_enrollment_operations", "voice_clone_consents"),
        """
        SELECT count(*) FROM voice_enrollment_operations child
        LEFT JOIN voice_clone_consents parent
          ON parent.account_id = child.account_id
        WHERE parent.account_id IS NULL
        """,
    ),
    "voice_blind_trials_without_profile": (
        ("voice_blind_trials", "voice_profiles"),
        """
        SELECT count(*) FROM voice_blind_trials child
        LEFT JOIN voice_profiles parent
          ON parent.profile_id = child.profile_id
         AND parent.account_id = child.account_id
        WHERE parent.profile_id IS NULL
        """,
    ),
    "voice_evaluations_without_profile": (
        ("voice_evaluations", "voice_profiles"),
        """
        SELECT count(*) FROM voice_evaluations child
        LEFT JOIN voice_profiles parent
          ON parent.profile_id = child.profile_id
         AND parent.account_id = child.account_id
        WHERE parent.profile_id IS NULL
        """,
    ),
    "voice_quality_measurements_without_profile": (
        ("voice_quality_measurements", "voice_profiles"),
        """
        SELECT count(*) FROM voice_quality_measurements child
        LEFT JOIN voice_profiles parent
          ON parent.profile_id = child.profile_id
         AND parent.account_id = child.account_id
        WHERE parent.profile_id IS NULL
        """,
    ),
}


@dataclass(frozen=True, slots=True)
class ObjectRestoreReport:
    object_count: int
    failed_count: int
    key_versions: tuple[str, ...]
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class LocalObjectRestorePlan:
    domain: str
    source_root: Path
    restore_root: Path
    keys: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.domain not in {"archive", "voice"}:
            raise ValueError("local object domain must be archive or voice")
        if self.source_root.expanduser().resolve() == self.restore_root.expanduser().resolve():
            raise ValueError("object restore root must differ from its source")


@dataclass(frozen=True, slots=True)
class PostgresSnapshot:
    tables: tuple[str, ...]
    counts: Mapping[str, int]
    event_manifest_sha256: str
    key_versions: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ProjectionRebuildReport:
    compiled_events: int = 0
    ignored_events: int = 0
    failed_events: int = 0
    skill_documents_rebuilt: int = 0


@dataclass(frozen=True, slots=True)
class RLSRestoreReport:
    passed: bool
    tables_checked: int
    account_scopes_checked: int
    failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PostgresRestoreDrillReport:
    started_at: str
    finished_at: str
    rto_seconds: float
    dump_byte_count: int
    dump_sha256: str
    source: PostgresSnapshot
    restored: PostgresSnapshot
    objects: Mapping[str, ObjectRestoreReport]
    projection_rebuild: ProjectionRebuildReport
    orphan_counts: Mapping[str, int]
    rls: RLSRestoreReport

    @property
    def passed(self) -> bool:
        return (
            self.source == self.restored
            and all(report.failed_count == 0 for report in self.objects.values())
            and self.projection_rebuild.failed_events == 0
            and all(count == 0 for count in self.orphan_counts.values())
            and self.rls.passed
        )

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["passed"] = self.passed
        return payload


def _path_below(root: Path, object_key: str) -> Path:
    path = (root / object_key).resolve()
    if not path.is_relative_to(root):
        raise ValueError("object key escapes configured root")
    return path


def _copy_ciphertext(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.restore.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _object_manifest_sha256(items: Iterable[ObjectRef]) -> str:
    manifest = json.dumps(
        [asdict(item) for item in sorted(items, key=lambda item: item.object_key)],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(manifest).hexdigest()


async def copy_and_verify_local_objects(
    *,
    source_root: Path,
    restore_root: Path,
    references: Iterable[ObjectRef],
    key: str,
    key_version: str,
) -> ObjectRestoreReport:
    """Copy encrypted objects and verify restored plaintext against the manifest."""

    source_root = source_root.expanduser().resolve()
    restore_root = restore_root.expanduser().resolve()
    items = tuple(references)
    verifier = EncryptedLocalObjectStore(
        root=restore_root,
        key=key,
        key_version=key_version,
    )
    failed_count = 0
    for reference in items:
        try:
            if reference.encryption_key_version != key_version:
                raise ValueError("object key version does not match the restore key")
            await asyncio.to_thread(
                _copy_ciphertext,
                _path_below(source_root, reference.object_key),
                _path_below(restore_root, reference.object_key),
            )
            await verifier.get(reference)
        except (OSError, RuntimeError, ValueError):
            failed_count += 1

    return ObjectRestoreReport(
        object_count=len(items),
        failed_count=failed_count,
        key_versions=tuple(sorted({item.encryption_key_version for item in items})),
        manifest_sha256=_object_manifest_sha256(items),
    )


async def _load_object_references(dsn: str, domain: str) -> tuple[ObjectRef, ...]:
    connection = await asyncpg.connect(dsn)
    try:
        present = await _public_tables(connection)
        if domain == "archive" and "archive_evidence_blobs" in present:
            rows = await connection.fetch(
                """
                SELECT account_id, object_key, media_type, byte_count,
                       content_sha256, encryption_key_version
                FROM archive_evidence_blobs ORDER BY object_key
                """
            )
            return tuple(
                ObjectRef(
                    account_id=str(row["account_id"]),
                    object_key=str(row["object_key"]),
                    media_type=str(row["media_type"]),
                    byte_count=int(row["byte_count"]),
                    content_sha256=str(row["content_sha256"]),
                    encryption_key_version=str(row["encryption_key_version"]),
                    backend="local",
                )
                for row in rows
            )
        if domain == "voice" and "voice_samples" in present:
            rows = await connection.fetch(
                """
                SELECT account_id, object_key, media_type, byte_count,
                       content_sha256, encryption_key_version, object_backend
                FROM voice_samples ORDER BY object_key
                """
            )
            return tuple(
                ObjectRef(
                    account_id=str(row["account_id"]),
                    object_key=str(row["object_key"]),
                    media_type=str(row["media_type"]),
                    byte_count=int(row["byte_count"]),
                    content_sha256=str(row["content_sha256"]),
                    encryption_key_version=str(row["encryption_key_version"]),
                    backend=str(row["object_backend"]),
                )
                for row in rows
            )
        return ()
    finally:
        await connection.close()


async def _restore_local_objects(
    plan: LocalObjectRestorePlan,
    references: tuple[ObjectRef, ...],
) -> ObjectRestoreReport:
    restore_root = plan.restore_root.expanduser().resolve()
    if restore_root.exists() and any(restore_root.iterdir()):
        raise FileExistsError(f"object restore root is not empty: {restore_root}")
    failed = sum(reference.backend != "local" for reference in references)
    local = tuple(reference for reference in references if reference.backend == "local")
    for key_version in sorted({item.encryption_key_version for item in local}):
        group = tuple(item for item in local if item.encryption_key_version == key_version)
        key = plan.keys.get(key_version)
        if key is None:
            failed += len(group)
            continue
        try:
            report = await copy_and_verify_local_objects(
                source_root=plan.source_root,
                restore_root=plan.restore_root,
                references=group,
                key=key,
                key_version=key_version,
            )
            failed += report.failed_count
        except (TypeError, ValueError):
            failed += len(group)
    return ObjectRestoreReport(
        object_count=len(references),
        failed_count=failed,
        key_versions=tuple(sorted({item.encryption_key_version for item in references})),
        manifest_sha256=_object_manifest_sha256(references),
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _database_dsn(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("restore DSN must use PostgreSQL")
    return urlunsplit(parsed._replace(path=f"/{database}"))


def _connection_identity(dsn: str) -> tuple[str, str]:
    parsed = urlsplit(dsn)
    database = unquote(parsed.path.lstrip("/"))
    username = unquote(parsed.username or "postgres")
    if not database or not username:
        raise ValueError("PostgreSQL DSN must include a database and user")
    return username, database


async def _public_tables(connection: asyncpg.Connection) -> set[str]:
    rows = await connection.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {str(row["tablename"]) for row in rows}


async def snapshot_postgres(dsn: str) -> PostgresSnapshot:
    connection = await asyncpg.connect(dsn)
    try:
        privilege = await connection.fetchrow(
            """
            SELECT rolsuper, rolbypassrls
            FROM pg_roles WHERE rolname = current_user
            """
        )
        if privilege is None or not (
            bool(privilege["rolsuper"]) or bool(privilege["rolbypassrls"])
        ):
            raise PermissionError("restore audit requires a BYPASSRLS database role")
        present = await _public_tables(connection)
        tables = tuple(table for table in _AUTHORITATIVE_TABLES if table in present)
        counts = {
            table: int(
                await connection.fetchval(f"SELECT count(*) FROM {_quote_identifier(table)}")
            )
            for table in tables
        }
        events = (
            await connection.fetch(
                """
                SELECT event_id, content_sha256
                FROM archive_evidence_events ORDER BY event_id
                """
            )
            if "archive_evidence_events" in present
            else ()
        )
        event_manifest = "\n".join(
            f"{row['event_id']}:{row['content_sha256']}" for row in events
        ).encode("utf-8")
        key_versions: dict[str, tuple[str, ...]] = {}
        for domain, table in (
            ("archive", "archive_evidence_blobs"),
            ("voice", "voice_samples"),
        ):
            if table in present:
                rows = await connection.fetch(
                    f"""
                    SELECT DISTINCT encryption_key_version
                    FROM {_quote_identifier(table)}
                    ORDER BY encryption_key_version
                    """
                )
                key_versions[domain] = tuple(str(row["encryption_key_version"]) for row in rows)
        return PostgresSnapshot(
            tables=tables,
            counts=counts,
            event_manifest_sha256=hashlib.sha256(event_manifest).hexdigest(),
            key_versions=key_versions,
        )
    finally:
        await connection.close()


async def rebuild_postgres_memory_projections(dsn: str) -> ProjectionRebuildReport:
    connection = await asyncpg.connect(dsn)
    skill_account_ids: tuple[str, ...] = ()
    try:
        present = await _public_tables(connection)
        projections = tuple(table for table in _PROJECTION_TABLES if table in present)
        if not projections or "archive_processing_outbox" not in present:
            raise RuntimeError("memory projection schema is incomplete")
        role = await connection.fetchrow(
            """
            SELECT rolsuper, rolbypassrls
            FROM pg_roles
            WHERE rolname = current_user
            """
        )
        if role is None or not (bool(role["rolsuper"]) or bool(role["rolbypassrls"])):
            raise PermissionError(
                "memory projection rebuild requires a maintenance role that bypasses RLS"
            )
        protected_identity_projections = (
            {
                "person_entities",
                "relationships",
            }
            if "self_model_relationship_profiles" in present
            else set()
        )
        if "skill_definitions" in present:
            skill_account_ids = tuple(
                str(row["account_id"])
                for row in await connection.fetch(
                    "SELECT DISTINCT account_id FROM skill_definitions ORDER BY account_id"
                )
            )
        async with connection.transaction():
            truncatable = tuple(
                table for table in projections if table not in protected_identity_projections
            )
            if truncatable:
                await connection.execute(
                    "TRUNCATE TABLE " + ", ".join(_quote_identifier(table) for table in truncatable)
                )
            if protected_identity_projections:
                await connection.execute(
                    """
                    DELETE FROM relationships relationship
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM self_model_relationship_profiles profile
                        WHERE profile.relationship_id = relationship.relationship_id
                    )
                    """
                )
                await connection.execute(
                    """
                    DELETE FROM person_entities person
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM self_model_relationship_profiles profile
                        WHERE profile.person_id = person.person_id
                    )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM relationships relationship
                          WHERE relationship.person_id = person.person_id
                      )
                    """
                )
            await connection.execute(
                """
                UPDATE archive_processing_outbox
                SET status = 'pending', attempts = 0, available_at = now(),
                    completed_at = NULL, last_error_code = NULL
                WHERE task_type = 'compile_evidence'
                """
            )
    finally:
        await connection.close()

    catalog = PostgresMemoryCatalog(dsn, extractor=RuleBasedMemoryExtractor())
    compiled = ignored = failed = 0
    try:
        for _ in range(100_000):
            report = await catalog.compile_pending(limit=1000)
            compiled += report.compiled_events
            ignored += report.ignored_events
            failed += report.failed_events
            if report.failed_events or not (report.compiled_events or report.ignored_events):
                break
        else:  # pragma: no cover - defensive bound for a corrupt outbox
            raise RuntimeError("projection rebuild exceeded its safety bound")
    finally:
        await catalog.close()
    connection = await asyncpg.connect(dsn)
    try:
        incomplete = int(
            await connection.fetchval(
                """
                SELECT count(*) FROM archive_processing_outbox
                WHERE task_type = 'compile_evidence' AND status != 'completed'
                """
            )
        )
    finally:
        await connection.close()
    if incomplete:
        raise RuntimeError(f"memory projection rebuild left {incomplete} incomplete events")
    skills = PostgresSkillCatalog(dsn)
    rebuilt_skills = 0
    try:
        for account_id in skill_account_ids:
            rebuilt_skills += await skills.rebuild_search_projections(account_id=account_id)
    finally:
        await skills.close()
    return ProjectionRebuildReport(
        compiled_events=compiled,
        ignored_events=ignored,
        failed_events=failed,
        skill_documents_rebuilt=rebuilt_skills,
    )


async def postgres_orphan_counts(dsn: str) -> dict[str, int]:
    connection = await asyncpg.connect(dsn)
    try:
        present = await _public_tables(connection)
        result = {
            name: int(await connection.fetchval(sql))
            for name, (tables, sql) in _ORPHAN_QUERIES.items()
            if set(tables) <= present
        }
        result["unvalidated_foreign_keys"] = int(
            await connection.fetchval(
                """
                SELECT count(*)
                FROM pg_constraint constraint_row
                JOIN pg_namespace namespace
                  ON namespace.oid = constraint_row.connamespace
                WHERE namespace.nspname = 'public'
                  AND constraint_row.contype = 'f'
                  AND NOT constraint_row.convalidated
                """
            )
        )
        return result
    finally:
        await connection.close()


async def verify_postgres_rls(dsn: str) -> RLSRestoreReport:
    connection = await asyncpg.connect(dsn)
    role = f"memoria_restore_audit_{os.urandom(6).hex()}"
    created = False
    try:
        rows = await connection.fetch(
            """
            SELECT DISTINCT class.relname
            FROM pg_class class
            JOIN pg_namespace namespace ON namespace.oid = class.relnamespace
            JOIN pg_attribute attribute ON attribute.attrelid = class.oid
            WHERE namespace.nspname = 'public'
              AND class.relkind IN ('r', 'p')
              AND class.relrowsecurity
              AND attribute.attname = 'account_id'
              AND NOT attribute.attisdropped
            ORDER BY class.relname
            """
        )
        tables = tuple(str(row["relname"]) for row in rows)
        expected: dict[str, tuple[tuple[str, int], ...]] = {}
        for table in tables:
            account_rows = await connection.fetch(
                f"""
                SELECT account_id, count(*) AS row_count
                FROM {_quote_identifier(table)}
                GROUP BY account_id ORDER BY account_id LIMIT 2
                """
            )
            expected[table] = tuple(
                (str(row["account_id"]), int(row["row_count"])) for row in account_rows
            )

        await connection.execute(
            f"CREATE ROLE {_quote_identifier(role)} NOLOGIN NOSUPERUSER NOBYPASSRLS"
        )
        created = True
        await connection.execute(f"GRANT USAGE ON SCHEMA public TO {_quote_identifier(role)}")
        for table in tables:
            await connection.execute(
                f"GRANT SELECT ON TABLE {_quote_identifier(table)} TO {_quote_identifier(role)}"
            )
        await connection.execute(f"SET ROLE {_quote_identifier(role)}")
        passed = True
        failures: list[str] = []
        scopes_checked = 0
        for table in tables:
            await connection.execute(
                "SELECT set_config('app.account_id', $1, false)",
                "__memoria_restore_no_account__",
            )
            visible = int(
                await connection.fetchval(f"SELECT count(*) FROM {_quote_identifier(table)}")
            )
            if visible != 0:
                passed = False
                failures.append(f"{table}:unscoped_visible")
            for scope_index, (account_id, expected_count) in enumerate(expected[table]):
                await connection.execute(
                    "SELECT set_config('app.account_id', $1, false)", account_id
                )
                visible_count = int(
                    await connection.fetchval(f"SELECT count(*) FROM {_quote_identifier(table)}")
                )
                visible_accounts = await connection.fetch(
                    f"""
                    SELECT DISTINCT account_id
                    FROM {_quote_identifier(table)} ORDER BY account_id
                    """
                )
                if visible_count != expected_count:
                    passed = False
                    failures.append(f"{table}:scope_{scope_index}:count")
                if {str(row["account_id"]) for row in visible_accounts} != {account_id}:
                    passed = False
                    failures.append(f"{table}:scope_{scope_index}:cross_account")
                scopes_checked += 1
        await connection.execute("RESET ROLE")
        return RLSRestoreReport(
            passed=passed,
            tables_checked=len(tables),
            account_scopes_checked=scopes_checked,
            failures=tuple(failures),
        )
    finally:
        with contextlib.suppress(Exception):
            await connection.execute("RESET ROLE")
        if created:
            with contextlib.suppress(Exception):
                await connection.execute(f"DROP OWNED BY {_quote_identifier(role)}")
            with contextlib.suppress(Exception):
                await connection.execute(f"DROP ROLE {_quote_identifier(role)}")
        await connection.close()


def _run_pg_dump(
    source_dsn: str,
    dump_path: Path,
    postgres_container: str | None,
) -> None:
    if dump_path.exists():
        raise FileExistsError(f"refusing to overwrite existing dump: {dump_path}")
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    username, database = _connection_identity(source_dsn)
    if postgres_container is not None:
        if not _CONTAINER_NAME.fullmatch(postgres_container):
            raise ValueError("invalid PostgreSQL container name")
        command = [
            "docker",
            "exec",
            postgres_container,
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--username",
            username,
            "--dbname",
            database,
        ]
    else:
        command = [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            source_dsn,
        ]
    temporary = dump_path.with_name(f".{dump_path.name}.{os.urandom(6).hex()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            completed = subprocess.run(
                command,
                stdout=destination,
                stderr=subprocess.PIPE,
                check=False,
            )
            destination.flush()
            os.fsync(destination.fileno())
        if completed.returncode != 0:
            raise RuntimeError(f"pg_dump failed with exit code {completed.returncode}")
        os.replace(temporary, dump_path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_pg_restore(
    restore_dsn: str,
    dump_path: Path,
    postgres_container: str | None,
) -> None:
    username, database = _connection_identity(restore_dsn)
    if postgres_container is not None:
        command = [
            "docker",
            "exec",
            "-i",
            postgres_container,
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-acl",
            "--username",
            username,
            "--dbname",
            database,
        ]
    else:
        command = [
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-acl",
            f"--dbname={restore_dsn}",
        ]
    with dump_path.open("rb") as source:
        completed = subprocess.run(
            command,
            stdin=source,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(f"pg_restore failed with exit code {completed.returncode}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _create_restore_database(admin_dsn: str, database: str) -> None:
    if not _DATABASE_NAME.fullmatch(database):
        raise ValueError("restore database must be a lowercase PostgreSQL identifier")
    connection = await asyncpg.connect(admin_dsn)
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database)
        if exists:
            raise FileExistsError(f"restore database already exists: {database}")
        await connection.execute(f"CREATE DATABASE {_quote_identifier(database)}")
    finally:
        await connection.close()


async def run_postgres_restore_drill(
    *,
    source_dsn: str,
    admin_dsn: str,
    restore_database: str,
    dump_path: Path,
    postgres_container: str | None = None,
    local_objects: Iterable[LocalObjectRestorePlan] = (),
) -> PostgresRestoreDrillReport:
    """Dump, restore, rebuild, and audit a fresh PostgreSQL database."""

    started_at = datetime.now(UTC)
    started_clock = time.monotonic()
    source = await snapshot_postgres(source_dsn)
    await asyncio.to_thread(_run_pg_dump, source_dsn, dump_path, postgres_container)
    if await snapshot_postgres(source_dsn) != source:
        raise RuntimeError("source database changed during the restore drill backup")
    await _create_restore_database(admin_dsn, restore_database)
    restore_dsn = _database_dsn(admin_dsn, restore_database)
    await asyncio.to_thread(_run_pg_restore, restore_dsn, dump_path, postgres_container)
    restored = await snapshot_postgres(restore_dsn)
    if restored != source:
        raise RuntimeError("restored authoritative PostgreSQL snapshot does not match source")
    object_plans = tuple(local_objects)
    plans = {plan.domain: plan for plan in object_plans}
    if len(plans) != len(object_plans):
        raise ValueError("each local object domain may appear only once")
    expected_objects = {
        "archive": source.counts.get("archive_evidence_blobs", 0),
        "voice": source.counts.get("voice_samples", 0),
    }
    missing = tuple(
        domain for domain, count in expected_objects.items() if count and domain not in plans
    )
    if missing:
        raise ValueError("object restore plans are required for: " + ", ".join(sorted(missing)))
    objects: dict[str, ObjectRestoreReport] = {}
    for domain, plan in plans.items():
        references = await _load_object_references(restore_dsn, domain)
        if len(references) != expected_objects[domain]:
            raise RuntimeError(f"{domain} object manifest count does not match PostgreSQL")
        objects[domain] = await _restore_local_objects(plan, references)
    projection_rebuild = await rebuild_postgres_memory_projections(restore_dsn)
    restored = await snapshot_postgres(restore_dsn)
    orphan_counts = await postgres_orphan_counts(restore_dsn)
    rls = await verify_postgres_rls(restore_dsn)
    finished_at = datetime.now(UTC)
    return PostgresRestoreDrillReport(
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        rto_seconds=round(time.monotonic() - started_clock, 3),
        dump_byte_count=dump_path.stat().st_size,
        dump_sha256=await asyncio.to_thread(_file_sha256, dump_path),
        source=source,
        restored=restored,
        objects=objects,
        projection_rebuild=projection_rebuild,
        orphan_counts=orphan_counts,
        rls=rls,
    )
