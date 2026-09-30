"""Device turns reach the archive under the profile Control actually issues.

Control never lists ``memory_capture`` in a signed Runtime Profile: it is an
action-time capability (``PROFILE_ISSUE_DEFERRED_CAPABILITIES``), and the
Agent has no production receipt verifier for it.  Before this fix the archive
gate required the deferred capability, so no committed turn from any
production session was ever handed to the archive.  An adult's
long-term-memory authority is the profile's own ``memory_recall_private``
grant, which Policy decides under the same consent, delegation and
trusted-device rules.  A minor's capture decision also minimises what is kept
(``PERSIST_AGGREGATE_ONLY``), which only an action-time receipt carries.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    ObligationParams,
    PolicyObligation,
    PolicyObligationSpec,
)
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.media_agent_factory import ProductionMediaSessionFactory
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.obligation_executor import decide_persistence
from services.agent.src.runtime_profile import VerifiedRuntimeProfile, parse_runtime_profile
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.session_runtime.profile_service import (
    PROFILE_ISSUE_DEFERRED_CAPABILITIES,
    RuntimeProfile,
    runtime_profile_wire_payload,
    sign_runtime_profile_payload,
)

_KEY = "device-archive-persistence-key"
_ACTOR = "person-parent"
_CHILD = "person-child"
_DEVICE = "dev-archive"
_BINDING = "bind-archive"


def _obligation(code: PolicyObligation, **params: Any) -> PolicyObligationSpec:
    values: dict[str, Any] = {
        "max_session_seconds": None,
        "retention_ttl_seconds": None,
        "quiet_hours": None,
        "extras": (),
    }
    values.update(params)
    return PolicyObligationSpec(code=code, params=ObligationParams(**values))


def _control_profile(
    session_id: str,
    *,
    capabilities: tuple[str, ...],
    minor: bool = True,
    unknown_safe: bool = False,
    extra_obligations: tuple[PolicyObligation, ...] = (),
) -> VerifiedRuntimeProfile:
    """Sign a profile the way Control's Session Runtime issues it."""

    now = datetime.now(UTC)
    if unknown_safe:
        subject: str | None = None
        category, age_band, speaker_state, mode = "unknown", "unknown", "unconfirmed", "unknown_safe"
        obligations = tuple(
            _obligation(code)
            for code in (
                PolicyObligation.POLICY_OBLIGATION_DO_NOT_PERSIST,
                PolicyObligation.POLICY_OBLIGATION_DO_NOT_WRITE_LEARNING_PROGRESS,
                PolicyObligation.POLICY_OBLIGATION_NO_MODEL_TRAINING,
                PolicyObligation.POLICY_OBLIGATION_REQUIRE_SPEAKER_CONFIRMATION,
            )
        )
    elif minor:
        subject = _CHILD
        category, age_band, speaker_state, mode = "minor", "under_14", "confirmed", "student_minor"
        obligations = (
            _obligation(
                PolicyObligation.POLICY_OBLIGATION_MAX_SESSION_SECONDS,
                max_session_seconds=1800,
            ),
            _obligation(PolicyObligation.POLICY_OBLIGATION_NO_MODEL_TRAINING),
        )
    else:
        subject = _ACTOR
        category, age_band, speaker_state, mode = "adult", "adult", "confirmed", "adult_companion"
        obligations = (_obligation(PolicyObligation.POLICY_OBLIGATION_NO_MODEL_TRAINING),)
    issued = RuntimeProfile(
        runtime_profile_id=f"rp-{session_id}",
        device_id=_DEVICE,
        session_id=session_id,
        actor_id=_ACTOR,
        binding_id=_BINDING,
        binding_version=3,
        active_subject_id=subject,
        subject_revision=1 if subject is not None else 0,
        subject_category=category,  # type: ignore[arg-type]
        age_band=age_band,  # type: ignore[arg-type]
        speaker_state=speaker_state,  # type: ignore[arg-type]
        speaker_confidence=None,
        service_mode=mode,  # type: ignore[arg-type]
        persona_assignment_id="starlight:v1",
        persona_id="starlight",
        persona_version=1,
        relationship_stage="new",
        policy_bundle_version="policy-v2",
        capabilities=capabilities,  # type: ignore[arg-type]
        obligations=obligations + tuple(_obligation(code) for code in extra_obligations),
        policy_receipt_ids=tuple(f"receipt-{name}" for name in capabilities),
        session_epoch=1,
        issued_at=now,
        expires_at=now + timedelta(minutes=30),
        signature="",
    )
    payload = runtime_profile_wire_payload(issued)
    payload["signature"] = sign_runtime_profile_payload(payload, signing_key=_KEY.encode())
    verified = parse_runtime_profile(payload, verify_key=_KEY)
    assert verified is not None
    return verified


