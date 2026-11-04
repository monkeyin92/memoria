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
from services.guardian.child_archive_review import (
    ChildArchiveReviewCommand,
    ChildArchiveReviewError,
    ChildArchiveReviewReceipt,
    build_fact_view,
)
from services.guardian.child_memory import KIND_CHILD_ARCHIVE, KIND_DIALOGUE_SUMMARY, ArchiveFact
from services.memory_scope.child_layers import ChildLayerRecord


@dataclass
class _FakeLayers:
    summaries: dict[str, ChildLayerRecord] = field(default_factory=dict)
    archive: dict[str, ChildLayerRecord] = field(default_factory=dict)
    statuses: dict[str, tuple[str, str, datetime]] = field(default_factory=dict)
    audits: list[dict[str, Any]] = field(default_factory=list)

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

    async def list_archive_views(
        self,
        *,
        subject_id: str,
        viewer_subject_id: str,
        limit: int = 50,
        include_withdrawn: bool = False,
    ) -> list[Any]:
        del viewer_subject_id
        views = []
        for item in self.archive.values():
            if item.subject_id != subject_id:
                continue
            status, reason, handled = self.statuses.get(
                item.record_id, ("confirmed", "child_archive_fact", item.created_at)
            )
            if not include_withdrawn and status == "revoked":
                continue
            confidence = float(item.payload.get("confidence", 1.0)) if isinstance(item.payload.get("confidence"), (int, float)) else 1.0
            if item.payload.get("provenance") == "auto_extract":
                confidence = float(item.payload.get("confidence", 0.75))
            views.append(
                build_fact_view(
                    record_id=item.record_id,
                    field=str(item.field),
                    text=item.text,
                    created_at=item.created_at,
                    retention_expires_at=item.retention_expires_at,
                    payload=item.payload,
                    status=status,
                    reason_code=reason,
                    handled_at=handled,
                    confidence=confidence,
                )
            )
        views.sort(key=lambda view: view.created_at, reverse=True)
        return views[:limit]

    async def get_archive_view(
        self, *, subject_id: str, record_id: str, viewer_subject_id: str
    ) -> Any:
        views = await self.list_archive_views(
            subject_id=subject_id,
            viewer_subject_id=viewer_subject_id,
            limit=100,
            include_withdrawn=True,
        )
        for view in views:
            if view.record_id == record_id:
                return view
        return None

    async def review_archive_fact(
        self,
        *,
        subject_id: str,
        resource_owner_id: str,
        consent_snapshot_id: str,
        command: ChildArchiveReviewCommand,
    ) -> ChildArchiveReviewReceipt:
        del consent_snapshot_id
        current = await self.get_archive_view(
            subject_id=subject_id,
            record_id=command.record_id,
            viewer_subject_id=resource_owner_id,
        )
        if current is None:
            raise ChildArchiveReviewError("child_archive_not_found")
        if current.status == "revoked":
            raise ChildArchiveReviewError("child_archive_already_withdrawn")
        stamped = datetime.now(UTC)
        if command.action == "confirm":
            self.statuses[command.record_id] = ("confirmed", "parent_confirm", stamped)
            audit = f"audit:confirm:{command.record_id}"
            self.audits.append({"action": "confirm", "record_id": command.record_id})
            return ChildArchiveReviewReceipt(
                action="confirm",
                record_id=command.record_id,
                status="confirmed",
                display_lifecycle="parent_confirmed",
                display_label="家长已确认",
                reviewed_at=stamped,
                audit_event_id=audit,
                old_version=current.fact_version,
                new_version=current.fact_version,
            )
        if command.action == "withdraw":
            self.statuses[command.record_id] = ("revoked", "parent_withdraw", stamped)
            audit = f"audit:withdraw:{command.record_id}"
            self.audits.append({"action": "withdraw", "record_id": command.record_id})
            return ChildArchiveReviewReceipt(
                action="withdraw",
                record_id=command.record_id,
                status="revoked",
                display_lifecycle="withdrawn",
                display_label="已撤回",
                reviewed_at=stamped,
                audit_event_id=audit,
                old_version=current.fact_version,
                new_version=current.fact_version,
            )
        text = command.text or current.text
        field_name = command.field or current.field
        polarity = command.polarity or current.polarity or "unknown"
        fact = ArchiveFact(subject_id=subject_id, field=field_name, text=text)
        new_id = str(uuid4())
        new_version = current.fact_version + 1
        self.statuses[command.record_id] = (
            "revoked",
            f"superseded_by:{new_id}",
            stamped,
        )
        record = ChildLayerRecord(
            record_id=new_id,
            subject_id=subject_id,
            kind=KIND_CHILD_ARCHIVE,
            text=fact.text,
            field=fact.field,
            session_id=current.source_session_id,
            created_at=stamped,
            retention_expires_at=None,
            payload={
                "kind": KIND_CHILD_ARCHIVE,
                "field": fact.field,
                "text": fact.text,
                "provenance": "parent_confirm",
                "polarity": polarity,
                "fact_version": new_version,
                "supersedes_record_id": command.record_id,
                "source_session_id": current.source_session_id or "",
            },
        )
        self.archive[new_id] = record
        self.statuses[new_id] = (
            "confirmed",
            "parent_correct_history" if command.history_change else "parent_correct",
            stamped,
        )
        audit = f"audit:correct:{new_id}"
        self.audits.append({"action": "correct", "old": command.record_id, "new": new_id})
        return ChildArchiveReviewReceipt(
            action="correct",
            record_id=new_id,
            status="confirmed",
            display_lifecycle="parent_confirmed",
            display_label="家长已确认",
            reviewed_at=stamped,
            audit_event_id=audit,
            old_record_id=command.record_id,
            new_record_id=new_id,
            old_version=current.fact_version,
            new_version=new_version,
        )

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
        self,
        *,
        subject_id: str,
        field_name: str,
        text: str,
        last_mentioned_at: str | None = None,
        provenance: str = "parent_entry",
        status: str = "confirmed",
        reason_code: str = "child_archive_fact",
        source_session_id: str | None = None,
        rule_version: str | None = None,
        polarity: str | None = None,
        confidence: float = 1.0,
    ) -> ChildLayerRecord:
        now = datetime.now(UTC)
        payload: dict[str, object] = {
            "kind": KIND_CHILD_ARCHIVE,
            "field": field_name,
            "text": text,
            "provenance": provenance,
            "fact_version": 1,
            "confidence": confidence,
        }
        if last_mentioned_at:
            payload["last_mentioned_at"] = last_mentioned_at
        if source_session_id:
            payload["source_session_id"] = source_session_id
        if rule_version:
            payload["rule_version"] = rule_version
        if polarity:
            payload["polarity"] = polarity
        if provenance == "auto_extract" and status == "candidate":
            reason_code = "child_archive_auto_extract"
        record = ChildLayerRecord(
            record_id=str(uuid4()),
            subject_id=subject_id,
            kind=KIND_CHILD_ARCHIVE,
            text=text,
            field=field_name,
            session_id=source_session_id,
            created_at=now,
            retention_expires_at=None,
            payload=payload,
        )
        self.archive[record.record_id] = record
        self.statuses[record.record_id] = (status, reason_code, now)
        return record


