"""Shared fixtures for signed RuntimeProfile wire payloads.

The wire payload mirrors the Control API ``runtime_profile_wire_payload``
exactly (``signature_schema``, flat canonical fields, nested ``persona``).
Per the cross-agent contract the wire payload IS the canonical signed
document: the signature is HMAC-SHA256 over
``json.dumps(payload_without_signature, sort_keys=True, separators=(",", ":"))``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.receipt_evidence import evidence_from_policy_receipt
from services.agent.src.runtime_profile import VerifiedRuntimeProfile, parse_runtime_profile
from services.policy.action_fence import build_action_resource_fence
from services.policy.receipts import PolicyReceiptV2

TEST_VERIFY_KEY = "test-runtime-profile-verify-key"
SIGNATURE_SCHEMA = "runtime-profile-v1"
UNKNOWN_SAFE_OBLIGATIONS = [
    "DO_NOT_PERSIST",
    "NO_MODEL_TRAINING",
    "DO_NOT_WRITE_LEARNING_PROGRESS",
    "REQUIRE_SPEAKER_CONFIRMATION",
]


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_wire_payload(**overrides: object) -> dict[str, object]:
    """Build the backend-shaped signed runtime profile (serialize_profile)."""

    payload: dict[str, object] = {
        "signature_schema": SIGNATURE_SCHEMA,
        "runtime_profile_id": "rp_01J_test",
        "device_id": "dev_01J_test",
        "session_id": "ses_01J_test",
        "actor_id": "actor_01J_test",
        "binding_id": "bind_01J_test",
        "binding_version": 1,
        "active_subject_id": "person_child",
        "subject_revision": 1,
        "subject_category": "minor",
        "age_band": "under_14",
        "speaker_state": "confirmed",
        "speaker_confidence": 0.95,
        "service_mode": "student_minor",
        "persona_assignment_id": "starlight:v4",
        "persona": {
            "persona_id": "starlight",
            "version": 4,
            "relationship_stage": "familiar",
        },
        "policy_bundle_version": "cn-minor-v5",
        "capabilities": ["chat", "tutor", "english_practice"],
        "obligations": ["WRITE_POLICY_RECEIPT", "WRITE_SUBJECT_SCOPED_PROGRESS"],
        "policy_receipt_ids": ["receipt_1"],
        "session_epoch": 2,
        "issued_at": "2098-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    payload.update(overrides)
    payload["signature"] = sign_wire_payload(payload, TEST_VERIFY_KEY)
    return payload


def sign_wire_payload(payload: dict[str, object], key: str) -> str:
    """Sign exactly the wire payload minus the signature field."""

    canonical = {k: v for k, v in payload.items() if k != "signature"}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key.encode("utf-8"), encoded, hashlib.sha256).hexdigest()


def owner_profile_for_session(session_id: str) -> VerifiedRuntimeProfile:
    """A signed adult-owner profile bound to one exact session."""

    payload = canonical_wire_payload(
        session_id=session_id,
        runtime_profile_id=f"rp_owner_{session_id}",
        active_subject_id="person_owner",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        service_mode="adult_companion",
        session_epoch=1,
        capabilities=[
            "chat",
            "tutor",
            "english_practice",
            "memory_capture",
            "memory_recall_private",
        ],
    )
    verified = parse_runtime_profile(payload, verify_key=TEST_VERIFY_KEY)
    assert verified is not None
    return verified


def personal_voice_profile(
    session_id: str, *, mode: str = "self_preview"
) -> VerifiedRuntimeProfile:
    """A signed adult profile carrying personal/cloned voice capability."""

    payload = canonical_wire_payload(
        session_id=session_id,
        runtime_profile_id=f"rp_voice_{session_id}",
        active_subject_id="person_owner",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        service_mode=mode,
        session_epoch=1,
        capabilities=[
            "chat",
            "voice_clone_use",
            "digital_self_preview",
            "memory_recall_private",
        ],
    )
    verified = parse_runtime_profile(payload, verify_key=TEST_VERIFY_KEY)
    assert verified is not None
    return verified


class FakeReceiptVerifier:
    """Test verifier: produces exact evidence for configured capabilities.

    Never a production path: it mimics a Control receipt authority that has
    validated the receipt, and supports tamper overrides for negative tests
    (wrong device/epoch/expiry) that must fail the per-fence binding check.
    """

    def __init__(
        self,
        capabilities: tuple[str, ...] = ("memory_capture",),
        *,
        expires_at: datetime | None = None,
        tamper: dict[str, object] | None = None,
        receipt_id_override: str | None = None,
    ) -> None:
        self._capabilities = set(capabilities)
        self._expires_at = expires_at or datetime(2099, 1, 1, tzinfo=UTC)
        self._tamper = tamper or {}
        self._receipt_id_override = receipt_id_override

    def verify_receipt(
        self,
        *,
        capability: str,
        profile: object,
    ) -> object | None:
        if capability not in self._capabilities:
            return None
        purpose = {
            "memory_capture": "memory_capture",
            "raw_audio_retention": "raw_audio",
            "model_training_contribution": "model_training",
            "chat": "user_request",
        }[capability]
        overrides = {
            "actor_id": profile.actor_id,
            "subject_id": profile.active_subject_id,
            "device_id": profile.device_id,
            "binding_id": profile.binding_id,
            "binding_version": profile.binding_version,
            "runtime_profile_id": profile.runtime_profile_id,
            "session_id": profile.session_id,
            "session_epoch": profile.session_epoch,
            "expires_at": self._expires_at,
            "purpose": purpose,
        }
        overrides.update(self._tamper)
        created_at = datetime(2019, 1, 1, tzinfo=UTC)
        expires_at = overrides["expires_at"]
        assert isinstance(expires_at, datetime)
        action_resource_fence = build_action_resource_fence(
            capability=capability,
            purpose=purpose,
            action_resource_id=f"action-{self._receipt_id_override or 'test'}",
            action_revision=max(profile.session_epoch, 1),
            generation_id=0,
            turn_id=0,
            tool_epoch=0,
            issued_at=created_at,
            valid_until=max(expires_at, created_at + timedelta(seconds=1)),
        )
        receipt = PolicyReceiptV2(
            receipt_id=self._receipt_id_override
            or (profile.policy_receipt_ids[0] if profile.policy_receipt_ids else "orphan"),
            actor_id=overrides["actor_id"],
            subject_id=overrides["subject_id"],
            resource_owner_id=profile.actor_id,
            device_id=overrides["device_id"],
            capability=capability,
            purpose=overrides["purpose"],
            effect="allow",
            reason_code="exact_fence_consent",
            obligations=(),
            policy_version="cn-v2",
            context_hash="0" * 64,
            consent_snapshot_ids=(),
            consent_snapshot_revisions=(),
            relationship_snapshot_ids=(),
            relationship_snapshot_revisions=(),
            binding_id=overrides["binding_id"],
            binding_version=overrides["binding_version"],
            binding_canonical_hash="0" * 64,
            action_resource_fence=action_resource_fence,
            action_fence_hash=action_resource_fence.canonical_hash,
            session_id=overrides["session_id"],
            session_epoch=overrides["session_epoch"],
            runtime_profile_id=overrides["runtime_profile_id"],
            subject_revision=profile.subject_revision,
            device_trust="trusted",
            data_classification="private",
            safety_state="normal",
            jurisdiction="CN",
            created_at=_rfc3339(created_at),
            expires_at=_rfc3339(expires_at),
            exact_fence=False,
        )
        try:
            return evidence_from_policy_receipt(
                receipt,
                capability=capability,
                profile=profile,
            )
        except ValueError:
            return None


def install_receipt_verifier(
    runtime: object,
    *,
    capabilities: tuple[str, ...] = ("memory_capture",),
    expires_at: datetime | None = None,
    tamper: dict[str, object] | None = None,
) -> None:
    """Install a fake verifier port; sensitive persistence stays closed
    without it (default-deny)."""

    runtime.orchestrator.runtime_profiles.receipt_verifier = FakeReceiptVerifier(
        capabilities,
        expires_at=expires_at,
        tamper=tamper,
    )


def install_static_refresher(runtime: object) -> None:
    """Install a turn-boundary refresher that re-applies the current profile.

    Mirrors production wiring (ModePolicyClient fetch); without it the
    runtime revokes the profile at the next turn boundary (fail-closed).
    """

    async def _static_refresh() -> VerifiedRuntimeProfile | None:
        return runtime.orchestrator.runtime_profiles.current

    runtime.set_runtime_profile_refresher(_static_refresh)


def install_playback_stop_seam(runtime: object) -> None:
    """Install a fake production playback stop/flush owner (epoch drain)."""

    async def _stop() -> None:
        runtime.orchestrator.playback.playing = False
        runtime.orchestrator.playback.pcm_played.clear()

    runtime.set_playback_stop_seam(_stop)


def bind_owner_policy(
    runtime: object,
    *,
    policy_version: str = "test-policy",
    private_context: bool = True,
    owner_evidence: bool = True,
    tools: bool = True,
    voice_profile: bool = True,
    shadow_low_sensitivity_persona: bool = False,
    include_voice_clone: bool = False,
    include_raw_audio: bool = False,
) -> None:
    """Bind a companion ModePolicy that carries a signed owner profile."""

    policy = ModePolicy.companion_for_test(
        policy_version=policy_version,
        private_context=private_context,
        owner_evidence=owner_evidence,
        tools=tools,
        voice_profile=voice_profile,
        shadow_low_sensitivity_persona=shadow_low_sensitivity_persona,
    )
    capabilities = [
        "chat",
        "tutor",
        "english_practice",
        "memory_capture",
        "memory_recall_private",
    ]
    if include_voice_clone:
        capabilities.append("voice_clone_use")
    if include_raw_audio:
        capabilities.append("raw_audio_retention")
    owner_payload = canonical_wire_payload(
        session_id=runtime.session_id,
        runtime_profile_id=f"rp_owner_{runtime.session_id}",
        active_subject_id="person_owner",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        service_mode="adult_companion",
        session_epoch=1,
        capabilities=capabilities,
    )
    verified = parse_runtime_profile(owner_payload, verify_key=TEST_VERIFY_KEY)
    assert verified is not None
    runtime.orchestrator.runtime_profiles.expected_device_id = "dev_01J_test"
    install_receipt_verifier(
        runtime,
        capabilities=("memory_capture",) + (("raw_audio_retention",) if include_raw_audio else ()),
    )

    install_static_refresher(runtime)
    install_playback_stop_seam(runtime)
    policy = replace(policy, runtime_profile=verified)
    runtime.set_mode_policy(policy)
