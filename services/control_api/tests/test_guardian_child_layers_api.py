"""HTTP contract for parent-visible dialogue summaries and child_archive facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app
from services.control_api.tests.test_bound_subject_binding import (
    _bind,
    _configure,
    _owner,
    _RecordingConsent,
)
from services.guardian.child_memory import KIND_CHILD_ARCHIVE, KIND_DIALOGUE_SUMMARY
from services.memory_scope.child_layers import ChildLayerRecord


@dataclass
class _FakeLayers:
    summaries: dict[str, ChildLayerRecord] = field(default_factory=dict)
    archive: dict[str, ChildLayerRecord] = field(default_factory=dict)

    async def list(
        self,
        *,
        subject_id: str,
        kind: Literal["dialogue_summary", "child_archive"],
        viewer_subject_id: str,
        limit: int = 20,
    ) -> list[ChildLayerRecord]:
        del viewer_subject_id
        source = self.summaries if kind == KIND_DIALOGUE_SUMMARY else self.archive
        items = [item for item in source.values() if item.subject_id == subject_id]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items[:limit]

    async def get(
        self, *, subject_id: str, record_id: str, viewer_subject_id: str
    ) -> ChildLayerRecord | None:
        del viewer_subject_id
        item = self.summaries.get(record_id) or self.archive.get(record_id)
        if item is None or item.subject_id != subject_id:
            return None
        return item

    async def delete_record(self, *, subject_id: str, record_id: str) -> dict[str, Any]:
        for bucket in (self.summaries, self.archive):
            item = bucket.get(record_id)
            if item is not None and item.subject_id == subject_id:
                del bucket[record_id]
                return {"memory_records": 1}
        return {"memory_records": 0}

    async def erase_kind(
        self,
        *,
        subject_id: str,
        kind: Literal["dialogue_summary", "child_archive"],
        only_expired: bool = False,
    ) -> dict[str, Any]:
        del only_expired
        bucket = self.summaries if kind == KIND_DIALOGUE_SUMMARY else self.archive
        victims = [rid for rid, item in bucket.items() if item.subject_id == subject_id]
        for rid in victims:
            del bucket[rid]
        return {"memory_records": len(victims)}

    async def remaining_kind(
        self,
        *,
        subject_id: str,
        kind: Literal["dialogue_summary", "child_archive"],
        only_expired: bool = False,
    ) -> dict[str, Any]:
        del only_expired
        bucket = self.summaries if kind == KIND_DIALOGUE_SUMMARY else self.archive
        left = sum(1 for item in bucket.values() if item.subject_id == subject_id)
        return {"memory_records": left}

    def seed_summary(self, *, subject_id: str, text: str) -> ChildLayerRecord:
        now = datetime.now(UTC)
        record = ChildLayerRecord(
            record_id=str(uuid4()),
            subject_id=subject_id,
            kind=KIND_DIALOGUE_SUMMARY,
            text=text,
            field=None,
            session_id="sess-1",
            created_at=now,
            retention_expires_at=None,
            payload={"kind": KIND_DIALOGUE_SUMMARY, "text": text, "session_id": "sess-1"},
        )
        self.summaries[record.record_id] = record
        return record

    def seed_archive(
        self, *, subject_id: str, field_name: str, text: str, last_mentioned_at: str | None = None
    ) -> ChildLayerRecord:
        now = datetime.now(UTC)
        payload: dict[str, object] = {
            "kind": KIND_CHILD_ARCHIVE,
            "field": field_name,
            "text": text,
        }
        if last_mentioned_at:
            payload["last_mentioned_at"] = last_mentioned_at
        record = ChildLayerRecord(
            record_id=str(uuid4()),
            subject_id=subject_id,
            kind=KIND_CHILD_ARCHIVE,
            text=text,
            field=field_name,
            session_id=None,
            created_at=now,
            retention_expires_at=None,
            payload=payload,
        )
        self.archive[record.record_id] = record
        return record


async def _bound_child(
    client: AsyncClient, app: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, dict[str, str], _FakeLayers]:
    app.state.bound_subject_consent = _RecordingConsent()
    layers = _FakeLayers()
    app.state.child_layer_store = layers
    owner_id, headers = await _owner(client, app, "child-layers-owner")
    created = await _bind(
        client,
        app,
        owner_id=owner_id,
        headers=headers,
        device_id="device-child-layers",
        declared_mode="parent_for_child",
        relationship="guardian_of",
        age_band="under_14",
        offers=["offer_minor_voice_session_v1"],
    )
    assert created.status_code == 201, created.text
    child_id = created.json()["primary_subject_ids"][0]
    return owner_id, child_id, headers, layers


@pytest.mark.asyncio
async def test_parent_lists_deletes_and_clears_session_summaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _owner_id, child_id, headers, layers = await _bound_child(client, app, monkeypatch)
        first = layers.seed_summary(subject_id=child_id, text="孩子今天聊了小狗和画画。")
        second = layers.seed_summary(subject_id=child_id, text="孩子问起恐龙为什么灭绝。")
        base = f"/v1/guardian/minors/{child_id}/session-summaries"

        listed = await client.get(base, headers=headers)
        body = listed.json()
        deleted = await client.delete(f"{base}/{first.record_id}", headers=headers)
        after_one = await client.get(base, headers=headers)
        cleared = await client.delete(base, headers=headers)
        after_clear = await client.get(base, headers=headers)

    assert listed.status_code == 200, listed.text
    assert body["privacy"] == {"contains_transcript": False}
    assert {item["record_id"] for item in body["items"]} == {first.record_id, second.record_id}
    assert all("text" in item and "session_id" in item for item in body["items"])
    assert deleted.status_code == 200, deleted.text
    assert [item["record_id"] for item in after_one.json()["items"]] == [second.record_id]
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["remaining"]["memory_records"] == 0
    assert after_clear.json()["items"] == []


@pytest.mark.asyncio
async def test_parent_lists_and_clears_child_archive_with_last_mentioned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _owner_id, child_id, headers, layers = await _bound_child(client, app, monkeypatch)
        fact = layers.seed_archive(
            subject_id=child_id,
            field_name="preference",
            text="喜欢恐龙",
            last_mentioned_at="2026-10-12T08:00:00+00:00",
        )
        base = f"/v1/guardian/minors/{child_id}/child-archive"

        listed = await client.get(base, headers=headers)
        body = listed.json()
        cleared = await client.delete(base, headers=headers)
        after = await client.get(base, headers=headers)

    assert listed.status_code == 200, listed.text
    assert body["items"] == [
        {
            "record_id": fact.record_id,
            "field": "preference",
            "text": "喜欢恐龙",
            "created_at": fact.created_at.isoformat(),
            "last_mentioned_at": "2026-10-12T08:00:00+00:00",
        }
    ]
    assert cleared.status_code == 200, cleared.text
    assert after.json()["items"] == []


@pytest.mark.asyncio
async def test_stranger_cannot_read_or_clear_child_layers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _owner_id, child_id, headers, layers = await _bound_child(client, app, monkeypatch)
        layers.seed_summary(subject_id=child_id, text="孩子聊了画画。")
        _, stranger = await _owner(client, app, "child-layers-stranger")
        summaries = f"/v1/guardian/minors/{child_id}/session-summaries"
        archive = f"/v1/guardian/minors/{child_id}/child-archive"

        seen = await client.get(summaries, headers=stranger)
        cleared = await client.delete(summaries, headers=stranger)
        archive_seen = await client.get(archive, headers=stranger)
        still_there = await client.get(summaries, headers=headers)

    assert seen.status_code == 403
    assert cleared.status_code == 403
    assert archive_seen.status_code == 403
    assert len(still_there.json()["items"]) == 1
