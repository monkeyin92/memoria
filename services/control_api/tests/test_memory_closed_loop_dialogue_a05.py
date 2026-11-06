from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.memory_domain import MemorySearchResult
from services.control_api.app.main import create_app
from services.control_api.app.response_plan_cache import forget_subject_plans
from services.control_api.tests.interaction_api_test_support import (
    _attach_signed_runtime_profile,
    _configure,
    _identity,
    _response_plan_body,
)
from services.guardian.child_archive_review import (
    ChildArchiveReviewCommand,
    build_fact_view,
)
from services.guardian.child_memory import KIND_CHILD_ARCHIVE
from services.memory_scope.child_layers import ChildLayerRecord


class _EmptyCatalog:
    async def people(self, **_: object) -> tuple[Any, ...]:
        return ()

    async def context(self, query: object) -> MemorySearchResult:
        del query
        return MemorySearchResult()


class _ConsentGuardian:
    def __init__(self) -> None:
        self.kinds = {"memory_retention", "child_archive"}

    async def active_consent(self, *, minor_user_id: str, consent_kind: str) -> object | None:
        del minor_user_id
        if consent_kind in self.kinds:
            return {"consent_kind": consent_kind}
        return None


class _LoopLayers:
    def __init__(self) -> None:
        self.archive: dict[str, ChildLayerRecord] = {}
        self.statuses: dict[str, tuple[str, str, datetime]] = {}

    def seed_candidate(
        self,
        *,
        subject_id: str,
        text: str,
        record_id: str | None = None,
        fact_version: int = 1,
    ) -> ChildLayerRecord:
        now = datetime.now(UTC)
        rid = record_id or str(uuid4())
        record = ChildLayerRecord(
            record_id=rid,
            subject_id=subject_id,
            kind=KIND_CHILD_ARCHIVE,
            text=text,
            field="preference",
            session_id="sess-a05",
            created_at=now,
            retention_expires_at=None,
            payload={
                "kind": KIND_CHILD_ARCHIVE,
                "field": "preference",
                "text": text,
                "provenance": "auto_extract",
                "source_session_id": "sess-a05",
                "rule_version": "preference-extract-v2",
                "fact_version": fact_version,
                "polarity": "positive",
                "confidence": 0.75,
            },
        )
        self.archive[rid] = record
        self.statuses[rid] = ("candidate", "child_archive_auto_extract", now)
        return record

    async def list(
        self,
        *,
        subject_id: str,
        kind: str,
        viewer_subject_id: str,
        limit: int = 50,
    ) -> list[ChildLayerRecord]:
        del viewer_subject_id, limit
        if kind != KIND_CHILD_ARCHIVE:
            return []
        return [row for row in self.archive.values() if row.subject_id == subject_id]

    async def list_archive_views(
        self,
        *,
        subject_id: str,
        viewer_subject_id: str,
        limit: int = 50,
        include_withdrawn: bool = False,
    ) -> list[Any]:
        del viewer_subject_id
        views: list[Any] = []
        for item in self.archive.values():
            if item.subject_id != subject_id:
                continue
            status, reason, handled = self.statuses[item.record_id]
            if not include_withdrawn and status == "revoked":
                continue
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
                    confidence=float(item.payload.get("confidence", 0.75)),
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
    ) -> Any:
        from services.guardian.child_archive_review import (
            ChildArchiveReviewError,
            ChildArchiveReviewReceipt,
        )

        del consent_snapshot_id
        current = await self.get_archive_view(
            subject_id=subject_id,
            record_id=command.record_id,
            viewer_subject_id=resource_owner_id,
        )
        if current is None:
            raise ChildArchiveReviewError("child_archive_not_found")
        stamped = datetime.now(UTC)
        if command.action == "confirm":
            self.statuses[command.record_id] = ("confirmed", "parent_confirm", stamped)
            return ChildArchiveReviewReceipt(
                action="confirm",
                record_id=command.record_id,
                status="confirmed",
                display_lifecycle="parent_confirmed",
                display_label="家长已确认",
                reviewed_at=stamped,
                audit_event_id=f"audit:confirm:{command.record_id}",
                old_version=current.fact_version,
                new_version=current.fact_version,
            )
        if command.action == "withdraw":
            self.statuses[command.record_id] = ("revoked", "parent_withdraw", stamped)
            return ChildArchiveReviewReceipt(
                action="withdraw",
                record_id=command.record_id,
                status="revoked",
                display_lifecycle="withdrawn",
                display_label="已撤回",
                reviewed_at=stamped,
                audit_event_id=f"audit:withdraw:{command.record_id}",
                old_version=current.fact_version,
                new_version=current.fact_version,
            )
        new_id = str(uuid4())
        new_version = current.fact_version + 1
        text = command.text or current.text
        self.statuses[command.record_id] = ("revoked", f"superseded_by:{new_id}", stamped)
        record = ChildLayerRecord(
            record_id=new_id,
            subject_id=subject_id,
            kind=KIND_CHILD_ARCHIVE,
            text=text,
            field=current.field,
            session_id=current.source_session_id,
            created_at=stamped,
            retention_expires_at=None,
            payload={
                "kind": KIND_CHILD_ARCHIVE,
                "field": current.field,
                "text": text,
                "provenance": "parent_confirm",
                "polarity": "positive",
                "fact_version": new_version,
                "supersedes_record_id": command.record_id,
                "source_session_id": current.source_session_id or "",
                "rule_version": current.rule_version,
                "confidence": 1.0,
            },
        )
        self.archive[new_id] = record
        self.statuses[new_id] = (
            "confirmed",
            "parent_correct_history" if command.history_change else "parent_correct",
            stamped,
        )
        return ChildArchiveReviewReceipt(
            action="correct",
            record_id=new_id,
            status="confirmed",
            display_lifecycle="parent_confirmed",
            display_label="家长已确认",
            reviewed_at=stamped,
            audit_event_id=f"audit:correct:{new_id}",
            old_record_id=command.record_id,
            new_record_id=new_id,
            old_version=current.fact_version,
            new_version=new_version,
        )