async def _bound_child(
    client: AsyncClient, app: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, dict[str, str], _FakeLayers]:
    # Set up consent store with default approve for memory_retention
    consent_store = _RecordingConsent()
    app.state.guardian_store = consent_store
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
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["record_id"] == fact.record_id
    assert item["field"] == "preference"
    assert item["text"] == "喜欢恐龙"
    assert item["created_at"] == fact.created_at.isoformat()
    assert item["last_mentioned_at"] == "2026-10-12T08:00:00+00:00"
    assert item["display_lifecycle"] == "parent_confirmed"
    assert item["display_label"] == "家长已确认"
    assert "已确认" not in item["display_label"] or item["display_label"] == "家长已确认"
    assert cleared.status_code == 200, cleared.text
    assert after.json()["items"] == []


@pytest.mark.asyncio
async def test_parent_confirms_corrects_and_withdraws_archive_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _owner_id, child_id, headers, layers = await _bound_child(client, app, monkeypatch)
        wrong = layers.seed_archive(
            subject_id=child_id,
            field_name="preference",
            text="喜欢恐龙",
            provenance="auto_extract",
            status="candidate",
            source_session_id="sess-review",
            rule_version="preference-extract-v2",
            polarity="positive",
            confidence=0.75,
        )
        base = f"/v1/guardian/minors/{child_id}/child-archive"
        listed = await client.get(base, headers=headers)
        assert listed.status_code == 200, listed.text
        card = listed.json()["items"][0]
        assert card["display_lifecycle"] == "auto_pending"
        assert card["display_label"] == "自动提取·待确认"
        assert card["source_session_id"] == "sess-review"
        assert card["rule_version"] == "preference-extract-v2"
        assert card["recall_eligible"] is False

        confirmed = await client.post(
            f"{base}/{wrong.record_id}/review",
            headers=headers,
            json={"action": "confirm"},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["display_lifecycle"] == "parent_confirmed"
        assert confirmed.json()["audit_event_id"]
        assert confirmed.json()["r09_guardian_token"] == "未验"

        after_confirm = await client.get(base, headers=headers)
        assert after_confirm.json()["items"][0]["display_label"] == "家长已确认"
        assert after_confirm.json()["items"][0]["recall_eligible"] is True

        history = layers.seed_archive(
            subject_id=child_id,
            field_name="preference",
            text="喜欢画画",
            provenance="auto_extract",
            status="candidate",
            polarity="positive",
            confidence=0.75,
        )
        corrected = await client.post(
            f"{base}/{history.record_id}/review",
            headers=headers,
            json={
                "action": "correct",
                "text": "不喜欢画画",
                "polarity": "negative",
                "history_change": True,
            },
        )
        assert corrected.status_code == 200, corrected.text
        body = corrected.json()
        assert body["old_record_id"] == history.record_id
        assert body["new_record_id"] == body["record_id"]
        assert body["new_version"] == 2
        listed_after = await client.get(base, headers=headers)
        texts = {item["text"] for item in listed_after.json()["items"]}
        assert "喜欢画画" not in texts
        assert "不喜欢画画" in texts

        withdraw_target = next(
            item for item in listed_after.json()["items"] if item["text"] == "不喜欢画画"
        )
        withdrawn = await client.post(
            f"{base}/{withdraw_target['record_id']}/review",
            headers=headers,
            json={"action": "withdraw"},
        )
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["display_lifecycle"] == "withdrawn"
        assert withdrawn.json()["audit_event_id"]
        active = await client.get(base, headers=headers)
        assert all(item["text"] != "不喜欢画画" for item in active.json()["items"])


@pytest.mark.asyncio
async def test_stranger_cannot_review_child_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _owner_id, child_id, headers, layers = await _bound_child(client, app, monkeypatch)
        fact = layers.seed_archive(
            subject_id=child_id,
            field_name="preference",
            text="喜欢小狗",
            provenance="auto_extract",
            status="candidate",
        )
        _, stranger = await _owner(client, app, "child-layers-stranger-review")
        reviewed = await client.post(
            f"/v1/guardian/minors/{child_id}/child-archive/{fact.record_id}/review",
            headers=stranger,
            json={"action": "confirm"},
        )
        assert reviewed.status_code == 403
        still = await client.get(
            f"/v1/guardian/minors/{child_id}/child-archive", headers=headers
        )
        assert still.json()["items"][0]["display_lifecycle"] == "auto_pending"


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
