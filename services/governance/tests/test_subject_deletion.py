"""The subject deletion saga: ordered, checkpointed, fenced and verified."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from services.archive.object_store import ObjectNotFoundError, ObjectRef
from services.governance.subject_deletion import (
    SubjectDeletionIncompleteError,
    SubjectDeletionLedger,
    SubjectDeletionService,
)
from services.governance.subject_ports import SubjectScope

SCOPE = SubjectScope(account_id="parent", subject_id="child")


def _ref(key: str) -> ObjectRef:
    return ObjectRef(
        account_id="parent",
        object_key=key,
        media_type="audio/wav",
        byte_count=1,
        content_sha256="0" * 64,
        encryption_key_version="v1",
        backend="archive",
    )


@dataclass
class _Archive:
    owner_events: set[str] = field(default_factory=lambda: {"e1", "e2"})
    subject_events: set[str] = field(default_factory=lambda: {"crisis-1"})
    calls: list[str] = field(default_factory=list)
    fail_delete_once: bool = False

    async def subject_event_ids(self, scope: SubjectScope) -> tuple[str, ...]:
        self.calls.append("subject_event_ids")
        return tuple(sorted(self.owner_events))

    async def subject_account_event_ids(self, subject_id: str) -> tuple[str, ...]:
        return tuple(sorted(self.subject_events))

    async def object_references_for(
        self, *, account_id: str, event_ids: tuple[str, ...]
    ) -> tuple[ObjectRef, ...]:
        return (_ref("blob-e1"),) if account_id == "parent" and "e1" in event_ids else ()

    async def delete_events(self, *, account_id: str, event_ids: tuple[str, ...]) -> dict[str, int]:
        if self.fail_delete_once:
            self.fail_delete_once = False
            raise RuntimeError("database went away")
        self.calls.append(f"delete:{account_id}")
        target = self.owner_events if account_id == "parent" else self.subject_events
        removed = len(target & set(event_ids))
        target -= set(event_ids)
        return {"evidence_events": removed}

    async def remaining_rows_for(
        self, *, account_id: str, event_ids: tuple[str, ...], subject_id: str | None
    ) -> dict[str, int]:
        target = self.owner_events if account_id == "parent" else self.subject_events
        left = len(target & set(event_ids))
        return {"evidence_events": left} if left else {}


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
    """The persona learns per person; the saga forgets exactly one subject's."""

    def __init__(self) -> None:
        self.forgotten: list[tuple[str, str]] = []
        self.leftover: dict[str, int] = {}

    async def forget_subject(self, *, account_id: str, subject_id: str) -> int:
        self.forgotten.append((account_id, subject_id))
        return 4

    async def remaining_subject_rows(self, *, account_id: str, subject_id: str) -> dict[str, int]:
        return dict(self.leftover)


def _service(tmp_path: Path, **overrides: Any) -> tuple[SubjectDeletionService, dict[str, Any]]:
    ledger = SubjectDeletionLedger(tmp_path / "control.sqlite3")
    ledger.initialize()
    parts: dict[str, Any] = {
        "ledger": ledger,
        "archive": _Archive(),
        "object_store": _Objects(),
        "guardian": _Guardian(),
        "memory_scope": _MemoryScope(),
        "sessions": [],
        "corpus": [],
        "redacted": [],
        "persona": _Persona(),
    }
    parts.update(overrides)

    async def terminate(subject_id: str) -> int:
        parts["sessions"].append(subject_id)
        return 1

    async def purge(subject_id: str) -> int:
        parts["corpus"].append(subject_id)
        return 0

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
        redact_identity=redact,
        persona=parts["persona"],
    )
    return service, parts


@pytest.mark.asyncio
async def test_deletes_only_the_subject_and_proves_it(tmp_path: Path) -> None:
    service, parts = _service(tmp_path)

    result = await service.delete_subject(SCOPE)

    assert result["status"] == "completed"
    assert parts["sessions"] == ["child"]
    # Tutor evidence (archived without subject_id) joins the owner-side set.
    assert result["deleted_counts"]["lineage.owner_events"] == 3
    assert result["deleted_counts"]["archive.objects"] == 1
    assert parts["archive"].owner_events == set()
    assert parts["archive"].subject_events == set()
    assert parts["corpus"] == ["child"]
    assert parts["persona"].forgotten == [("parent", "child")]
    assert result["deleted_counts"]["persona.rows"] == 4
    # Still served by the device: the name stays.
    assert parts["redacted"] == []
    assert service.is_subject_deleting(account_id="parent", subject_id="child") is False