def _device_runtime(session_id: str, profile: VerifiedRuntimeProfile) -> DuplexRuntime:
    """Wire the runtime exactly as the production media factory does."""

    factory = ProductionMediaSessionFactory(
        settings=SimpleNamespace(
            use_paralinguistic_tags=False,
            speaker_enroll_speech_ms=1_000,
            speaker_enroll_timeout_ms=10_000,
            speaker_accept_threshold=0.8,
            speaker_min_verify_speech_ms=800,
        ),
        llm_factory=object(),
    )
    identity = SessionIdentity(
        session_id,
        account_id=_ACTOR,
        device_id=_DEVICE,
        client_type="device",
        subject_id=profile.profile.active_subject_id or "",
        binding_id=_BINDING,
        binding_version=3,
        runtime_profile_version=1,
    )
    runtime = factory._new_runtime(
        session_id,
        SimpleNamespace(pool=object()),
        device_id=identity.device_id,
        identity=identity,
    )
    runtime.set_mode_policy(ModePolicy.from_runtime_profile(profile))
    assert runtime.orchestrator.runtime_profiles.current is profile
    return runtime


async def _archived_turn(runtime: DuplexRuntime) -> list[dict[str, object]]:
    archived: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        archived.append(event)

    runtime.set_evidence_publisher(capture)
    fence = runtime.fence
    runtime.publish_transcript(
        speaker="user", text="我今天学会骑自行车了。", final=True, fence=fence
    )
    runtime.publish_transcript(
        speaker="assistant",
        text="太棒了，骑车时记得戴头盔。",
        final=True,
        heard=True,
        fence=fence,
    )
    await asyncio.sleep(0)
    await runtime.close()
    return archived


def test_control_never_issues_memory_capture_in_a_runtime_profile() -> None:
    # The gate below must not depend on a capability no profile carries.
    assert "memory_capture" in PROFILE_ISSUE_DEFERRED_CAPABILITIES


@pytest.mark.asyncio
async def test_adult_device_turn_is_archived_under_the_profile_memory_grant() -> None:
    session_id = "device-archive-adult"
    subject = _ACTOR
    profile = _control_profile(
        session_id,
        capabilities=("chat", "tutor", "english_practice", "memory_recall_private"),
        minor=False,
    )
    runtime = _device_runtime(session_id, profile)

    archived = await _archived_turn(runtime)

    assert [event["event_type"] for event in archived] == [
        "speech.utterance_finalized",
        "assistant.playout_stopped",
    ]
    for event in archived:
        assert event["session_id"] == session_id
        assert event["session_epoch"] == 1
        assert event["device_id"] == _DEVICE
        assert event["actor_id"] == _ACTOR
        assert event["binding_id"] == _BINDING
        assert event["binding_version"] == 3
        assert event["active_subject_id"] == subject
        assert event["subject_revision"] == 1
        assert event["runtime_profile_id"] == f"rp-{session_id}"
        assert event["memory_scope"] == "personal_private"
        assert event["no_model_training"] is True
        # No memory_capture receipt exists at session level; the archive
        # re-verifies the same signed profile instead of a claimed receipt.
        assert event["policy_receipt_id"] is None
    assert archived[0]["payload"]["text"] == "我今天学会骑自行车了。"  # type: ignore[index]


