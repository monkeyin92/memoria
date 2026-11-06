"""Capture, write-fence and child-chat read tests for MemoryScopeService
(PR-12/PR-14).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRole,
    PolicyEffect,
    PolicyObligation,
)
from services.memory_scope.domain import (
    ActorNotAuthorizedError,
    CoSubjectContext,
    MemoryRecord,
    MemoryScope,
    MemoryWriteDraft,
    MemoryWriteRejected,
    ReceiptNotVerifiedError,
    WriteFenceExpiredError,
    WriteFenceMissingError,
)
from services.memory_scope.service import MemoryScopeService
from services.memory_scope.tests.service_helpers import (
    _FENCE,
    _consent,
    _context,
    _draft,
    _fence,
    _policy,
    _register_consent,
    _register_receipt,
    _subject,
)

# Re-exported (not redefined) so pytest discovers the fixture by name.
from services.memory_scope.tests.service_helpers import (
    service as service,
)


def _register_guardian(
    service: MemoryScopeService, actor_subject_id: str, wards: frozenset[str]
) -> None:
    resolver = service.grant_resolver
    assert resolver is not None
    resolver.register_guardian(actor_subject_id, wards)


class TestCapture:
    async def test_capture_private_persists_with_full_sources(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        await _register_receipt(service, _FENCE)
        await _register_consent(service, ("person-a",))
        record = await service.capture(
            _context(subject, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE),
            _draft(),
            actor_subject_id="person-a",
        )
        assert record.scope is MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE
        assert record.subject_id == "person-a"
        assert record.resource_owner_id == "person-a"
        assert record.source_evidence_ids == ("evidence-1", "evidence-2")
        assert record.policy_receipt_id == "receipt-1"
        assert record.consent_snapshot_id == "consent-1"
        assert record.status == "confirmed"
        assert record.payload["content"] == "今天一起去了公园"

        fetched = await service.get(record.record_id, "person-a")
        assert fetched is not None
        assert fetched.payload["content"] == "今天一起去了公园"

    async def test_capture_rejects_unregistered_guest(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("guest-1", registered=False)
        guest_fence = _fence(
            actor_subject_id="guest-1", active_subject_id="guest-1"
        )
        await _register_receipt(service, guest_fence)
        await _register_consent(service, ("guest-1",))
        with pytest.raises(MemoryWriteRejected, match="unregistered_guest"):
            await service.capture(
                _context(
                    subject,
                    requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                    fence=guest_fence,
                ),
                _draft(),
                actor_subject_id="guest-1",
            )

    async def test_capture_rejects_unconfirmed_speaker(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a", speaker="unconfirmed")
        await _register_receipt(service, _FENCE)
        with pytest.raises(MemoryWriteRejected, match="subject_not_confirmed"):
            await service.capture(
                _context(subject, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE),
                _draft(),
                actor_subject_id="person-a",
            )

    async def test_capture_rejects_policy_deny(self, service: MemoryScopeService) -> None:
        """A deny-effect receipt can never authorize a durable write: the
        authoritative receipt verification rejects it before resolution."""
        await _register_receipt(
            service,
            _FENCE,
            effect=PolicyEffect.POLICY_EFFECT_DENY,
        )
        with pytest.raises(ReceiptNotVerifiedError, match="does not match"):
            await service.capture(
                _context(
                    _subject("person-a"),
                    requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                ),
                _draft(),
                actor_subject_id="person-a",
            )

    async def test_capture_rejects_missing_evidence(
        self, service: MemoryScopeService
    ) -> None:
        await _register_receipt(service, _FENCE)
        with pytest.raises(MemoryWriteRejected, match="missing_sensitive_source"):
            await service.capture(
                _context(_subject("person-a"), requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE),
                MemoryWriteDraft(content="x"),
                actor_subject_id="person-a",
            )

    async def test_capture_rejects_missing_consent(
        self, service: MemoryScopeService
    ) -> None:
        await _register_receipt(service, _FENCE)
        with pytest.raises(MemoryWriteRejected, match="consent snapshot missing"):
            await service.capture(
                _context(
                    _subject("person-a"),
                    requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                    consent=_consent("person-a", granted=False),
                ),
                _draft(),
                actor_subject_id="person-a",
            )

    async def test_capture_guardian_summary_rejects_raw_transcript(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject(
            "person-child",
            category="minor",
            guardian_active=True,
        )
        guardian_fence = _fence(
            binding_role=BindingRole.BINDING_ROLE_GUARDIAN.value,
            actor_subject_id="parent-1",
            active_subject_id="person-child",
        )
        await _register_receipt(
            service,
            guardian_fence,
            actor_subject_id="parent-1",
            purpose="guardian_summary",
            capability="guardian_summary_view",
            obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
        )
        await _register_consent(service, ("person-child",))
        draft = MemoryWriteDraft(
            content="summary",
            source_evidence_ids=("evidence-1",),
            payload={"raw_transcript": "孩子说：妈妈我不想去上学"},
        )
        with pytest.raises(MemoryWriteRejected, match="raw transcript"):
            await service.capture(
                _context(
                    subject,
                    requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
                    policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
                    fence=guardian_fence,
                ),
                draft,
                actor_subject_id="parent-1",
            )

    async def test_capture_guardian_summary_ok_for_aggregate(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-child", category="minor", guardian_active=True)
        guardian_fence = _fence(
            binding_role=BindingRole.BINDING_ROLE_GUARDIAN.value,
            actor_subject_id="parent-1",
            active_subject_id="person-child",
        )
        await _register_receipt(
            service,
            guardian_fence,
            actor_subject_id="parent-1",
            purpose="guardian_summary",
            capability="guardian_summary_view",
            obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
        )
        await _register_consent(service, ("person-child",))
        record = await service.capture(
            _context(
                subject,
                requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
                policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
                fence=guardian_fence,
            ),
            _draft("本周学习 120 分钟，主题：英语"),
            actor_subject_id="parent-1",
        )
        assert record.scope is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY
        assert record.subject_id == "person-child"

    async def test_capture_family_shared_must_use_lifecycle(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a", family="family-1")
        family_fence = _fence(family_space_id="family-1")
        co = CoSubjectContext(
            subject_ids=("person-b",), confirmed_subject_ids=("person-b",)
        )
        await _register_receipt(service, family_fence)
        await _register_consent(service, ("person-a", "person-b"))
        with pytest.raises(MemoryWriteRejected, match="propose_shared"):
            await service.capture(
                _context(
                    subject,
                    requested=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
                    consent=_consent("person-a", "person-b"),
                    co_subjects=co,
                    fence=family_fence,
                ),
                _draft(),
                actor_subject_id="person-a",
            )

    async def test_capture_emits_outbox_and_audit(
        self, service: MemoryScopeService
    ) -> None:
        await _register_receipt(service, _FENCE)
        await _register_consent(service, ("person-a",))
        record = await service.capture(
            _context(_subject("person-a"), requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE),
            _draft(),
            actor_subject_id="person-a",
        )
        outbox = await service.store.list_pending_outbox()
        assert any(event.event_id == f"{record.record_id}:captured" for event in outbox)
        assert all(event.topic == "memory.record.captured" for event in outbox)


class TestWriteFence:
    """PR-12 / section 11.5 fence enforcement at the service boundary."""

    async def test_capture_without_fence_fails_closed(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        context = _context(
            subject, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE
        )
        context = replace(context, fence=None)  # type: ignore[arg-type]
        with pytest.raises(WriteFenceMissingError, match="fence"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_stale_receipt_after_epoch_bump(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        await _register_receipt(service, _fence(epoch=1))
        late = _fence(epoch=2)  # speaker switch bumped the epoch
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            fence=late,
        )
        with pytest.raises(ReceiptNotVerifiedError, match="does not match"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_receipt_issued_for_different_binding(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        await _register_receipt(service, _FENCE)
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            fence=_fence(binding_id="binding-2"),
        )
        with pytest.raises(ReceiptNotVerifiedError, match="does not match"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_expired_receipt(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        await _register_receipt(
            service,
            _FENCE,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        context = _context(
            subject, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE
        )
        with pytest.raises(ReceiptNotVerifiedError, match="does not match"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_legacy_receipt_with_older_binding_version(
        self, service: MemoryScopeService
    ) -> None:
        """P0-4/联审: a receipt issued under an older binding manifest version
        must never be reusable with the current fence."""
        subject = _subject("person-a")
        await _register_receipt(
            service,
            _FENCE,
            binding_version=1,  # legacy receipt
        )
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            fence=_fence(binding_version=2),
        )
        with pytest.raises(ReceiptNotVerifiedError, match="does not match"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_expired_fence(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            fence=_fence(valid_until=datetime.now(UTC) - timedelta(days=1)),
        )
        with pytest.raises(WriteFenceExpiredError, match="expired"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_actor_who_is_not_the_subject(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        wrong_actor_fence = _fence(
            actor_subject_id="person-b", active_subject_id="person-a"
        )
        await _register_receipt(
            service,
            wrong_actor_fence,
            actor_subject_id="person-b",
            subject_id="person-a",
        )
        await _register_consent(service, ("person-a",))
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            fence=wrong_actor_fence,
        )
        with pytest.raises(ActorNotAuthorizedError, match="not the subject"):
            await service.capture(context, _draft(), actor_subject_id="person-b")

    async def test_guardian_summary_requires_guardian_binding_role(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-child", category="minor", guardian_active=True)
        primary_fence = _fence(
            actor_subject_id="parent-1", active_subject_id="person-child"
        )
        await _register_receipt(
            service,
            primary_fence,
            actor_subject_id="parent-1",
            purpose="guardian_summary",
            capability="guardian_summary_view",
            obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
        )
        await _register_consent(service, ("person-child",))
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
            policy=_policy(
                obligations=(
                    PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,
                )
            ),
            fence=primary_fence,  # primary-subject role is not enough
        )
        with pytest.raises(ActorNotAuthorizedError, match="guardian binding role"):
            await service.capture(context, _draft(), actor_subject_id="parent-1")

    async def test_propose_shared_rejects_stale_receipt(
        self, service: MemoryScopeService
    ) -> None:
        fence = _fence(family_space_id="family-1")
        await _register_receipt(
            service,
            fence,
            resource_owner_id="family-1",
            obligations=(
                PolicyObligation.POLICY_OBLIGATION_REQUIRE_SUBJECT_APPROVAL,
                PolicyObligation.POLICY_OBLIGATION_WRITE_POLICY_RECEIPT,
            ),
        )
        with pytest.raises(MemoryWriteRejected, match="promotion"):
            await service.propose_shared(
                actor_subject_id="person-a",
                family_space_id="family-1",
                co_subject_ids=("person-b",),
                title="t",
                content="c",
                source_evidence_ids=("evidence-1",),
                policy_receipt_id="receipt-99",
                consent_snapshot_id="consent-1",
                fence=fence,
            )

    async def test_propose_shared_requires_fence(
        self, service: MemoryScopeService
    ) -> None:
        with pytest.raises(WriteFenceMissingError, match="fence"):
            await service.propose_shared(
                actor_subject_id="person-a",
                family_space_id="family-1",
                co_subject_ids=("person-b",),
                title="t",
                content="c",
                source_evidence_ids=("evidence-1",),
                policy_receipt_id="receipt-1",
                consent_snapshot_id="consent-1",
                fence=None,  # type: ignore[arg-type]
            )

    async def test_expired_record_is_invisible_on_read(
        self, service: MemoryScopeService
    ) -> None:
        record = MemoryRecord(
            record_id="expired-1",
            scope=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            subject_id="person-a",
            resource_owner_id="person-a",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
            created_by_actor_id="person-a",
            retention_expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        await service.store.persist_record(
            record, actor_family_space_id=None
        )
        assert await service.get("expired-1", "person-a") is None
        assert await service.list_for_subject("person-a", "person-a") == ()
        assert not record.is_visible(datetime.now(UTC))


class TestParentReadsChildChat:
    """Section 6.5: parent gets summaries, never verbatim chat."""

    async def test_parent_cannot_read_child_private_chat(
        self, service: MemoryScopeService
    ) -> None:
        await _register_receipt(
            service,
            _fence(
                actor_subject_id="person-child",
                active_subject_id="person-child",
            ),
        )
        await _register_consent(service, ("person-child",))
        child_record = await service.capture(
            _context(
                _subject("person-child", category="minor", speaker="confirmed"),
                requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                fence=_fence(
                    actor_subject_id="person-child",
                    active_subject_id="person-child",
                ),
            ),
            _draft("我不想告诉妈妈"),
            actor_subject_id="person-child",
        )
        parent_view = await service.get(child_record.record_id, "parent-1")
        assert parent_view is None
        listed = await service.list_for_subject("person-child", "parent-1")
        assert listed == ()

    async def test_guardian_gets_summary_only_without_content(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-child", category="minor", guardian_active=True)
        guardian_fence = _fence(
            binding_role=BindingRole.BINDING_ROLE_GUARDIAN.value,
            actor_subject_id="parent-1",
            active_subject_id="person-child",
        )
        await _register_receipt(
            service,
            guardian_fence,
            actor_subject_id="parent-1",
            purpose="guardian_summary",
            capability="guardian_summary_view",
            obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
        )
        await _register_consent(service, ("person-child",))
        _register_guardian(
            service, "parent-1", frozenset({"person-child"})
        )
        record = await service.capture(
            _context(
                subject,
                requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
                policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
                fence=guardian_fence,
            ),
            _draft("学习时长 120 分钟"),
            actor_subject_id="parent-1",
        )
        view = await service.get(
            record.record_id,
            "parent-1",
        )
        assert view is not None
        assert "content" not in view.payload
        # The minor subject themselves sees the full summary record.
        own = await service.get(record.record_id, "person-child")
        assert own is not None

    async def test_non_guardian_cannot_read_summary(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-child", category="minor", guardian_active=True)
        guardian_fence = _fence(
            binding_role=BindingRole.BINDING_ROLE_GUARDIAN.value,
            actor_subject_id="parent-1",
            active_subject_id="person-child",
        )
        await _register_receipt(
            service,
            guardian_fence,
            actor_subject_id="parent-1",
            purpose="guardian_summary",
            capability="guardian_summary_view",
            obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
        )
        await _register_consent(service, ("person-child",))
        record = await service.capture(
            _context(
                subject,
                requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
                policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
                fence=guardian_fence,
            ),
            _draft("学习时长 120 分钟"),
            actor_subject_id="parent-1",
        )
        view = await service.get(record.record_id, "stranger-1")
        assert view is None