@pytest.mark.asyncio
async def test_a_failed_store_resumes_from_its_checkpoint(tmp_path: Path) -> None:
    archive = _Archive(fail_delete_once=True)
    service, parts = _service(tmp_path, archive=archive)

    with pytest.raises(RuntimeError, match="went away"):
        await service.delete_subject(SCOPE)
    # Fenced while incomplete: new evidence for the child is refused.
    assert service.is_subject_deleting(account_id="parent", subject_id="child") is True

    assert await service.retry_pending_deletions() == 1
    # Lineage was captured once and reused, sessions were not closed twice.
    assert archive.calls.count("subject_event_ids") == 1
    assert parts["sessions"] == ["child"]
    assert archive.owner_events == set()


@pytest.mark.asyncio
async def test_leftover_rows_keep_the_deletion_open(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, memory_scope=_MemoryScope(leftover=1))

    with pytest.raises(SubjectDeletionIncompleteError, match="memory_scope.memory_records"):
        await service.delete_subject(SCOPE)
    assert service.is_subject_deleting(account_id="parent", subject_id="child") is True


@pytest.mark.asyncio
async def test_unbind_erasure_also_redacts_the_identity(tmp_path: Path) -> None:
    service, parts = _service(tmp_path)

    result = await service.delete_subject(SCOPE, redact_identity=True)

    assert result["identity_redacted"] is True
    assert parts["redacted"] == [("child", "parent")]


@pytest.mark.asyncio
async def test_a_completed_deletion_can_run_again_for_new_data(tmp_path: Path) -> None:
    service, parts = _service(tmp_path)
    await service.delete_subject(SCOPE)
    parts["archive"].owner_events.add("e3")

    second = await service.delete_subject(SCOPE)

    assert second["status"] == "completed"
    assert parts["archive"].owner_events == set()


def test_a_subject_scope_never_targets_the_account_itself() -> None:
    with pytest.raises(ValueError, match="never targets the account"):
        SubjectScope(account_id="parent", subject_id="parent")


@pytest.mark.asyncio
async def test_leftover_persona_rows_keep_the_deletion_open(tmp_path: Path) -> None:
    persona = _Persona()
    persona.leftover = {"persona_traits": 1, "persona_versions": 0}
    service, _parts = _service(tmp_path, persona=persona)

    with pytest.raises(SubjectDeletionIncompleteError, match="persona.persona_traits"):
        await service.delete_subject(SCOPE)
    assert service.is_subject_deleting(account_id="parent", subject_id="child") is True



@pytest.mark.asyncio
async def test_completed_deletions_are_reapplied_after_a_restore(tmp_path: Path) -> None:
    """P2-03: restoring the data stores must not resurrect an erased subject.

    The ledger lives in the control database; restoring the archive from a
    backup brings the rows back while the ledger still says completed.
    """

    service, parts = _service(tmp_path)
    await service.delete_subject(SCOPE)
    assert parts["archive"].owner_events == set()

    # A backup restore brings the child's evidence back.
    parts["archive"].owner_events |= {"e1", "e2", "e3-restored"}
    parts["archive"].subject_events |= {"crisis-1"}
    report = await service.replay_completed_deletions()

    assert report == {"replayed": 1, "incomplete": 0}
    assert parts["archive"].owner_events == set()
    assert parts["archive"].subject_events == set()
    assert parts["persona"].forgotten == [("parent", "child"), ("parent", "child")]
    assert service.is_subject_deleting(account_id="parent", subject_id="child") is False


@pytest.mark.asyncio
async def test_a_replay_that_cannot_finish_stays_pending_for_the_worker(tmp_path: Path) -> None:
    service, parts = _service(tmp_path)
    await service.delete_subject(SCOPE)
    parts["archive"].owner_events |= {"e1"}
    parts["archive"].fail_delete_once = True

    report = await service.replay_completed_deletions()

    assert report == {"replayed": 0, "incomplete": 1}
    assert service.is_subject_deleting(account_id="parent", subject_id="child") is True
    assert await service.retry_pending_deletions() == 1
    assert parts["archive"].owner_events == set()
