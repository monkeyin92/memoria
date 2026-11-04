#!/usr/bin/env python3
"""Isolation restore + deletion-ledger replay drill (A15).

Lab-only. Reuses ``SubjectDeletionLedger`` export/merge/replay and the
restore-before-reads order in ``docs/runbooks.md`` (A15 section). Does not
invent a second backup system, touch production, or claim A14 copy sign-off.

    uv run python -m scripts.run_isolation_restore_deletion_drill \
      --report /tmp/a15-drill-report.json \
      --inventory docs/ops/current-runtime-inventory.json

Exit 0 when deleted-subject surfaces stay empty after restore+replay and the
kept subject remains present. Exit 1 on assertion failure or incomplete replay.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.archive.object_store import ObjectNotFoundError, ObjectRef
from services.governance.subject_deletion import SubjectDeletionLedger, SubjectDeletionService
from services.governance.subject_ports import SubjectScope

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "docs" / "ops" / "current-runtime-inventory.json"
DELETE_SCOPE = SubjectScope(account_id="lab-parent", subject_id="lab-child-deleted")
KEEP_SUBJECT_ID = "lab-child-kept"

PHASES: tuple[str, ...] = (
    "seed",
    "delete_subject",
    "export_ledger",
    "simulate_restore",
    "merge_and_replay",
    "assert_surfaces",
    "write_report",
)


def _ref(key: str) -> ObjectRef:
    return ObjectRef(
        account_id=DELETE_SCOPE.account_id,
        object_key=key,
        media_type="audio/wav",
        byte_count=1,
        content_sha256="0" * 64,
        encryption_key_version="v1",
        backend="archive",
    )


@dataclass
class _Archive:
    owner_events: set[str] = field(default_factory=set)
    subject_events: set[str] = field(default_factory=set)
    vectors: int = 0
    deleted_events: int = 0

    async def subject_event_ids(self, scope: SubjectScope) -> tuple[str, ...]:
        return tuple(sorted(self.owner_events))

    async def subject_account_event_ids(self, subject_id: str) -> tuple[str, ...]:
        return tuple(sorted(self.subject_events))

    async def object_references_for(
        self, *, account_id: str, event_ids: tuple[str, ...]
    ) -> tuple[ObjectRef, ...]:
        if account_id == DELETE_SCOPE.account_id and "e1" in event_ids:
            return (_ref("blob-e1"),)
        return ()

    async def delete_events(
        self,
        *,
        account_id: str,
        event_ids: tuple[str, ...],
        subject_id: str | None = None,
    ) -> dict[str, int]:
        target = self.owner_events if account_id == DELETE_SCOPE.account_id else self.subject_events
        removed = len(target & set(event_ids))
        target -= set(event_ids)
        self.deleted_events += removed
        return {"evidence_events": removed}

    async def remaining_rows_for(
        self, *, account_id: str, event_ids: tuple[str, ...], subject_id: str | None
    ) -> dict[str, int]:
        target = self.owner_events if account_id == DELETE_SCOPE.account_id else self.subject_events
        left = len(target & set(event_ids))
        return {"evidence_events": left} if left else {}

    async def delete_subject_vectors(self, *, account_id: str, subject_id: str) -> int:
        removed, self.vectors = self.vectors, 0
        return removed


@dataclass
class _Guardian:
    rows: int = 2
    tutor_events: tuple[str, ...] = ("tutor-1",)

    async def subject_tutor_event_ids(self, *, account_id: str, subject_id: str) -> tuple[str, ...]:
        return self.tutor_events

    async def delete_subject_rows(self, *, account_id: str, subject_id: str) -> dict[str, int]:
        deleted, self.rows = self.rows, 0
        return {"crisis_events": deleted}

    async def remaining_subject_rows(self, *, account_id: str, subject_id: str) -> dict[str, int]:
        return {"crisis_events": self.rows} if self.rows else {}


@dataclass
class _MemoryScope:
    leftover: int = 0

    async def erase_subject(self, *, subject_id: str) -> dict[str, int]:
        return {"memory_records": 3}

    async def remaining_subject_rows(self, *, subject_id: str) -> dict[str, int]:
        return {"memory_records": self.leftover} if self.leftover else {}


@dataclass
class _Objects:
    stored: set[str] = field(default_factory=lambda: {"blob-e1"})

    async def delete(self, reference: ObjectRef) -> None:
        self.stored.discard(reference.object_key)

    async def get(self, reference: ObjectRef) -> bytes:
        if reference.object_key in self.stored:
            return b"x"
        raise ObjectNotFoundError(reference.object_key)


class _Persona:
    def __init__(self) -> None:
        self.forgotten: list[tuple[str, str]] = []
        self.leftover: dict[str, int] = {}

    async def forget_subject(self, *, account_id: str, subject_id: str) -> int:
        self.forgotten.append((account_id, subject_id))
        return 4

    async def remaining_subject_rows(self, *, account_id: str, subject_id: str) -> dict[str, int]:
        return dict(self.leftover)


def _git_head() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_inventory(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "present": False,
            "path": None,
            "unmet": "inventory path not requested",
            "components": [],
        }
    if not path.is_file():
        return {
            "present": False,
            "path": str(path),
            "unmet": "A12 inventory missing (open PR #539 / docs/ops/current-runtime-inventory.json)",
            "components": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    components = []
    for row in payload.get("components", []):
        if not isinstance(row, dict):
            continue
        components.append(
            {
                "role": row.get("role"),
                "image_ref": row.get("image_ref"),
                "image_revision": row.get("image_revision"),
                "stack_commit": row.get("stack_commit"),
            }
        )
    return {
        "present": True,
        "path": str(path),
        "sha256": _sha256_file(path),
        "evidence_class": payload.get("evidence_class"),
        "schema_version": payload.get("schema_version"),
        "stack": payload.get("stack"),
        "components": components,
        "rebuild_note": (
            "Lab drill cites inventory roles/tags as the service rebuild checklist. "
            "Full docker compose rebuild against live digests is unmet without Monkey auth."
        ),
    }


def _service(tmp_path: Path, **overrides: Any) -> tuple[SubjectDeletionService, dict[str, Any]]:
    ledger = SubjectDeletionLedger(tmp_path / "control.sqlite3")
    ledger.initialize()
    parts: dict[str, Any] = {
        "ledger": ledger,
        "archive": _Archive(
            owner_events={"e1", "e2"},
            subject_events={"crisis-1"},
            vectors=2,
        ),
        "object_store": _Objects(),
        "guardian": _Guardian(),
        "memory_scope": _MemoryScope(),
        "sessions": [],
        "corpus": [],
        "chat_usage": [],
        "redacted": [],
        "persona": _Persona(),
        "keep_recall": {"owner_events": {"keep-e1"}, "parent_visible": True},
    }
    parts.update(overrides)

    async def terminate(subject_id: str) -> int:
        parts["sessions"].append(subject_id)
        return 1

    async def purge(subject_id: str) -> int:
        parts["corpus"].append(subject_id)
        return 0

    async def purge_chat_usage(subject_id: str) -> int:
        parts["chat_usage"].append(subject_id)
        return 2

    async def redact(subject_id: str, account_id: str) -> None:
        parts["redacted"].append((subject_id, account_id))

    service = SubjectDeletionService(
        ledger=parts["ledger"],
        archive=parts["archive"],
        object_store=parts["object_store"],
        guardian=parts["guardian"],
        memory_scope=parts["memory_scope"],
        terminate_sessions=terminate,
        purge_corpus=purge,
        purge_chat_usage=purge_chat_usage,
        redact_identity=redact,
        persona=parts["persona"],
    )
    return service, parts


def _surface_snapshot(parts: dict[str, Any]) -> dict[str, Any]:
    archive: _Archive = parts["archive"]
    guardian: _Guardian = parts["guardian"]
    memory: _MemoryScope = parts["memory_scope"]
    persona: _Persona = parts["persona"]
    objects: _Objects = parts["object_store"]
    keep = parts["keep_recall"]
    return {
        "child_recall": {
            "owner_events": sorted(archive.owner_events),
            "subject_events": sorted(archive.subject_events),
            "vectors": archive.vectors,
        },
        "parent_ui": {
            "guardian_rows": guardian.rows,
            "memory_records": memory.leftover,
            "object_keys": sorted(objects.stored),
        },
        "caches": {
            "sessions_terminated_for": list(parts["sessions"]),
            "persona_forgotten": [list(item) for item in persona.forgotten],
            "corpus_purged_for": list(parts["corpus"]),
            "chat_usage_purged_for": list(parts["chat_usage"]),
        },
        "keep_subject": {
            "subject_id": KEEP_SUBJECT_ID,
            "owner_events": sorted(keep["owner_events"]),
            "parent_visible": bool(keep["parent_visible"]),
        },
    }


async def run_drill(
    *,
    work_dir: Path,
    inventory_path: Path | None,
    report_path: Path,
) -> dict[str, Any]:
    """Run one isolation drill and write a machine-readable report."""

    started = time.perf_counter()
    started_at = datetime.now(UTC).isoformat()
    phase_log: list[dict[str, Any]] = []
    failure_points: list[str] = []
    unmet: list[str] = [
        "A14 product-copy matrix still open (Notion A14 进行中); deletion UI claims 未验",
        "No claim of 全部不可恢复 while backups retain copies (A14/compliance)",
        "Production / offsite restore RTO 未验 (no Monkey auth; no prod restore this card)",
        "Full docker service rebuild from live image digests 未验 (lab cites A12 inventory only)",
        "Online-ledger-unavailable window (post-backup deletions) remains a known gap in runbooks",
        "Hold #514 / #448 and child-facing audio: out of scope",
    ]

    def mark(phase: str, detail: dict[str, Any] | None = None) -> None:
        phase_log.append({"phase": phase, "at": datetime.now(UTC).isoformat(), "detail": detail or {}})

    inventory = _load_inventory(inventory_path)
    if not inventory["present"]:
        unmet.append(str(inventory.get("unmet") or "A12 inventory unavailable"))

    service, parts = _service(work_dir)
    mark("seed", {"delete_scope": DELETE_SCOPE.subject_id, "keep_subject": KEEP_SUBJECT_ID})

    delete_result = await service.delete_subject(DELETE_SCOPE)
    if delete_result.get("status") != "completed":
        failure_points.append("delete_subject did not complete")
    mark(
        "delete_subject",
        {
            "status": delete_result.get("status"),
            "surfaces": _surface_snapshot(parts),
        },
    )

    snapshot = work_dir / "deletion-ledger.json"
    exported = service.ledger.export_completed(snapshot)
    mark("export_ledger", {"exported": exported, "mode": 0o600 & snapshot.stat().st_mode})

    # Restore from a backup taken before the deletion completed.
    parts["archive"].owner_events |= {"e1", "e2", "e3-restored"}
    parts["archive"].subject_events |= {"crisis-1"}
    parts["archive"].vectors = 2
    parts["object_store"].stored.add("blob-e1")
    parts["guardian"].rows = 2
    parts["memory_scope"].leftover = 0
    restored_ledger = SubjectDeletionLedger(work_dir / "restored-control.sqlite3")
    restored_ledger.initialize()
    if restored_ledger.completed():
        failure_points.append("restored ledger was not empty before merge")
    mark(
        "simulate_restore",
        {
            "resurrected_owner_events": sorted(parts["archive"].owner_events),
            "ledger_completed_before_merge": len(restored_ledger.completed()),
        },
    )

    merge = restored_ledger.merge_completed(snapshot)
    restored, restored_parts = _service(
        work_dir / "restored-app",
        ledger=restored_ledger,
        archive=parts["archive"],
        object_store=parts["object_store"],
        guardian=parts["guardian"],
        memory_scope=parts["memory_scope"],
        keep_recall=parts["keep_recall"],
    )
    replay = await restored.replay_completed_deletions()
    mark(
        "merge_and_replay",
        {
            "merge": merge,
            "replay": replay,
            "reads_opened": False,
        },
    )
    if int(replay.get("incomplete", 1)) != 0:
        failure_points.append(f"replay incomplete={replay.get('incomplete')}")

    surfaces = _surface_snapshot(restored_parts)
    assertions: dict[str, bool] = {
        "deleted_child_recall_empty": surfaces["child_recall"]["owner_events"] == []
        and surfaces["child_recall"]["subject_events"] == []
        and surfaces["child_recall"]["vectors"] == 0,
        "deleted_parent_ui_empty": surfaces["parent_ui"]["guardian_rows"] == 0
        and surfaces["parent_ui"]["object_keys"] == [],
        "deleted_not_in_active_sessions_fence": restored.is_subject_deleting(
            account_id=DELETE_SCOPE.account_id, subject_id=DELETE_SCOPE.subject_id
        )
        is False,
        "keep_subject_still_visible": surfaces["keep_subject"]["owner_events"] == ["keep-e1"]
        and surfaces["keep_subject"]["parent_visible"] is True,
        "caches_record_deleted_subject": DELETE_SCOPE.subject_id
        in surfaces["caches"]["sessions_terminated_for"],
    }
    for name, ok in assertions.items():
        if not ok:
            failure_points.append(f"assertion failed: {name}")
    mark("assert_surfaces", {"assertions": assertions, "surfaces": surfaces})

    finished = time.perf_counter()
    report: dict[str, Any] = {
        "schema_version": 1,
        "drill": "a15-isolation-restore-deletion-ledger-replay",
        "passed": not failure_points,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "rto_seconds": round(finished - started, 3),
        "git_head": _git_head(),
        "phases": list(PHASES),
        "phase_log": phase_log,
        "inventory": inventory,
        "ledger": {
            "exported": exported,
            "merge": merge,
            "replay": replay,
            "snapshot_sha256": _sha256_file(snapshot) if snapshot.is_file() else None,
        },
        "assertions": assertions,
        "surfaces_after_replay": surfaces,
        "failure_points": failure_points,
        "rollback": {
            "lab_only": True,
            "action": "discard work_dir; no production state mutated",
            "work_dir": str(work_dir),
        },
        "consistency": {
            "incomplete": int(replay.get("incomplete", -1)),
            "replayed": int(replay.get("replayed", -1)),
            "keep_subject_intact": assertions["keep_subject_still_visible"],
        },
        "a14_gaps": [
            {"item": "deletion-stage product copy matrix", "status": "未验"},
            {"item": "UI never claims 全部不可恢复 while backups retain copies", "status": "未验"},
            {"item": "backup retention / external processor presentation", "status": "未验"},
        ],
        "unmet": unmet,
        "holds": ["#514", "#448"],
        "child_audio": "none",
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(report_path)
    report_sha = _sha256_file(report_path)
    mark("write_report", {"report": str(report_path), "sha256": report_sha})
    report["report_sha256"] = report_sha
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(report_path)
    report["report_sha256"] = _sha256_file(report_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Lab directory (default: a fresh temp dir under the process tmp)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
        help="Path for the JSON drill report (mode 0600)",
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=DEFAULT_INVENTORY,
        help="A12 runtime inventory JSON (default: docs/ops/current-runtime-inventory.json)",
    )
    parser.add_argument(
        "--allow-missing-inventory",
        action="store_true",
        help="Continue when the A12 inventory file is absent (records unmet)",
    )
    args = parser.parse_args(argv)

    inventory_path: Path | None = args.inventory
    if inventory_path is not None and not inventory_path.is_file():
        if not args.allow_missing_inventory:
            print(
                json.dumps(
                    {
                        "error": "inventory_missing",
                        "path": str(inventory_path),
                        "hint": "pass --allow-missing-inventory or land A12 (#539)",
                    }
                ),
                file=sys.stderr,
            )
            return 2
    work_dir = args.work_dir
    if work_dir is None:
        import tempfile

        work_dir = Path(tempfile.mkdtemp(prefix="a15-isolation-restore-"))
    else:
        work_dir.mkdir(parents=True, exist_ok=True)

    report = asyncio.run(
        run_drill(
            work_dir=work_dir,
            inventory_path=inventory_path,
            report_path=args.report,
        )
    )
    print(json.dumps({"passed": report["passed"], "report": str(args.report), "rto_seconds": report["rto_seconds"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
