"""PR-13: subject-scoped tutor API with server-owned assessment evidence."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from packages.contracts.generated.python.multi_subject_contracts import (
    ObligationParams,
    PolicyActionResourceFence,
    PolicyEffect,
    PolicyObligationSpec,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyReceiptV2 as ContractPolicyReceiptV2,
)
from services.control_api.app.main import create_app
from services.control_api.tests.identity_test_helpers import (
    install_test_identity_authority,
)
from services.guardian.domain import ConsentRecord
from services.policy.action_fence import (
    build_action_resource_fence,
    verify_action_resource_fence,
)
from services.tutor.authority import (
    FencedAssessmentAuthority,
    TutorAssessmentSignals,
    TutorFenceSnapshot,
    TutorReceiptExpectation,
    TutorReceiptVerification,
    TutorTurnCounters,
)


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> Any:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / f"{name}.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_IDENTITY_DB_PATH",
        str(tmp_path / f"{name}-identity.sqlite3"),
    )
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", f"{name}-auth-secret-long-enough-0123456789")
    monkeypatch.setenv(
        "MEMORIA_TRANSFER_EVIDENCE_SECRET",
        f"{name}-transfer-evidence-secret-32-bytes",
    )
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "archive-internal-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    app = create_app()
    install_test_identity_authority(
        app,
        secret=app.state.settings.transfer_evidence_key(),
    )
    return app


async def _register(client: AsyncClient, username: str) -> dict[str, Any]:
    response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": "safe-password"},
    )
    assert response.status_code == 201
    return response.json()


def _profile_dict(
    subject_id: str,
    actor_id: str,
    *,
    session_id: str = "voice-session-1",
    **overrides: Any,
) -> dict[str, Any]:
    """Stand-in session profile for the Policy WIP dependency gate.

    The persistent Session/Policy seam is still being implemented (PR-17):
    runtime profile issuance is blocked by the new canonical action-pair
    validation, so these tests exercise the tutor surface with an explicit
    fixed fence.  Production wiring stays fail-closed (503) until the real
    seam lands; this is a documented dependency gate, not a production path.
    """

    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "session_id": session_id,
        "actor_id": actor_id,
        "active_subject_id": subject_id,
        "device_id": "device-self",
        "binding_id": "binding-1",
        "binding_version": 1,
        "subject_revision": 0,
        "session_epoch": 1,
        "runtime_profile_id": "rp_test",
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    values.update(overrides)
    return values


def _install_assessment_authority(app: Any) -> FencedAssessmentAuthority:
    authority = FencedAssessmentAuthority(
        signing_key=app.state.settings.runtime_profile_signing_key(),
    )
    app.state.tutor_assessment_authority = authority
    return authority


def _seed_mastered_turn(
    authority: FencedAssessmentAuthority,
    *,
    voice_session_id: str,
    subject_id: str,
    runtime_profile_id: str,
    session_epoch: int,
    turn_id: int = 1,
) -> None:
    counters = TutorTurnCounters(generation_id=1, turn_id=turn_id, tool_epoch=3)
    authority.set_turn_state(
        voice_session_id,
        counters=counters,
        signals=TutorAssessmentSignals(
            subject_id=subject_id,
            runtime_profile_id=runtime_profile_id,
            session_epoch=session_epoch,
            task_id="english-past-story",
            evaluator_version="tutor-rubric-v2",
            generation_id=1,
            turn_id=turn_id,
            tool_epoch=3,
            support_level="none",
            completion="completed",
            correctness_score=0.95,
            attempt_count=3,
            criterion_ids=("english-past-story.criterion.v1",),
        ),
    )


def _practice_event_id(subject_id: str, key: str) -> str:
    return (
        "tutor-practice-event:"
        + str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"memoria:tutor-evidence:{subject_id}:{key}",
            )
        )
    )


def _obligation(code: str) -> PolicyObligationSpec:
    return PolicyObligationSpec(
        code=code,  # type: ignore[arg-type]
        params=ObligationParams(
            max_session_seconds=None,
            retention_ttl_seconds=None,
            quiet_hours=None,
            extras=(),
        ),
    )


def _install_action_receipt(
    app: Any,
    *,
    profile: dict[str, Any],
    subject_id: str,
    event_id: str,
    action_revision: int,
    generation_id: int,
    turn_id: int,
    tool_epoch: int,
    now: datetime,
    receipt_id: str = "receipt-action",
) -> PolicyActionResourceFence:
    """Simulate the future Policy seam: persist the canonical action receipt."""

    fence = build_action_resource_fence(
        capability="tutor",
        purpose="runtime_sensitive_action",
        action_resource_id=event_id,
        action_revision=action_revision,
        generation_id=generation_id,
        turn_id=turn_id,
        tool_epoch=tool_epoch,
        issued_at=now,
        valid_until=now + timedelta(minutes=5),
    )
    receipt = ContractPolicyReceiptV2(
        receipt_id=receipt_id,
        actor_id=profile["actor_id"],
        subject_id=subject_id,
        resource_owner_id=subject_id,
        device_id=profile["device_id"],
        capability="tutor",
        purpose="runtime_sensitive_action",
        effect=PolicyEffect.POLICY_EFFECT_ALLOW_WITH_OBLIGATIONS.value,
        reason_code="tutor_action_authorized",
        obligations=(_obligation("WRITE_SUBJECT_SCOPED_PROGRESS"),),
        policy_version="v2",
        context_hash="c" * 64,
        action_resource_fence=fence,
        action_fence_hash=fence.canonical_hash,
        consent_snapshot_ids=(),
        consent_snapshot_revisions=(),
        relationship_snapshot_ids=(),
        relationship_snapshot_revisions=(),
        binding_id=profile["binding_id"],
        binding_version=int(profile["binding_version"]),
        binding_canonical_hash=None,
        session_id=profile["session_id"],
        session_epoch=int(profile["session_epoch"]),
        runtime_profile_id=profile["runtime_profile_id"],
        subject_revision=int(profile["subject_revision"]),
        device_trust="trusted",
        data_classification="ephemeral",
        safety_state="normal",
        jurisdiction="CN",
        created_at=(now - timedelta(minutes=1)).isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
        exact_fence=False,
    )
    app.state.policy_receipt_writer.write(receipt)
    return fence


class _StrictReceiptVerifier:
    """Test double for the Policy transaction-bound verifier."""

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    async def verify_receipt(
        self,
        *,
        receipt_id: str,
        expectation: TutorReceiptExpectation,
        action_fence: PolicyActionResourceFence,
        now: datetime,
        connection: object | None = None,
    ) -> TutorReceiptVerification:
        receipt = self._writer.get(receipt_id)
        if receipt is None:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_not_found",
            )
        fence = getattr(receipt, "action_resource_fence", None)
        if not isinstance(fence, PolicyActionResourceFence):
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_version_unsupported",
            )
        if receipt.effect not in {"allow", "allow_with_obligations"}:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_not_allow",
            )
        if receipt.capability != expectation.capability:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_capability_mismatch",
            )
        if receipt.purpose != expectation.purpose:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_purpose_mismatch",
            )
        literal_fields = {
            "actor_id": expectation.actor_id,
            "subject_id": expectation.subject_id,
            "resource_owner_id": expectation.subject_id,
            "device_id": expectation.device_id,
            "binding_id": expectation.binding_id,
            "binding_version": expectation.binding_version,
            "session_id": expectation.session_id,
            "session_epoch": expectation.session_epoch,
            "runtime_profile_id": expectation.runtime_profile_id,
            "subject_revision": expectation.subject_revision,
        }
        for name, expected in literal_fields.items():
            if getattr(receipt, name) != expected:
                return TutorReceiptVerification(
                    ok=False,
                    receipt_id=receipt_id,
                    reason=f"receipt_fence_mismatch:{name}",
                )
        if not verify_action_resource_fence(fence):
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_action_fence_invalid",
            )
        for name in (
            "capability",
            "purpose",
            "action_resource_id",
            "action_revision",
            "generation_id",
            "turn_id",
            "tool_epoch",
        ):
            if getattr(fence, name) != getattr(action_fence, name):
                return TutorReceiptVerification(
                    ok=False,
                    receipt_id=receipt_id,
                    reason="receipt_action_fence_mismatch",
                )
        if not fence.issued_at <= now < fence.valid_until:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_expired",
            )
        if not receipt.created_at <= now < receipt.expires_at:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_expired",
            )
        receipt_obligations = {
            str(obligation.code) for obligation in receipt.obligations
        }
        missing = [
            code
            for code in expectation.required_obligations
            if code not in receipt_obligations
        ]
        if missing:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason=f"receipt_obligations_missing:{','.join(missing)}",
            )
        return TutorReceiptVerification(
            ok=True,
            receipt_id=receipt_id,
            expires_at=min(receipt.expires_at, fence.valid_until),
        )


def _install_fixed_fence(
    app: Any,
    *,
    profile: dict[str, Any],
    subject_id: str,
    receipt_ids: tuple[str, ...] = ("receipt-action",),
) -> None:
    """Install the fixed session fence (Policy/Session WIP dependency gate)."""

    now = datetime.now(UTC)
    fence = TutorFenceSnapshot(
        voice_session_id=profile["session_id"],
        actor_id=profile["actor_id"],
        active_subject_id=subject_id,
        device_id=profile["device_id"],
        binding_id=profile["binding_id"],
        binding_version=int(profile["binding_version"]),
        subject_revision=int(profile["subject_revision"]),
        session_epoch=int(profile["session_epoch"]),
        runtime_profile_id=profile["runtime_profile_id"],
        policy_receipt_ids=receipt_ids,
        expires_at=datetime.fromisoformat(profile["expires_at"]),
        resolved_at=now,
    )

    class _FixedFencePort:
        async def resolve_fence(
            self,
            *,
            actor_id: str,
            voice_session_id: str,
            now: datetime,
        ) -> TutorFenceSnapshot | None:
            if actor_id != fence.actor_id or voice_session_id != fence.voice_session_id:
                return None
            return fence

    app.state.tutor_session_fence = _FixedFencePort()


def _wire_action_flow(
    app: Any,
    *,
    profile: dict[str, Any],
    subject_id: str,
    receipt_ids: tuple[str, ...] = ("receipt-action",),
) -> FencedAssessmentAuthority:
    """Wire the assessment authority and the action-receipt verifier."""

    authority = _install_assessment_authority(app)
    app.state.tutor_receipt_verifier = _StrictReceiptVerifier(
        app.state.policy_receipt_writer
    )
    _install_fixed_fence(
        app,
        profile=profile,
        subject_id=subject_id,
        receipt_ids=receipt_ids,
    )
    return authority


@pytest.mark.asyncio
async def test_tutor_lessons_are_server_catalog_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env(monkeypatch, tmp_path, "lessons")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register(client, "tutor-lessons")
        auth = {"Authorization": f"Bearer {identity['access_token']}"}
        english = await client.get("/v1/tutor/lessons", headers=auth)
        homework = await client.get(
            "/v1/tutor/lessons",
            params={"focus": "tutor_homework"},
            headers=auth,
        )
    assert english.status_code == 200
    assert english.json()["count"] == 50
    assert homework.json()["count"] == 1


@pytest.mark.asyncio
async def test_unknown_session_and_missing_authorities_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env(monkeypatch, tmp_path, "fail-closed")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register(client, "tutor-fail-closed")
        auth = {"Authorization": f"Bearer {identity['access_token']}"}
        unknown = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "unknown-session-001"},
            json={
                "focus": "tutor_english",
                "task_id": "english-past-story",
                "voice_session_id": "voice-unknown",
            },
        )
        missing_param = await client.get(
            "/v1/tutor/progress",
            headers=auth,
        )
    assert unknown.status_code == 403
    assert unknown.json()["detail"] == {"code": "tutor_session_unknown"}
    assert missing_param.status_code == 422


@pytest.mark.asyncio
async def test_client_cannot_claim_outcome_or_skill_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _env(monkeypatch, tmp_path, "no-outcome")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register(client, "tutor-no-outcome")
        auth = {"Authorization": f"Bearer {identity['access_token']}"}
        claimed = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "claim-001"},
            json={
                "focus": "tutor_english",
                "task_id": "english-past-story",
                "voice_session_id": "voice-any",
                "outcome": "mastered",
                "skill_key": "past-story",
            },
        )
        forged = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "claim-002"},
            json={
                "focus": "tutor_english",
                "task_id": "english-past-story",
                "voice_session_id": "voice-any",
                "assessment": {
                    "event_id": "forged",
                    "subject_id": identity["user_id"],
                },
            },
        )
    assert claimed.status_code == 422
    assert forged.status_code == 422


@pytest.mark.asyncio
async def test_self_subject_practice_flow_is_server_scored_and_one_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _env(monkeypatch, tmp_path, "self-practice")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register(client, "tutor-self")
        profile = _profile_dict(identity["user_id"], identity["user_id"])
        voice_session_id = profile["session_id"]
        auth = {"Authorization": f"Bearer {identity['access_token']}"}

        authority = _wire_action_flow(
            app,
            profile=profile,
            subject_id=identity["user_id"],
        )
        _seed_mastered_turn(
            authority,
            voice_session_id=voice_session_id,
            subject_id=identity["user_id"],
            runtime_profile_id=profile["runtime_profile_id"],
            session_epoch=int(profile["session_epoch"]),
            turn_id=1,
        )
        _install_action_receipt(
            app,
            profile=profile,
            subject_id=identity["user_id"],
            event_id=_practice_event_id(identity["user_id"], "practice-turn-001"),
            action_revision=2,
            generation_id=1,
            turn_id=1,
            tool_epoch=3,
            now=datetime.now(UTC),
        )

        created = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "practice-create-001"},
            json={
                "focus": "tutor_english",
                "task_id": "english-past-story",
                "voice_session_id": voice_session_id,
            },
        )
        session_id = created.json()["session_id"]
        activated = await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "practice-active-001"},
            json={
                "action": "active",
                "expected_revision": 0,
                "voice_session_id": voice_session_id,
            },
        )
        practiced = await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "practice-turn-001"},
            json={
                "action": "practice",
                "expected_revision": 1,
                "duration_seconds": 600,
                "voice_session_id": voice_session_id,
            },
        )
        replay = await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "practice-turn-001"},
            json={
                "action": "practice",
                "expected_revision": 1,
                "duration_seconds": 600,
                "voice_session_id": voice_session_id,
            },
        )
        completed = await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "practice-complete-001"},
            json={
                "action": "completed",
                "expected_revision": 2,
                "voice_session_id": voice_session_id,
            },
        )
        progress = await client.get(
            "/v1/tutor/progress",
            params={"voice_session_id": voice_session_id},
            headers=auth,
        )
        stored = await app.state.tutor_store.study_progress(
            subject_id=identity["user_id"]
        )
        exported = await app.state.guardian_store.export_for_account(
            account_id=identity["user_id"]
        )

    assert created.status_code == 201, created.text
    assert created.json()["subject_id"] == identity["user_id"]
    assert created.json()["actor_id"] == identity["user_id"]
    assert activated.json()["revision"] == 1
    assert practiced.status_code == 200, practiced.text
    assert practiced.json()["revision"] == 2
    assert replay.json()["revision"] == 2
    assert completed.json()["status"] == "completed"
    assert progress.status_code == 200, progress.text
    assert progress.json()["mastered_skills"] == ["past-story"]
    assert progress.json()["practiced_seconds"] == 600
    assert progress.json()["subject_id"] == identity["user_id"]
    assert stored is not None
    assert stored.source_event_ids
    assert exported["tutor_study_progress"] is not None
    assert len(exported["tutor_practice_sessions"]) == 1


@pytest.mark.asyncio
async def test_practice_scoring_requires_the_assessment_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _env(monkeypatch, tmp_path, "no-authority")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register(client, "tutor-no-authority")
        profile = _profile_dict(identity["user_id"], identity["user_id"])
        voice_session_id = profile["session_id"]
        auth = {"Authorization": f"Bearer {identity['access_token']}"}
        _install_fixed_fence(app, profile=profile, subject_id=identity["user_id"])
        created = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "gate-create-001"},
            json={
                "focus": "tutor_english",
                "task_id": "english-past-story",
                "voice_session_id": voice_session_id,
            },
        )
        session_id = created.json()["session_id"]
        await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "gate-active-001"},
            json={
                "action": "active",
                "expected_revision": 0,
                "voice_session_id": voice_session_id,
            },
        )
        practiced = await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "gate-turn-001"},
            json={
                "action": "practice",
                "expected_revision": 1,
                "duration_seconds": 600,
                "voice_session_id": voice_session_id,
            },
        )
    assert practiced.status_code == 503
    assert practiced.json()["detail"] == {"code": "assessment_authority_unavailable"}


@pytest.mark.asyncio
async def test_practice_requires_a_field_exact_tutor_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _env(monkeypatch, tmp_path, "no-receipt")
    # Wire the strict verifier over the REAL profile-issuance receipts: none
    # of them is a runtime_sensitive_action receipt for this write, so the
    # profile receipt list can never authorize progress.
    authority = _install_assessment_authority(app)
    app.state.tutor_receipt_verifier = _StrictReceiptVerifier(
        app.state.policy_receipt_writer
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register(client, "tutor-no-receipt")
        profile = _profile_dict(identity["user_id"], identity["user_id"])
        voice_session_id = profile["session_id"]
        authority = app.state.tutor_assessment_authority
        assert isinstance(authority, FencedAssessmentAuthority)
        _seed_mastered_turn(
            authority,
            voice_session_id=voice_session_id,
            subject_id=identity["user_id"],
            runtime_profile_id=profile["runtime_profile_id"],
            session_epoch=int(profile["session_epoch"]),
        )
        _install_fixed_fence(app, profile=profile, subject_id=identity["user_id"])
        auth = {"Authorization": f"Bearer {identity['access_token']}"}
        created = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "receipt-create-001"},
            json={
                "focus": "tutor_english",
                "task_id": "english-past-story",
                "voice_session_id": voice_session_id,
            },
        )
        session_id = created.json()["session_id"]
        await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "receipt-active-001"},
            json={
                "action": "active",
                "expected_revision": 0,
                "voice_session_id": voice_session_id,
            },
        )
        practiced = await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "receipt-turn-001"},
            json={
                "action": "practice",
                "expected_revision": 1,
                "duration_seconds": 600,
                "voice_session_id": voice_session_id,
            },
        )
    assert practiced.status_code == 403
    assert practiced.json()["detail"] == {"code": "tutor_receipt_required"}


@pytest.mark.asyncio
async def test_stale_turn_fence_is_rejected_and_concurrent_requests_serialize(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _env(monkeypatch, tmp_path, "concurrent")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = await _register(client, "tutor-concurrent")
        profile = _profile_dict(identity["user_id"], identity["user_id"])
        voice_session_id = profile["session_id"]
        authority = _wire_action_flow(
            app,
            profile=profile,
            subject_id=identity["user_id"],
            receipt_ids=("receipt-action-a", "receipt-action-b"),
        )
        auth = {"Authorization": f"Bearer {identity['access_token']}"}
        created = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "concurrent-create-001"},
            json={
                "focus": "tutor_english",
                "task_id": "english-past-story",
                "voice_session_id": voice_session_id,
            },
        )
        session_id = created.json()["session_id"]
        await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "concurrent-active-001"},
            json={
                "action": "active",
                "expected_revision": 0,
                "voice_session_id": voice_session_id,
            },
        )
        _seed_mastered_turn(
            authority,
            voice_session_id=voice_session_id,
            subject_id=identity["user_id"],
            runtime_profile_id=profile["runtime_profile_id"],
            session_epoch=int(profile["session_epoch"]),
            turn_id=1,
        )
        now = datetime.now(UTC)
        _install_action_receipt(
            app,
            profile=profile,
            subject_id=identity["user_id"],
            event_id=_practice_event_id(identity["user_id"], "concurrent-turn-a"),
            action_revision=2,
            generation_id=1,
            turn_id=1,
            tool_epoch=3,
            now=now,
            receipt_id="receipt-action-a",
        )
        _install_action_receipt(
            app,
            profile=profile,
            subject_id=identity["user_id"],
            event_id=_practice_event_id(identity["user_id"], "concurrent-turn-b"),
            action_revision=2,
            generation_id=1,
            turn_id=1,
            tool_epoch=3,
            now=now,
            receipt_id="receipt-action-b",
        )

        async def practice_turn(key: str) -> int:
            response = await client.post(
                f"/v1/tutor/practice-sessions/{session_id}/events",
                headers={**auth, "Idempotency-Key": key},
                json={
                    "action": "practice",
                    "expected_revision": 1,
                    "duration_seconds": 600,
                    "voice_session_id": voice_session_id,
                },
            )
            return response.status_code

        statuses = await asyncio.gather(
            practice_turn("concurrent-turn-a"),
            practice_turn("concurrent-turn-b"),
        )
        assert sorted(statuses) == [200, 403]
        # The same-turn fence is consumed once: a later request on the same
        # turn is stale even with a fresh idempotency key.
        stale = await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "concurrent-turn-c"},
            json={
                "action": "practice",
                "expected_revision": 2,
                "duration_seconds": 600,
                "voice_session_id": voice_session_id,
            },
        )
        assert stale.status_code == 403
        assert stale.json()["detail"] == {"code": "stale_event_counters"}


@pytest.mark.asyncio
async def test_actor_subject_mismatch_and_cross_family_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _env(monkeypatch, tmp_path, "cross-family")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "tutor-owner")
        child_id = "subject-child-0001"
        session_id = "voice-family-1"
        # A confirmed device profile belongs to the child subject; the owner
        # account is a different actor.  The web tutor API must fail closed
        # for the guardian actor and for any other family's member.
        fence = TutorFenceSnapshot(
            voice_session_id=session_id,
            actor_id=owner["user_id"],
            active_subject_id=child_id,
            device_id="device-family",
            binding_id="binding-family",
            binding_version=1,
            subject_revision=0,
            session_epoch=1,
            runtime_profile_id="rp_family",
            policy_receipt_ids=("receipt-family",),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            resolved_at=datetime.now(UTC),
        )

        class _FamilyFencePort:
            async def resolve_fence(
                self,
                *,
                actor_id: str,
                voice_session_id: str,
                now: datetime,
            ) -> TutorFenceSnapshot | None:
                if actor_id != fence.actor_id or voice_session_id != fence.voice_session_id:
                    return None
                return fence

        app.state.tutor_session_fence = _FamilyFencePort()
        owner_auth = {"Authorization": f"Bearer {owner['access_token']}"}
        for_guardian = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**owner_auth, "Idempotency-Key": "family-create-001"},
            json={
                "focus": "tutor_english",
                "task_id": "english-past-story",
                "voice_session_id": session_id,
            },
        )
        assert for_guardian.status_code == 403
        assert for_guardian.json()["detail"] == {
            "code": "tutor_actor_subject_mismatch"
        }

        # A member of another family cannot borrow this session.
        stranger = await _register(client, "tutor-stranger")
        stranger_auth = {"Authorization": f"Bearer {stranger['access_token']}"}
        borrowed = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**stranger_auth, "Idempotency-Key": "family-create-002"},
            json={
                "focus": "tutor_english",
                "task_id": "english-past-story",
                "voice_session_id": session_id,
            },
        )
        assert borrowed.status_code == 403
        assert borrowed.json()["detail"] == {"code": "tutor_session_unknown"}


@pytest.mark.asyncio
async def test_minor_tutor_progress_requires_memory_retention_consent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ConsentView:
        def __init__(self) -> None:
            self.allowed = False

        async def active_consent(self, **_: Any) -> ConsentRecord | None:
            if not self.allowed:
                return None
            return ConsentRecord(
                consent_id="00000000-0000-0000-0000-000000000001",
                link_id="00000000-0000-0000-0000-000000000002",
                consent_kind="memory_retention",
                policy_version="minor-memory-v1",
                granted_at=datetime.now(UTC),
                evidence_event_id="minor-memory-consent",
            )

    app = _env(monkeypatch, tmp_path, "minor-tutor")
    consents = ConsentView()
    app.state.guardian_store = consents
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "minor-tutor", "password": "safe-password"},
            )
        ).json()
        app.state.memory_store.update_subject_profile(
            user_id=identity["user_id"],
            subject_category="minor",
            birth_year_band="14_17",
            now=datetime.now(UTC).isoformat(),
        )
        refreshed = (
            await client.post(
                "/v1/auth/login",
                json={"username": "minor-tutor", "password": "safe-password"},
            )
        ).json()
        auth = {"Authorization": f"Bearer {refreshed['access_token']}"}
        lessons = await client.get("/v1/tutor/lessons", headers=auth)
        denied = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "minor-practice-001"},
            json={
                "focus": "tutor_homework",
                "task_id": "homework-self-guided",
                "voice_session_id": "voice-minor",
            },
        )
        consents.allowed = True
        allowed = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "minor-practice-001"},
            json={
                "focus": "tutor_homework",
                "task_id": "homework-self-guided",
                "voice_session_id": "voice-minor",
            },
        )

    assert lessons.status_code == 200
    assert denied.status_code == 403
    assert denied.json()["detail"] == {
        "code": "guardian_consent_required",
        "capability": "memory_retention",
    }
    assert allowed.status_code == 403
    assert allowed.json()["detail"] == {"code": "tutor_session_unknown"}