async def _memory_claims(
    client: AsyncClient,
    *,
    session_id: str,
    query: str = "我喜欢什么？",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    token = {"X-Memoria-Internal-Token": "response-plan-token-that-is-long-enough"}
    body = _response_plan_body(session_id)
    body["query"] = query
    planned = await client.post("/v1/interaction/response-plan", headers=token, json=body)
    prefetched = await client.post(
        "/v1/interaction/context-prefetch",
        headers=token,
        json={
            "session_id": session_id,
            "query": query,
            "speaker_decision": body["speaker_decision"],
        },
    )
    assert planned.status_code == 200, planned.text
    assert prefetched.status_code == 200, prefetched.text
    planned_claims = [
        item for item in planned.json()["grounded_items"] if item["kind"] == "memory_claim"
    ]
    prefetched_claims = [
        item for item in prefetched.json()["grounded_items"] if item["kind"] == "memory_claim"
    ]
    return planned_claims, prefetched_claims


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_closed_loop_dialogue_confirm_correct_withdraw_cache_drop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    child_id = "person-child-a05"
    layers = _LoopLayers()
    first = layers.seed_candidate(subject_id=child_id, text="喜欢画画", record_id="charch:a05:paint")
    second = layers.seed_candidate(subject_id=child_id, text="喜欢小狗", record_id="charch:a05:dog")
    third = layers.seed_candidate(subject_id=child_id, text="喜欢恐龙", record_id="charch:a05:dino")
    app.state.memory_catalog = _EmptyCatalog()
    app.state.child_layer_store = layers
    app.state.guardian_store = _ConsentGuardian()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, user_headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=user_headers, json={})).json()[
            "session_id"
        ]
        _attach_signed_runtime_profile(
            app,
            user_id=user_id,
            session_id=session_id,
            active_subject_id=child_id,
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
            capabilities=("chat", "memory_recall_private"),
        )

        pending_plan, pending_prefetch = await _memory_claims(client, session_id=session_id)
        assert pending_plan == []
        assert pending_prefetch == []

        actor_id = user_id
        for record in (first, second, third):
            receipt = await layers.review_archive_fact(
                subject_id=child_id,
                resource_owner_id=actor_id,
                consent_snapshot_id="consent-a05",
                command=ChildArchiveReviewCommand(
                    action="confirm",
                    record_id=record.record_id,
                    actor_id=actor_id,
                ),
            )
            assert receipt.display_lifecycle == "parent_confirmed"
            assert receipt.audit_event_id

        dropped = await forget_subject_plans(app.state, child_id)
        assert dropped >= 1, "empty pre-confirm plan must drop before re-plan"

        after_confirm_plan, after_confirm_prefetch = await _memory_claims(
            client, session_id=session_id
        )
        assert {item["item_id"] for item in after_confirm_plan} == {
            first.record_id,
            second.record_id,
            third.record_id,
        }
        assert after_confirm_prefetch == after_confirm_plan
        assert all(item["use_as"] == "fact" for item in after_confirm_plan)
        assert all(item["source_event_ids"] == ["fact_version:1"] for item in after_confirm_plan)

        corrected = await layers.review_archive_fact(
            subject_id=child_id,
            resource_owner_id=actor_id,
            consent_snapshot_id="consent-a05",
            command=ChildArchiveReviewCommand(
                action="correct",
                record_id=first.record_id,
                actor_id=actor_id,
                text="喜欢水彩",
                history_change=True,
            ),
        )
        assert corrected.new_record_id is not None
        assert corrected.old_record_id == first.record_id
        assert corrected.new_version == 2
        assert corrected.audit_event_id
        await forget_subject_plans(app.state, child_id)

        after_correct_plan, after_correct_prefetch = await _memory_claims(
            client, session_id=session_id
        )
        ids_after_correct = {item["item_id"] for item in after_correct_plan}
        assert first.record_id not in ids_after_correct
        assert corrected.new_record_id in ids_after_correct
        corrected_item = next(
            item for item in after_correct_plan if item["item_id"] == corrected.new_record_id
        )
        assert corrected_item["content"] == "喜好：喜欢水彩"
        assert corrected_item["source_event_ids"] == ["fact_version:2"]
        assert "喜欢画画" not in {item["content"] for item in after_correct_plan}
        assert after_correct_prefetch == after_correct_plan

        withdrawn = await layers.review_archive_fact(
            subject_id=child_id,
            resource_owner_id=actor_id,
            consent_snapshot_id="consent-a05",
            command=ChildArchiveReviewCommand(
                action="withdraw",
                record_id=second.record_id,
                actor_id=actor_id,
            ),
        )
        assert withdrawn.display_lifecycle == "withdrawn"
        assert withdrawn.audit_event_id
        await forget_subject_plans(app.state, child_id)

        after_withdraw_plan, after_withdraw_prefetch = await _memory_claims(
            client, session_id=session_id
        )
        ids_after_withdraw = {item["item_id"] for item in after_withdraw_plan}
        assert second.record_id not in ids_after_withdraw
        assert corrected.new_record_id in ids_after_withdraw
        assert third.record_id in ids_after_withdraw
        assert "小狗" not in str(after_withdraw_plan)
        assert after_withdraw_prefetch == after_withdraw_plan

        again_plan, again_prefetch = await _memory_claims(client, session_id=session_id)
        assert {item["item_id"] for item in again_plan} == {
            corrected.new_record_id,
            third.record_id,
        }
        assert second.record_id not in {item["item_id"] for item in again_plan}
        assert first.record_id not in {item["item_id"] for item in again_plan}
        assert again_prefetch == again_plan