@pytest.mark.asyncio
async def test_consented_minor_waits_for_the_action_time_capture_receipt() -> None:
    # Policy decides a minor's memory_capture with PERSIST_AGGREGATE_ONLY and
    # RETENTION_TTL; the recall grant carries neither, so it is not enough.
    session_id = "device-archive-minor"
    profile = _control_profile(
        session_id,
        capabilities=("chat", "tutor", "english_practice", "memory_recall_private"),
        minor=True,
    )
    runtime = _device_runtime(session_id, profile)

    assert await _archived_turn(runtime) == []


@pytest.mark.asyncio
async def test_profile_without_memory_grant_keeps_turns_out_of_the_archive() -> None:
    # An untrusted device or a subject without memory consent gets a profile
    # that only allows conversation: nothing may be persisted.
    session_id = "device-archive-no-grant"
    profile = _control_profile(
        session_id, capabilities=("chat", "tutor", "english_practice"), minor=False
    )
    runtime = _device_runtime(session_id, profile)

    assert await _archived_turn(runtime) == []


@pytest.mark.asyncio
async def test_unknown_safe_device_profile_persists_nothing() -> None:
    session_id = "device-archive-unknown-safe"
    profile = _control_profile(session_id, capabilities=("chat",), unknown_safe=True)
    runtime = _device_runtime(session_id, profile)

    assert await _archived_turn(runtime) == []


@pytest.mark.asyncio
async def test_memory_grant_without_a_bound_device_persists_nothing() -> None:
    # P1-9: a profile that cannot be tied to this session's device authorizes
    # no sensitive effect, persistence included.
    session_id = "device-archive-unbound"
    profile = _control_profile(
        session_id, capabilities=("chat", "memory_recall_private"), minor=False
    )
    runtime = DuplexRuntime.create(session_id=session_id)
    runtime.set_mode_policy(ModePolicy.from_runtime_profile(profile))
    assert runtime.orchestrator.runtime_profiles.current is profile

    assert await _archived_turn(runtime) == []


@pytest.mark.asyncio
async def test_degraded_epoch_stops_archiving_the_withdrawn_profile() -> None:
    session_id = "device-archive-degraded"
    profile = _control_profile(
        session_id, capabilities=("chat", "memory_recall_private"), minor=False
    )
    runtime = _device_runtime(session_id, profile)
    runtime.degrade_runtime_profile()

    assert await _archived_turn(runtime) == []


def test_memory_grant_obeys_signed_obligations_and_leaves_raw_audio_closed() -> None:
    granted = _control_profile(
        "grant-rules", capabilities=("chat", "memory_recall_private"), minor=False
    )
    decision = decide_persistence(granted, session_memory_grant=True)
    assert decision.allowed is True
    assert decision.aggregate_only is False
    # Raw audio keeps needing its own action-time receipt.
    assert decision.raw_audio_allowed is False
    assert decision.memory_capture_receipt_id is None
    # The caller's grant flag alone never opens a profile that lacks the grant.
    ungranted = _control_profile("grant-rules-none", capabilities=("chat",), minor=False)
    assert decide_persistence(ungranted, session_memory_grant=True).allowed is False
    assert decide_persistence(granted).allowed is False
    minor = _control_profile("grant-rules-minor", capabilities=("chat", "memory_recall_private"))
    assert decide_persistence(minor, session_memory_grant=True).allowed is False
    forbidden = _control_profile(
        "grant-rules-forbidden",
        capabilities=("chat", "memory_recall_private"),
        minor=False,
        extra_obligations=(PolicyObligation.POLICY_OBLIGATION_DO_NOT_PERSIST,),
    )
    assert decide_persistence(forbidden, session_memory_grant=True).allowed is False
    aggregate = _control_profile(
        "grant-rules-aggregate",
        capabilities=("chat", "memory_recall_private"),
        minor=False,
        extra_obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
    )
    aggregated = decide_persistence(aggregate, session_memory_grant=True)
    assert aggregated.allowed is True
    assert aggregated.aggregate_only is True
