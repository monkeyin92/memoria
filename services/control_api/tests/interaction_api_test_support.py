"""Shared fixtures and helpers for the split interaction API tests."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyReceiptV2,
    RuntimeProfileSignedV2,
    RuntimeProfileV2,
)
from services.archive.memory_domain import (
    MemorySearchItem,
    MemorySearchResult,
    PersonItem,
)
from services.digital_self.domain import (
    DigitalSelfVersion,
    VersionNotFoundError,
)
from services.persona.domain import PersonaCapsule, PersonaCapsuleEntry
from services.policy.context import PolicyContext
from services.policy.engine import PolicyEngine
from services.session_runtime.postgres_store import SessionRuntimeContext
from services.session_runtime.profile_service import (
    canonical_runtime_decision_purpose,
    sign_runtime_profile_payload,
)
from services.session_runtime.service import PersistentSessionDenied


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-test-s2")
    monkeypatch.setenv("READINESS_GATE_TTL_S", "86400")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv(
        "MEMORIA_INTERACTION_POLICY_TOKEN", "interaction-policy-token-that-is-long-enough"
    )
    monkeypatch.setenv("MEMORIA_RESPONSE_PLAN_TOKEN", "response-plan-token-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_EVOLUTION_CONTROL_TOKEN", "evolution-control-token-test")


_RUNTIME_PROFILE_SIGNING_KEY = b"interaction-test-runtime-profile-signing-key"


@dataclass(frozen=True)
class _FakeBindingEvidence:
    binding_id: str
    version: int
    device_id: str
    status: str
    declared_mode: str
    valid_from: datetime
    valid_until: datetime
    canonical_hash: str

    def is_active_at(self, now: datetime) -> bool:
        return self.status == "active" and self.valid_from <= now < self.valid_until


@dataclass(frozen=True)
class _FakeConsentParams:
    max_session_seconds: int | None = None
    retention_ttl_seconds: int | None = None
    quiet_hours: tuple[str, str] | None = None
    extras: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _FakeConsentEvidence:
    consent_id: str
    version: int
    snapshot_id: str
    status: str
    subject_id: str
    resource_owner_id: str
    actor_id: str
    actor_kind: str
    device_id: str | None
    binding_id: str
    binding_version: int
    capability: str
    purpose: str
    policy_version: str
    evidence_id: str
    offer_id: str
    idempotency_key: str | None
    params: _FakeConsentParams
    valid_from: datetime
    valid_until: datetime
    supersedes_consent_id: str | None
    superseded_by_consent_id: str | None
    canonical_hash: str

    def is_effective_at(self, now: datetime) -> bool:
        return self.status == "active" and self.valid_from <= now < self.valid_until

    def matches(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: str,
    ) -> bool:
        return (
            self.subject_id == subject_id
            and self.binding_id == binding_id
            and self.binding_version == binding_version
            and self.capability == capability
        )


@dataclass(frozen=True)
class _FakeConsentSnapshotEvidence:
    snapshot_id: str
    revision: int
    canonical_hash: str
    status: str
    subject_id: str
    binding_id: str
    binding_version: int
    grant_refs: tuple[tuple[str, int, str], ...]
    valid_from: datetime
    valid_until: datetime

    def is_current_at(self, now: datetime) -> bool:
        return self.status == "current" and self.valid_from <= now < self.valid_until

    def contains(self, evidence: object) -> bool:
        return (
            isinstance(evidence, _FakeConsentEvidence)
            and evidence.snapshot_id == self.snapshot_id
            and evidence.binding_id == self.binding_id
        )


class _SignedRuntimeProfileAuthority:
    def __init__(self, profile: RuntimeProfileSignedV2) -> None:
        self.profile = profile
        self.context = SessionRuntimeContext.from_profile(profile, profile_revision=1)

    async def current(
        self, *, actor_id: str, session_id: str, now: datetime
    ) -> tuple[RuntimeProfileSignedV2, SessionRuntimeContext]:
        assert now.tzinfo is not None
        return self.profile, self.context

    async def authorize_action(
        self,
        *,
        actor_id: str,
        session_id: str,
        runtime_profile_id: str,
        capability: str,
        session_epoch: int,
        generation_id: int,
        turn_id: int,
        tool_epoch: int,
        data_classification: str,
        safety_state: str,
        now: datetime,
    ) -> PolicyReceiptV2:
        """Mirror the production action authority against the current profile.

        The fence advance itself is validated by the caller's transaction in
        production; the HTTP seam pre-validates staleness/forgery, so this fake
        only enforces the profile/actor/epoch identity and then lets the real
        PolicyEngine produce the complete immutable receipt.
        """
        if (
            runtime_profile_id != self.profile.runtime_profile_id
            or actor_id != self.profile.actor_id
            or session_id != self.profile.session_id
            or session_epoch != self.profile.session_epoch
        ):
            raise PersistentSessionDenied("action authority profile/actor/epoch mismatch")
        if (
            self.profile.active_subject_id is None
            or self.profile.subject_category in {None, "unknown"}
            or self.profile.speaker_state != "confirmed"
        ):
            raise PersistentSessionDenied("action authority subject unavailable")
        binding = _FakeBindingEvidence(
            binding_id=self.profile.binding_id,
            version=self.profile.binding_version,
            device_id=self.profile.device_id,
            status="active",
            declared_mode="self_use",
            valid_from=now - timedelta(hours=1),
            valid_until=now + timedelta(hours=1),
            canonical_hash="b" * 64,
        )
        snapshot_id = f"consent-snapshot-{self.profile.active_subject_id}"
        consent = _FakeConsentEvidence(
            consent_id=f"consent-{capability}",
            version=1,
            snapshot_id=snapshot_id,
            status="active",
            subject_id=self.profile.active_subject_id,
            resource_owner_id=self.profile.active_subject_id,
            actor_id=actor_id,
            actor_kind="subject",
            device_id=self.profile.device_id,
            binding_id=self.profile.binding_id,
            binding_version=self.profile.binding_version,
            capability=capability,
            purpose=canonical_runtime_decision_purpose(capability),
            policy_version="runtime-action-test-v1",
            evidence_id=f"evidence-{capability}",
            offer_id=f"offer-{capability}",
            idempotency_key=None,
            params=_FakeConsentParams(),
            valid_from=now - timedelta(hours=1),
            valid_until=now + timedelta(hours=1),
            supersedes_consent_id=None,
            superseded_by_consent_id=None,
            canonical_hash="c" * 64,
        )
        snapshot = _FakeConsentSnapshotEvidence(
            snapshot_id=snapshot_id,
            revision=1,
            canonical_hash="d" * 64,
            status="current",
            subject_id=self.profile.active_subject_id,
            binding_id=self.profile.binding_id,
            binding_version=self.profile.binding_version,
            grant_refs=((consent.consent_id, consent.version, consent.canonical_hash),),
            valid_from=now - timedelta(hours=1),
            valid_until=now + timedelta(hours=1),
        )
        purpose = canonical_runtime_decision_purpose(capability)
        context = PolicyContext(
            actor_id=actor_id,
            subject_id=self.profile.active_subject_id,
            resource_owner_id=self.profile.active_subject_id,
            device_id=self.profile.device_id,
            capability=capability,
            purpose=purpose,
            declared_device_mode="self_use",
            current_session_mode=self.profile.service_mode,
            subject_category=self.profile.subject_category,
            age_band=self.profile.age_band,
            speaker_state=self.profile.speaker_state,
            speaker_confidence=self.profile.speaker_confidence,
            device_trust="trusted",
            safety_state=safety_state,
            jurisdiction="CN",
            data_classification=data_classification,
            binding_id=self.profile.binding_id,
            binding_version=self.profile.binding_version,
            session_id=session_id,
            session_epoch=session_epoch,
            runtime_profile_id=runtime_profile_id,
            subject_revision=self.profile.subject_revision,
            evaluated_at=now,
            idempotency_key=(
                f"action:{runtime_profile_id}:{capability}:"
                f"{generation_id}:{turn_id}:{tool_epoch}"
            ),
            binding_evidence=binding,
            consent_evidence=(consent,),
            consent_snapshot_evidence=(snapshot,),
            generation_id=generation_id,
            turn_id=turn_id,
            tool_epoch=tool_epoch,
        )
        engine = PolicyEngine()
        decision = engine.decide(context)
        return engine.receipt_for(context, decision)


def _attach_signed_runtime_profile(
    app: Any,
    *,
    user_id: str,
    session_id: str,
    active_subject_id: str | None = None,
    expired: bool = False,
    unknown_subject: bool = False,
    subject_category: str = "adult",
    age_band: str = "adult",
    service_mode: str = "adult_companion",
    capabilities: tuple[str, ...] | None = None,
) -> RuntimeProfileSignedV2:
    now = datetime.now(UTC)
    resolved_active_subject_id = active_subject_id if active_subject_id is not None else user_id
    subject_payload = (
        {
            "active_subject_id": None,
            "subject_revision": 0,
            "subject_category": "unknown",
            "age_band": "unknown",
            "speaker_state": "unconfirmed",
            "speaker_confidence": None,
            "service_mode": "unknown_safe",
            "capabilities": ["chat"],
            "obligations": [
                {
                    "code": code,
                    "params": {
                        "max_session_seconds": None,
                        "retention_ttl_seconds": None,
                        "quiet_hours": None,
                        "extras": [],
                    },
                }
                for code in (
                    "DO_NOT_PERSIST",
                    "DO_NOT_WRITE_LEARNING_PROGRESS",
                    "NO_MODEL_TRAINING",
                    "REQUIRE_SPEAKER_CONFIRMATION",
                )
            ],
        }
        if unknown_subject
        else {
            "active_subject_id": resolved_active_subject_id,
            "subject_revision": 1,
            "subject_category": subject_category,
            "age_band": age_band,
            "speaker_state": "confirmed",
            "speaker_confidence": 0.99,
            "service_mode": service_mode,
            "capabilities": list(
                capabilities
                if capabilities is not None
                else (
                    "chat",
                    "tutor",
                    "english_practice",
                    "memory_recall_private",
                )
            ),
            "obligations": [],
        }
    )
    issued_at = now - timedelta(minutes=10) if expired else now
    expires_at = now - timedelta(minutes=5) if expired else now + timedelta(minutes=5)
    payload: dict[str, object] = {
        "signature_schema": "runtime-profile-v2",
        "runtime_profile_id": f"rp-{session_id}",
        "device_id": "device-test-1",
        "session_id": session_id,
        "actor_id": user_id,
        "binding_id": "binding-test-1",
        "binding_version": 1,
        "persona_assignment_id": "starlight:v1",
        "persona": {
            "persona_id": "starlight",
            "version": 1,
            "relationship_stage": "new",
        },
        "policy_bundle_version": "runtime-profile-test-v2",
        **subject_payload,
        "policy_receipt_ids": [],
        "session_epoch": 1,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    unsigned = RuntimeProfileV2.model_validate(payload)
    wire_payload = unsigned.model_dump(mode="json")
    profile = RuntimeProfileSignedV2.model_validate(
        {
            **wire_payload,
            "signature": sign_runtime_profile_payload(
                wire_payload,
                signing_key=_RUNTIME_PROFILE_SIGNING_KEY,
            ),
        }
    )
    app.state.session_runtime_service = _SignedRuntimeProfileAuthority(profile)
    return profile


async def _identity(client: AsyncClient) -> tuple[str, dict[str, str]]:
    response = await client.post("/v1/auth/anonymous")
    assert response.status_code == 200
    body = response.json()
    return str(body["user_id"]), {"Authorization": f"Bearer {body['access_token']}"}


def _mark_verified_adult(app: Any, user_id: str) -> None:
    app.state.memory_store.update_subject_profile(
        user_id=user_id,
        subject_category="adult",
        birth_year_band="adult",
        age_evidence_status="verified",
        now=datetime.now(UTC).isoformat(),
    )


def _response_plan_body(session_id: str, *, classification: str = "owner") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "query": "private-request-sentinel-4d7a",
        "fence": {
            "session_id": session_id,
            "turn_id": 7,
            "generation_id": 3,
            "tool_epoch": 2,
            "session_epoch": 0,
        },
        "speaker_decision": {
            "classification": classification,
            "reason_code": "trusted",
            "model_version": "campplus-test-v1",
            "profile_id": "speaker-profile-1",
            "template_version": 1,
        },
    }


class _Registry:
    def __init__(self, version: DigitalSelfVersion) -> None:
        self._version = version

    async def get(self, *, account_id: str, version_id: str) -> DigitalSelfVersion:
        if account_id != self._version.account_id or version_id != self._version.version_id:
            raise VersionNotFoundError(version_id)
        return self._version


class _MemoryCatalog:
    def __init__(self) -> None:
        self.queries: list[object] = []
        self.people_calls: list[dict[str, object]] = []
        self.people_items: tuple[PersonItem, ...] = ()
        self.items_by_subject: dict[str, tuple[MemorySearchItem, ...]] | None = None

    async def people(
        self, *, account_id: str, limit: int = 100, subject_id: str | None = None
    ) -> tuple[PersonItem, ...]:
        # A missing subject_id is the whole-account read.  Callers that mean
        # one person have to pass it before they get here.
        assert subject_id is not None and subject_id.strip()
        del limit
        self.people_calls.append({"account_id": account_id, "subject_id": subject_id})
        return self.people_items

    async def context(self, query: object) -> MemorySearchResult:
        self.queries.append(query)
        subject_id = getattr(query, "subject_id", None)
        if self.items_by_subject is not None:
            items = self.items_by_subject.get(str(subject_id), ())
        else:
            items = (
                MemorySearchItem(
                    item_id="memory-杭州",
                    kind="claim",
                    title="求学经历",
                    snippet="我在杭州读过书。",
                    category="life_story",
                    status="confirmed",
                    source_event_id="event-memory-杭州",
                    occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
                    score=0.99,
                ),
            )
        return MemorySearchResult(items=items)


class _PersonaEngine:
    async def capsule(self, _: object) -> PersonaCapsule:
        return PersonaCapsule(
            version_id="persona-version-1",
            version_number=1,
            entries=(
                PersonaCapsuleEntry(
                    trait_id="persona-direct",
                    category="discourse_style",
                    description="表达直接、语气平静",
                    context="自然对话",
                    counterexample="安慰别人时会更委婉",
                    confidence=0.9,
                    source_event_ids=("event-persona-direct",),
                ),
            ),
        )
