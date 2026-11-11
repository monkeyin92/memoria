"""Account and internal policy seams for the S2 interaction control plane."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from packages.contracts.generated.python.multi_subject_contracts import (
    CapabilityValue,
    DataClassification,
    PolicyReceiptV2,
    RuntimeProfileSignedV2,
    SafetyState,
)
from pydantic import BaseModel, ConfigDict, Field

from services.archive.memory_domain import MemoryCatalogPort, MemorySearchQuery, MemorySearchResult
from services.archive.recall_planner import RecallPlanner
from services.common.companions import CompanionDefinition, companion_definition
from services.common.crisis_policy import log_fixed_safety_reply
from services.common.failure_log import failure_log
from services.common.realtime_information import current_local_time
from services.control_api.app.account_gate import AccountDeletingError
from services.control_api.app.bound_subject import (
    DEVICE_BOUND_SUBJECT_UNVERIFIED,
    claims_device_bound_subject,
    runtime_profile_trusts_bound_subject,
)
from services.control_api.app.child_archive_companion_recall import (
    merge_companion_memory_claims,
    start_child_archive_recall_task,
)
from services.control_api.app.companion_context import gather_companion_context
from services.control_api.app.config import ControlSettings
from services.control_api.app.crisis_routing import route_crisis_with_bounded_evidence
from services.control_api.app.database import MemoryStore
from services.control_api.app.device_control import RuntimeProfileLedger
from services.control_api.app.frozen_turn_verification import (
    append_legacy_plan_audit,
    resolve_frozen_turn,
)
from services.control_api.app.mode_policy import FrozenMode, InteractionMode, ModePolicy
from services.control_api.app.persistent_runtime_profile import (
    current_persistent_runtime_profile,
)
from services.control_api.app.response_plan_cache import (
    ResponsePlanCacheKey as _ResponsePlanCacheKey,
)
from services.control_api.app.response_plan_cache import response_plan_cache as _response_plan_cache
from services.control_api.app.response_plan_contract import (
    EVOLUTION_PROTOCOL_V1,
    ContextPrefetchRequest,
    ResponsePlanRequest,
    ResponsePlanSpeakerDecision,
)
from services.control_api.app.response_plan_payload import _response_plan_payload
from services.control_api.app.security import (
    AuthenticatedUser,
    require_active_voice_session,
    require_authenticated_user,
)
from services.control_api.app.subject_persona import subject_persona_capsule
from services.control_api.app.subject_session_recall import (
    authoritative_recall_context,
    session_epoch_from_runtime_profile,
)
from services.control_api.app.weather_turn import fixed_reply_for_query, remember_user_query
from services.digital_self.domain import RegistryPort
from services.digital_self.preview import SelfPreviewRegistryPort
from services.digital_self.response_planner import (
    DigitalSelfResponsePlanner,
    GroundedItem,
    PlannerActor,
    PlannerSpeakerDecision,
    ResponsePlan,
    SourceRef,
)
from services.evolution.resolver import EvolutionResolver, ResolvedEvolutionArtifact
from services.evolution.store import EvolutionStore
from services.guardian.crisis import CrisisNotificationService
from services.guardian.domain import GuardianStorePort
from services.guardian.retention import (
    apply_memory_retention_ceiling,
    memory_retention_allowed,
)
from services.identity.domain import (
    CustomPersonaRecord,
    IdentityNotFoundError,
    is_custom_persona_id,
)
from services.identity.service import IdentityService
from services.persona.custom_persona_fields import (
    custom_persona_definition,
    custom_persona_envelope,
)
from services.persona.domain import PersonaCapsule
from services.session_runtime.commands import CommitToolEffectCommand
from services.session_runtime.postgres_store import SessionRuntimeConflict
from services.session_runtime.profile_service import (
    PROFILE_ISSUE_DEFERRED_CAPABILITIES,
)
from services.session_runtime.service import (
    PersistentSessionDenied,
    PersistentSessionNotFound,
    PersistentSessionUnavailable,
    PostgresSessionRuntimeService,
)
from services.tutor.domain import TutorFocus
from services.tutor.turn_policy import TutorTurnPolicy

router = APIRouter(prefix="/v1/interaction", tags=["interaction"])
logger = logging.getLogger(__name__)
_EVOLUTION_PROTOCOL_HEADER = "X-Memoria-Evolution-Protocol"

def _local_now(settings: ControlSettings) -> datetime:
    return current_local_time(settings.memoria_timezone)

def _with_fixed_reply(plan: ResponsePlan, reply: str | None) -> ResponsePlan:
    if reply is None:
        return plan
    return replace(plan, instructions=replace(plan.instructions, direct_text=reply))

async def _cached_plan_is_still_authorized(
    request: Request,
    *,
    session_id: str,
    actor_id: str,
) -> bool:
    """Re-fence a cached plan before it is reused.

    A cached payload can contain private persona/memory context, and the cache is
    keyed only by the generation fence.  A retried identical request would
    otherwise be served a plan computed before a legal manager change withdrew
    the session's authorization -- bypassing exactly the check the read path
    exists to apply.  Widening the key would NOT fix it: the take-over leaves the
    signed profile and the session epoch untouched, so the key is identical and
    only a live read of the device binding can tell the two apart.

    No configured authority means the login account is the subject by
    construction, which is the same convention the rest of this module follows.
    """

    runtime_service = getattr(request.app.state, "session_runtime_service", None)
    if runtime_service is None:
        return True
    try:
        await runtime_service.current(
            actor_id=actor_id,
            session_id=session_id,
            now=datetime.now(UTC),
        )
    except Exception:
        logger.warning(
            "cached response plan is no longer authorized session_id=%s",
            session_id,
            exc_info=True,
        )
        return False
    return True

def _tutor_turn_policy(request: Request) -> TutorTurnPolicy:
    policy = getattr(request.app.state, "tutor_turn_policy", None)
    if not isinstance(policy, TutorTurnPolicy):
        policy = TutorTurnPolicy()
        request.app.state.tutor_turn_policy = policy
    return policy


def _crisis_notifications(request: Request) -> CrisisNotificationService:
    return cast(
        CrisisNotificationService,
        request.app.state.crisis_notification_service,
    )


async def _declared_guardian_ids(request: Request, *, subject_person_id: str) -> tuple[str, ...]:
    """Who took responsibility for a bound person without an account (P0-04 D7).

    A child's declared or attested guardian, or an elder's attested delegate:
    the only basis they have, so consent-gated capabilities stay closed.  A
    read failure is logged and degrades to "no contact" instead of inventing one.
    """

    identity_service = getattr(request.app.state, "identity_service", None)
    if not isinstance(identity_service, IdentityService):
        return ()
    try:
        return await identity_service.crisis_contacts(subject_person_id=subject_person_id)
    except Exception:
        logger.exception(
            "declared guardian lookup failed subject_person_id=%s",
            subject_person_id,
        )
        return ()


def _response_plan_key(
    body: ResponsePlanRequest,
    *,
    evolution_protocol: str,
    subject_id: str | None,
) -> _ResponsePlanCacheKey:
    # The fence alone is the turn, not the person.  Switching the active
    # subject and replaying the same fence must not reuse the previous
    # subject's grounded items.
    return (
        body.fence.session_id,
        body.fence.turn_id,
        body.fence.generation_id,
        body.fence.tool_epoch,
        evolution_protocol,
        subject_id or "",
    )


def _response_plan_fingerprint(body: ResponsePlanRequest) -> str:
    encoded = json.dumps(
        body.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _identity(request: Request) -> IdentityService:
    return cast(IdentityService, request.app.state.identity_service)


async def _owner_custom_persona_record(
    request: Request,
    *,
    persona_id: object,
    owner_person_id: str,
) -> CustomPersonaRecord | None:
    """Resolve one owner-scoped custom persona, or ``None`` (never cross-account)."""
    if not is_custom_persona_id(persona_id):
        return None
    try:
        return await _identity(request).get_custom_persona(
            str(persona_id),
            owner_person_id=owner_person_id,
            actor_person_id=owner_person_id,
        )
    except IdentityNotFoundError:
        return None


async def _custom_persona_definition(
    request: Request,
    *,
    persona_id: object,
    owner_person_id: str,
) -> CompanionDefinition | None:
    """Adapt a custom persona for the SAME render path as a built-in companion."""
    record = await _owner_custom_persona_record(
        request, persona_id=persona_id, owner_person_id=owner_person_id
    )
    return custom_persona_definition(record) if record is not None else None


async def _subject_memory_retention_allowed(
    request: Request,
    *,
    subject_person_id: str,
    subject_category: object,
) -> bool:
    """One subject supplies both the category and the consent lookup.

    A minor's retention consent is recorded against the minor's own person id
    on an activated guardian link.  Reading the category from the current
    subject while looking the consent up under the login account would both
    miss real consent and answer about a different person, and it would let
    the two decisions disagree inside one turn.
    """

    consent_active = False
    if subject_category == "minor":
        consent_active = (
            await cast(GuardianStorePort, request.app.state.guardian_store).active_consent(
                minor_user_id=subject_person_id,
                consent_kind="memory_retention",
            )
            is not None
        )
    return memory_retention_allowed(
        subject_category=subject_category,
        active_consent=consent_active,
    )


def _confirmed_subject_id(value: object) -> str | None:
    """A subject the authority actually named, never a blank or the sentinel.

    ``unknown`` is the unsigned/unconfirmed sentinel the runtime profile uses
    when nobody has been identified.  Treating it as a person would query the
    catalog for a subject that does not exist and could not be fenced.
    """

    if not isinstance(value, str):
        return None
    subject_id = value.strip()
    if not subject_id or subject_id == "unknown":
        return None
    return subject_id


@dataclass(frozen=True, slots=True)
class _SubjectMemoryScope:
    """Which memory one turn may read, resolved in exactly one place.

    Two Agent-facing seams have to answer this question (`/response-plan` and
    `/context-prefetch`).  When they answered it independently, the seam that
    skipped the authority read became an account-keyed read for a subject that
    was not the login account -- and kept reading after a legal manager change
    withdrew the session's authorization.  One resolver is the point.
    """

    subject_authority: Literal["runtime_profile", "account_profile", "unavailable"]
    subject_id: str | None
    subject_category: str | None
    age_band: str | None
    retention_allowed: bool
    catalog_readable: bool
    # The current signed profile confirms this device's bound subject and
    # grants private recall: the only proof a device-bound owner claim needs.
    bound_subject_trusted: bool = False
    session_epoch: int | None = None

    @property
    def memory_readable(self) -> bool:
        """Whether a subject-scoped catalog read may answer for this turn."""

        return self.retention_allowed and self.catalog_readable


async def _resolve_subject_memory_scope(
    request: Request,
    *,
    session_id: str,
    account_id: str,
) -> _SubjectMemoryScope:
    """Read the current subject once and decide the memory it may read.

    The current subject is read once and decides the category, the consent
    lookup, the memory scope and the notification target.  A configured-but-
    unavailable authority stays observable and conservative: it never silently
    degrades to the account profile, never guesses a minor, and never notifies a
    guardian.  The signed Runtime Profile is the subject authority, and the
    authority's own read path fences it against the device's current binding, so
    a withdrawn session resolves to `unavailable` here.

    Equality with the login account is not a default.  It holds only when this
    deployment has no Session Runtime, or when the runtime profile itself names
    that account as the active subject.  A missing, blank or ``unknown``
    subject closes the catalog instead of reading the account owner's rows.
    """

    subject_authority: Literal["runtime_profile", "account_profile", "unavailable"]
    subject_id: str | None = None
    subject_category: str | None = None
    age_band: str | None = None
    session_epoch: int | None = None
    catalog_readable = False
    bound_subject_trusted = False
    runtime_service = getattr(request.app.state, "session_runtime_service", None)
    if runtime_service is None:
        # This deployment profile has no persistent Session Runtime; the login
        # account is the subject by construction.
        subject_authority = "account_profile"
        subject_id = account_id
        session_epoch = 1
        catalog_readable = True
    else:
        subject_authority = "unavailable"
        try:
            session_dict = require_active_voice_session(request, session_id)
            runtime_profile = await current_persistent_runtime_profile(
                request, session_dict, session_id=session_id
            )
        except Exception:
            logger.exception(
                "current subject authority unavailable session_id=%s; "
                "keeping the conservative capability gate",
                session_id,
            )
        else:
            subject_authority = "runtime_profile"
            subject_id = _confirmed_subject_id(runtime_profile.get("active_subject_id"))
            # The profile was read, but it did not name a person.  Do not keep
            # the resolver's historical account-id default and query as the
            # owner.
            catalog_readable = subject_id is not None
            profile_category = runtime_profile.get("subject_category")
            if isinstance(profile_category, str):
                subject_category = profile_category
            profile_age_band = runtime_profile.get("age_band")
            if isinstance(profile_age_band, str):
                age_band = profile_age_band
            bound_subject_trusted = runtime_profile_trusts_bound_subject(runtime_profile)
            session_epoch = session_epoch_from_runtime_profile(runtime_profile)
    if subject_authority == "account_profile" and subject_id is not None and subject_category is None:
        profile = _store(request).get_subject_profile(user_id=subject_id)
        if profile is not None:
            subject_category = profile.get("subject_category")
            age_band = profile.get("birth_year_band")
    retention_subject_id = subject_id if subject_id is not None else ""
    return _SubjectMemoryScope(
        subject_authority=subject_authority,
        subject_id=subject_id,
        subject_category=subject_category,
        age_band=age_band,
        session_epoch=session_epoch,
        retention_allowed=(
            await _subject_memory_retention_allowed(
                request,
                subject_person_id=retention_subject_id,
                subject_category=subject_category,
            )
            if catalog_readable and subject_id is not None
            else False
        ),
        catalog_readable=catalog_readable,
        bound_subject_trusted=bound_subject_trusted,
    )


def _trusted_speaker_decision(
    decision: ResponsePlanSpeakerDecision,
    scope: _SubjectMemoryScope | None,
) -> ResponsePlanSpeakerDecision:
    """Demote device-bound owner claims: bind proves config, not the speaker (A06)."""

    del scope
    if not claims_device_bound_subject(decision.classification, decision.reason_code):
        return decision
    return decision.model_copy(
        update={"classification": "uncertain", "reason_code": DEVICE_BOUND_SUBJECT_UNVERIFIED}
    )


def _registry(request: Request) -> RegistryPort:
    return cast(RegistryPort, request.app.state.digital_self_registry)


def _catalog(request: Request) -> MemoryCatalogPort:
    return cast(MemoryCatalogPort, request.app.state.memory_catalog)


def _evolution_resolver(request: Request) -> EvolutionResolver:
    return cast(EvolutionResolver, request.app.state.evolution_resolver)


def _require_policy_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    expected = cast(ControlSettings, request.app.state.settings).internal_token(
        "interaction_policy"
    )
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=401, detail="valid internal interaction policy token required"
        )


def _require_response_plan_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    expected = cast(ControlSettings, request.app.state.settings).internal_token("response_plan")
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="valid internal response plan token required")


@router.get("/capabilities")
async def capabilities(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    profile = _store(request).get_profile(user_id=user.user_id, now="1970-01-01T00:00:00Z")
    companion = companion_definition(profile.get("companion_id"))
    modes = {
        mode: ModePolicy.availability(cast(InteractionMode, mode)).payload()
        for mode in ("companion", "self_preview", "legacy", "archive")
    }
    versions = await _registry(request).list(account_id=user.user_id)
    preview_versions = tuple(
        version for version in versions if version.status in {"approved", "frozen"}
    )
    missing: list[str] = []
    if not preview_versions:
        missing.append("approved_digital_self_version")
    else:
        preview_registry = cast(SelfPreviewRegistryPort, request.app.state.self_preview_registry)
        preview_ready = False
        for version in preview_versions:
            if await preview_registry.version_stale(
                account_id=user.user_id,
                version_id=version.version_id,
                manifest_sha256=version.manifest_sha256,
            ):
                continue
            if (
                await preview_registry.completed_verdict(
                    account_id=user.user_id,
                    version_id=version.version_id,
                    manifest_sha256=version.manifest_sha256,
                )
                == "approve"
            ):
                preview_ready = True
                break
        if not preview_ready:
            missing.append("preview_version_stale_or_fidelity_unready")
    modes["self_preview"] = {
        "status": "blocked" if missing else "available",
        "conversational": True,
        **({"missing": missing} if missing else {}),
    }
    return {
        "selected_companion_id": companion.companion_id if companion else None,
        "modes": modes,
    }


class SessionPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    # Deprecated compatibility input. The session policy must not turn a
    # startup-time speaker guess into a permanent authorization decision.
    speaker_class: Literal["owner", "guest", "uncertain"] | None = None



@router.post("/session-policy")
async def session_policy(
    body: SessionPolicyRequest,
    request: Request,
    _: Annotated[None, Depends(_require_policy_token)],
) -> dict[str, Any]:
    session = require_active_voice_session(request, body.session_id)
    runtime_profile = await current_persistent_runtime_profile(
        request,
        session,
        session_id=body.session_id,
    )
    device_id = runtime_profile.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_profile_unavailable"},
        )
    ledger = RuntimeProfileLedger(_store(request)).current(device_id)
    runtime_profile_version = ledger.profile_version if ledger is not None else 0
    frozen = FrozenMode.from_session(session)
    policy = ModePolicy.session_context(frozen)
    if frozen.interaction_mode == "companion":
        account_id = str(session["user_id"])
        # Both the category and the consent must describe the subject that is
        # actually using the device.  The signed Runtime Profile is that
        # authority; the login account's own profile is used only when the
        # profile carries no subject at all (unknown-safe).
        profile_subject_id = runtime_profile.get("active_subject_id")
        subject_person_id = (
            profile_subject_id
            if isinstance(profile_subject_id, str) and profile_subject_id.strip()
            else account_id
        )
        profile_subject_category = runtime_profile.get("subject_category")
        subject_category = (
            profile_subject_category
            if isinstance(profile_subject_category, str)
            else None
        )
        retention_allowed = await _subject_memory_retention_allowed(
            request,
            subject_person_id=subject_person_id,
            subject_category=subject_category,
        )
        policy = apply_memory_retention_ceiling(policy, allowed=retention_allowed)
        profile = _store(request).get_profile(
            user_id=account_id,
            now=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        display_name = profile.get("display_name")
        if (
            isinstance(display_name, str)
            and display_name.strip()
            and subject_person_id == account_id
        ):
            # This stays on the internal Agent policy path. The Agent adds it
            # only after the current speaker is confirmed as the account owner,
            # and it is the account owner's own name: a different subject using
            # the device must not receive it.
            policy["owner_display_name"] = display_name.strip()
        persona_snapshot = runtime_profile.get("persona")
        persona_id = (
            persona_snapshot.get("persona_id")
            if isinstance(persona_snapshot, dict)
            else None
        )
        # The custom persona's structured body is an internal control->agent
        # bypass keyed to the SIGNED persona id: it never enters the signed
        # RuntimeProfile (design increment-02 §1.2).  A guest/unconfirmed
        # subject resolves to the built-in binding default, so it never lands
        # here; a mismatched envelope fails closed on the agent side.
        custom_persona_record = await _owner_custom_persona_record(
            request, persona_id=persona_id, owner_person_id=account_id
        )
        if custom_persona_record is not None:
            policy["custom_persona"] = custom_persona_envelope(custom_persona_record)
    profile_capabilities = {
        str(value)
        for value in runtime_profile.get("capabilities", ())
        if isinstance(value, str)
    }
    profile_obligations = {
        str(value.get("code"))
        for value in runtime_profile.get("obligations", ())
        if isinstance(value, dict) and isinstance(value.get("code"), str)
    }
    service_mode = runtime_profile.get("service_mode")
    if not isinstance(service_mode, str):
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_profile_unavailable"},
        )
    interaction_mode_by_service_mode = {
        "student_minor": "companion",
        "adult_companion": "companion",
        "senior_companion": "companion",
        "family_shared": "companion",
        "adult_archive": "archive",
        "self_preview": "self_preview",
        "legacy_access": "legacy",
        "unknown_safe": "unknown_safe",
    }
    interaction_mode = interaction_mode_by_service_mode.get(service_mode)
    if interaction_mode is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_profile_unavailable"},
        )
    history_eligible = (
        interaction_mode != "unknown_safe" and policy.get("memory_retention") != "ephemeral_only"
        and "memory_recall_private" in profile_capabilities
    )
    policy.update(
        {
            "interaction_mode": interaction_mode,
            "simulated_output": interaction_mode != "companion",
            "history_eligible": history_eligible,
            "owner_projection_eligible": history_eligible,
            "capabilities": {
                "conversation": "chat" in profile_capabilities,
                "private_memory": history_eligible,
                "persona": False,
                "persona_low_sensitivity": False,
                "tools": False,
                "history": history_eligible,
                "learning": (
                    interaction_mode != "unknown_safe"
                    and (
                        "tutor" in profile_capabilities
                        or "english_practice" in profile_capabilities
                    )
                    and "DO_NOT_WRITE_LEARNING_PROGRESS" not in profile_obligations
                ),
                "voice_profile": False,
            },
            "runtime_profile": runtime_profile,
            "runtime_profile_version": runtime_profile_version,
        }
    )
    if interaction_mode == "unknown_safe":
        # A signed unknown-safe profile is a valid conversation-only surface,
        # not an authority outage.  Keep it free of every owner/style hint so
        # the Agent cannot infer identity, private context, or persona from the
        # legacy companion session envelope.
        policy.pop("owner_display_name", None)
        policy["companion_style_id"] = None
        policy["companion_style_version"] = None
    return policy


class ActionPolicyRequest(BaseModel):
    """One deferred action at an exact generation fence.

    The Agent action executor supplies the action capability and the exact
    generation/turn/tool epoch it is about to execute.  The actor is never
    accepted from the caller: it is always derived from the Voice Session and
    cross-checked against the signed Runtime Profile.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    runtime_profile_id: str = Field(min_length=1, max_length=128)
    capability: CapabilityValue
    session_epoch: int = Field(ge=1, le=2**31 - 1)
    generation_id: int = Field(ge=0, le=2**31 - 1)
    turn_id: int = Field(ge=0, le=2**31 - 1)
    tool_epoch: int = Field(ge=0, le=2**31 - 1)
    data_classification: DataClassification
    safety_state: SafetyState


class ToolEffectFenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    session_epoch: int = Field(ge=1, le=2**31 - 1)
    generation_id: int = Field(ge=0, le=2**31 - 1)
    turn_id: int = Field(ge=0, le=2**31 - 1)
    tool_epoch: int = Field(ge=0, le=2**31 - 1)


class ToolEffectCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    fence: ToolEffectFenceRequest
    runtime_profile: dict[str, object]
    policy_receipt: dict[str, object]
    capability: str = Field(min_length=1, max_length=128)
    purpose: str = Field(min_length=1, max_length=128)
    resource_id: str = Field(min_length=1, max_length=128)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=512)
    intent: str = Field(min_length=1, max_length=512)
    payload: dict[str, object]
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fence_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class ToolEffectReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=512)


@router.post("/action-policy")
async def action_policy(
    body: ActionPolicyRequest,
    request: Request,
    _: Annotated[None, Depends(_require_policy_token)],
) -> dict[str, Any]:
    """Authorize one deferred action at an exact action fence.

    Fail-closed semantics: 401 without the internal token, 404/410 for a
    missing/tombstoned Voice Session, 503 when the persistent Runtime
    authority is unavailable, 403 for a forged actor/Profile/fence or a
    capability that must stay profile-issued, and 409 for a stale fence.
    The returned body is the complete immutable ``PolicyReceiptV2`` for the
    action, including the exact fence and all evidence identities.
    """
    session = require_active_voice_session(request, body.session_id)
    actor_id = session.get("user_id")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_context_unavailable"},
        )
    runtime_service = cast(
        PostgresSessionRuntimeService | None,
        getattr(request.app.state, "session_runtime_service", None),
    )
    if runtime_service is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        )
    authorize_action = getattr(runtime_service, "authorize_action", None)
    if not callable(authorize_action):
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        )
    # Resource-scoped and other deferred capabilities are authorized only at
    # action time; session-level capabilities must stay profile-issued.
    if body.capability not in PROFILE_ISSUE_DEFERRED_CAPABILITIES:
        raise HTTPException(
            status_code=403,
            detail={"code": "action_capability_not_deferred"},
        )
    try:
        profile, context = await runtime_service.current(
            actor_id=actor_id,
            session_id=body.session_id,
            now=datetime.now(UTC),
        )
    except (PersistentSessionNotFound, PersistentSessionUnavailable) as exc:
        logger.warning(
            "action-policy Runtime Profile unavailable session_id=%s %s",
            body.session_id,
            failure_log(exc),
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc
    except Exception as exc:
        logger.exception(
            "action-policy Runtime Profile read failed session_id=%s",
            body.session_id,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc
    if (
        getattr(profile, "session_id", None) != body.session_id
        or getattr(profile, "actor_id", None) != actor_id
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "action_actor_forged"},
        )
    if getattr(profile, "runtime_profile_id", None) != body.runtime_profile_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "action_profile_forged"},
        )
    if body.session_epoch != context.session_epoch:
        if body.session_epoch < context.session_epoch:
            raise HTTPException(
                status_code=409,
                detail={"code": "action_fence_stale"},
            )
        raise HTTPException(
            status_code=403,
            detail={"code": "action_fence_forged"},
        )
    if (
        body.generation_id < context.generation_id
        or body.turn_id < context.turn_id
        or body.tool_epoch < context.tool_epoch
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "action_fence_stale"},
        )
    try:
        receipt = await authorize_action(
            actor_id=actor_id,
            session_id=body.session_id,
            runtime_profile_id=body.runtime_profile_id,
            capability=body.capability,
            session_epoch=body.session_epoch,
            generation_id=body.generation_id,
            turn_id=body.turn_id,
            tool_epoch=body.tool_epoch,
            data_classification=body.data_classification,
            safety_state=body.safety_state,
            now=datetime.now(UTC),
        )
    except PersistentSessionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "action_denied"},
        ) from exc
    except SessionRuntimeConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "action_fence_conflict"},
        ) from exc
    except (PersistentSessionNotFound, PersistentSessionUnavailable) as exc:
        logger.warning(
            "action-policy authority unavailable session_id=%s %s",
            body.session_id,
            failure_log(exc),
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc
    except Exception as exc:
        logger.exception(
            "action-policy authorization failed session_id=%s",
            body.session_id,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "action_authorization_unavailable"},
        ) from exc
    if not isinstance(receipt, PolicyReceiptV2):
        logger.error(
            "action-policy authority returned a non-receipt session_id=%s",
            body.session_id,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "action_receipt_unavailable"},
        )
    return receipt.model_dump(mode="json")


@router.post("/tool-effect/commit")
async def commit_tool_effect(
    body: ToolEffectCommitRequest,
    request: Request,
    _: Annotated[None, Depends(_require_policy_token)],
) -> dict[str, object]:
    """Commit one Agent-prepared effect through the action transaction port."""
    session = require_active_voice_session(request, body.session_id)
    actor_id = session.get("user_id")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_context_unavailable"},
        )
    if body.fence.session_id != body.session_id:
        raise HTTPException(status_code=403, detail={"code": "tool_effect_fence_forged"})
    runtime_service = cast(
        PostgresSessionRuntimeService | None,
        getattr(request.app.state, "session_runtime_service", None),
    )
    if runtime_service is None or not callable(
        getattr(runtime_service, "commit_tool_effect", None)
    ):
        raise HTTPException(
            status_code=503,
            detail={"code": "tool_effect_authority_unavailable"},
        )
    try:
        profile = RuntimeProfileSignedV2.model_validate(body.runtime_profile)
        receipt = PolicyReceiptV2.model_validate(body.policy_receipt)
        command = CommitToolEffectCommand(
            actor_id=actor_id,
            session_id=body.session_id,
            runtime_profile=profile,
            policy_receipt=receipt,
            session_epoch=body.fence.session_epoch,
            generation_id=body.fence.generation_id,
            turn_id=body.fence.turn_id,
            tool_epoch=body.fence.tool_epoch,
            capability=body.capability,
            purpose=body.purpose,
            resource_id=body.resource_id,
            evidence_refs=body.evidence_refs,
            idempotency_key=body.idempotency_key,
            intent=body.intent,
            payload=body.payload,
            payload_sha256=body.payload_sha256,
            fence_fingerprint=body.fence_fingerprint,
            now=datetime.now(UTC),
        )
        result = await runtime_service.commit_tool_effect(command)
    except PersistentSessionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "tool_effect_denied"},
        ) from exc
    except SessionRuntimeConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "tool_effect_conflict"},
        ) from exc
    except (PersistentSessionNotFound, PersistentSessionUnavailable) as exc:
        logger.warning(
            "tool effect authority unavailable session_id=%s %s",
            body.session_id,
            failure_log(exc),
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "tool_effect_authority_unavailable"},
        ) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "tool_effect_payload_invalid"},
        ) from exc
    except Exception as exc:
        logger.exception("tool effect commit failed session_id=%s", body.session_id)
        raise HTTPException(
            status_code=503,
            detail={"code": "tool_effect_authority_unavailable"},
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=503,
            detail={"code": "tool_effect_receipt_unavailable"},
        )
    return result


@router.post("/tool-effect/reconcile")
async def reconcile_tool_effect(
    body: ToolEffectReconcileRequest,
    request: Request,
    _: Annotated[None, Depends(_require_policy_token)],
) -> dict[str, object]:
    """Reconcile a key; authority failure is deliberately UNKNOWN/503."""
    runtime_service = cast(
        PostgresSessionRuntimeService | None,
        getattr(request.app.state, "session_runtime_service", None),
    )
    if runtime_service is None or not callable(
        getattr(runtime_service, "reconcile_tool_effect", None)
    ):
        raise HTTPException(
            status_code=503,
            detail={"code": "tool_effect_authority_unknown", "state": "unknown"},
        )
    try:
        result = await runtime_service.reconcile_tool_effect(
            idempotency_key=body.idempotency_key,
        )
    except Exception as exc:
        logger.warning("tool effect reconcile authority unavailable %s", failure_log(exc))
        raise HTTPException(
            status_code=503,
            detail={"code": "tool_effect_authority_unknown", "state": "unknown"},
        ) from exc
    if not isinstance(result, dict) or result.get("state") not in {
        "committed",
        "not_found",
        "unknown",
    }:
        raise HTTPException(
            status_code=503,
            detail={"code": "tool_effect_authority_unknown", "state": "unknown"},
        )
    return result


async def _companion_items(
    *,
    request: Request,
    frozen: FrozenMode,
    account_id: str,
    scope: _SubjectMemoryScope,
    query: str,
    speaker: ResponsePlanSpeakerDecision,
    now: datetime,
    recall_context: tuple[str, ...] = (),
) -> tuple[tuple[GroundedItem, ...], PersonaCapsule | None]:
    trusted = ModePolicy.trusted_context(
        frozen,
        speaker_class=speaker.classification,
        reason_code=speaker.reason_code,
    )
    capabilities = trusted["capabilities"]
    # Fail closed before the catalog sees the call.  MemorySearchQuery.subject_id
    # defaults to None, and None means "the whole account", so a missing subject
    # must never be passed through as that default.
    confirmed_subject_id = _confirmed_subject_id(scope.subject_id)
    subject_ready = scope.catalog_readable and confirmed_subject_id is not None
    private_readable = capabilities["private_memory"] and subject_ready
    archive_readable = subject_ready and (
        capabilities["private_memory"] or capabilities["child_archive_confirmed_only"]
    )

    async def fetch_memory() -> MemorySearchResult:
        # The query carries the subject the turn was resolved for.  The legacy
        # archive stores every first-person claim under subject_key="self"
        # inside the login account; the catalog distinguishes speakers by the
        # evidence lineage of that subject_id, not by falling back to the owner.
        assert confirmed_subject_id is not None
        people = await _catalog(request).people(
            account_id=account_id, subject_id=confirmed_subject_id, limit=100
        )
        recall = RecallPlanner.plan(query=query, now=now, people=people)
        memory_query = recall.text
        if recall_context:
            memory_query = ("\n".join((memory_query, *recall_context))).strip()[:4000]
        return await _catalog(request).context(
            MemorySearchQuery(
                account_id=account_id,
                subject_id=confirmed_subject_id,
                speaker_class="owner",
                text=memory_query,
                entity_ids=recall.entity_ids,
                occurred_after=recall.occurred_after,
                occurred_before=recall.occurred_before,
                include_candidates=False,
                limit=8,
            )
        )

    memory_task = asyncio.create_task(fetch_memory()) if private_readable else None
    # P1-03: another subject's persona reads only under its long-term-memory grant.
    persona_readable = scope.bound_subject_trusted or confirmed_subject_id == account_id
    persona_subject = confirmed_subject_id if persona_readable else None
    persona_task = (
        asyncio.create_task(
            subject_persona_capsule(
                request,
                account_id=account_id,
                subject_id=persona_subject,
                speaker_class=speaker.classification,
                topic=query[:1000],
                low_sensitivity_only=capabilities["persona_low_sensitivity"],
            )
        )
        if (capabilities["persona"] or capabilities["persona_low_sensitivity"])
        and persona_subject is not None
        else None
    )
    archive_task = start_child_archive_recall_task(
        request,
        archive_readable=archive_readable,
        subject_id=confirmed_subject_id,
        subject_category=scope.subject_category,
    )
    completed = await gather_companion_context(memory_task, persona_task, archive_task)
    if completed is None:
        logger.warning("response planner companion context exceeded budget")
        completed = []
        memory_task = None
        persona_task = None
        archive_task = None
    cursor = iter(completed)
    memory_result = next(cursor) if memory_task is not None else None
    persona_result = next(cursor) if persona_task is not None else None
    archive_result = next(cursor) if archive_task is not None else None

    items: list[GroundedItem] = merge_companion_memory_claims(
        archive_result=archive_result,
        memory_result=memory_result,
        logger=logger,
    )
    persona_capsule: PersonaCapsule | None = None
    if persona_result is not None and not isinstance(persona_result, BaseException):
        persona_capsule = cast(PersonaCapsule, persona_result)
        for entry in persona_capsule.entries:
            items.append(
                GroundedItem(
                    item_id=entry.trait_id,
                    kind="persona_trait",
                    content=entry.description,
                    source_refs=(
                        (
                            SourceRef(
                                "persona_trait",
                                entry.trait_id,
                                tuple(entry.source_event_ids),
                            ),
                        )
                        if entry.source_event_ids
                        else ()
                    ),
                )
            )
    elif isinstance(persona_result, BaseException):
        logger.warning(
            "response planner persona context unavailable",
            exc_info=persona_result,
        )
    return tuple(items), persona_capsule


@router.post("/response-plan")
async def response_plan(
    body: ResponsePlanRequest,
    request: Request,
    _: Annotated[None, Depends(_require_response_plan_token)],
    evolution_protocol_header: Annotated[
        str | None,
        Header(alias=_EVOLUTION_PROTOCOL_HEADER),
    ] = None,
) -> dict[str, Any]:
    frozen, account_id, version, relationship, legacy_access = await resolve_frozen_turn(
        request, body.session_id
    )
    evolution_protocol = (
        EVOLUTION_PROTOCOL_V1 if evolution_protocol_header == EVOLUTION_PROTOCOL_V1 else ""
    )
    # Safety reads only the current utterance. Run it before the cache, scope,
    # recall and companion context: none may delay it or answer a crisis turn.
    crisis = await route_crisis_with_bounded_evidence(
        request, body.query, recall_context=body.recall_context
    )
    script = crisis.script
    if script is not None:
        log_fixed_safety_reply(logger, crisis)
    cache = _response_plan_cache(request)
    # Resolve the subject before the cache key.  A plan computed for the
    # previous person must not answer after the device switches subject, even
    # when the generation fence is replayed unchanged.
    scope = await _resolve_subject_memory_scope(
        request,
        session_id=body.session_id,
        account_id=account_id,
    )
    body = body.model_copy(
        update={"speaker_decision": _trusted_speaker_decision(body.speaker_decision, scope)}
    )
    key = _response_plan_key(
        body,
        evolution_protocol=evolution_protocol,
        subject_id=scope.subject_id,
    )
    fingerprint = _response_plan_fingerprint(body)
    key_lock = await cache.lock_for(key)
    async with key_lock:
        settings = cast(ControlSettings, request.app.state.settings)
        evolution_store = cast(EvolutionStore, request.app.state.evolution_store)
        account_gate = request.app.state.account_operations
        try:
            # A cached plan can contain private persona/memory context. Keep
            # the cache hit behind the same deletion fence as a fresh plan.
            with account_gate.sync_read(account_id):
                if await asyncio.to_thread(evolution_store.is_account_deleting, account_id):
                    raise HTTPException(
                        status_code=409, detail="account deletion is in progress"
                    )
                cached = await cache.get(key, fingerprint)
                if cached is not None:
                    if script is None and await _cached_plan_is_still_authorized(
                        request,
                        session_id=body.session_id,
                        actor_id=account_id,
                    ):
                        remember_user_query(
                            request, session_id=body.session_id, query=body.query
                        )
                        return cached
                    # A crisis turn must not reuse a non-crisis snapshot, and a
                    # withdrawn session's plan is private data to drop.
                    await cache.invalidate(key)
        except AccountDeletingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        now = _local_now(settings)
        fixed_reply = script.display if script is not None else fixed_reply_for_query(
            query=body.query, frozen=frozen, now=now, settings=settings,
            request=request, session_id=body.session_id,
        )
        # The subject was resolved once, before the cache key, and decides the
        # category, the consent lookup, the memory scope and the notification
        # target -- through the one resolver every Agent-facing seam shares.
        active_subject_id = scope.subject_id
        retention_allowed = scope.retention_allowed
        is_minor_student = (
            scope.subject_category == "minor"
            or scope.age_band in {"under_14", "14_17"}
        )
        notify = crisis.notify_guardian and None not in (script, active_subject_id)
        # A minor always notifies; an elder notifies the attested delegate (P0-04 D7).
        contacts = (
            await _declared_guardian_ids(request, subject_person_id=active_subject_id)
            if notify and active_subject_id is not None
            else ()
        )
        if notify and script is not None and active_subject_id is not None and (
            is_minor_student or contacts
        ):
            try:
                await _crisis_notifications(request).record_minor_crisis(
                    minor_user_id=active_subject_id,
                    session_id=body.session_id,
                    turn_id=body.fence.turn_id,
                    generation_id=body.fence.generation_id,
                    tool_epoch=body.fence.tool_epoch,
                    script_version=script.template_id,
                    reply_template_id=script.template_id,
                    occurred_at=now,
                    declared_guardian_ids=contacts,
                    attempt_push=crisis.attempt_push, possibly_story_retell=crisis.possibly_story_retell,
                )
            except Exception:
                logger.exception(
                    "minor crisis notification enqueue failed; preserving fixed safety reply "
                    "session_id=%s turn_id=%s",
                    body.session_id,
                    body.fence.turn_id,
                )
                # Notification failure must never suppress the fixed safety reply.
        tutor_directive: str | None = None
        if fixed_reply is None and frozen.session_focus in {
            "tutor_english",
            "tutor_homework",
        }:
            tutor_directive = (
                _tutor_turn_policy(request)
                .observe(
                    session_id=body.session_id,
                    turn_id=body.fence.turn_id,
                    focus=cast(TutorFocus, frozen.session_focus),
                    intent=body.utterance_intent,
                )
                .instruction
            )
        merged_recall = (
            await authoritative_recall_context(
                request,
                subject_id=scope.subject_id,
                session_id=body.session_id,
                session_epoch=scope.session_epoch,
                retention_allowed=retention_allowed,
                client_items=tuple(body.recall_context),
            )
            if fixed_reply is None
            else ()
        )
        companion_items, persona_capsule = (
            await _companion_items(
                request=request,
                frozen=frozen,
                account_id=account_id,
                scope=scope,
                query=body.query,
                speaker=body.speaker_decision,
                now=now,
                recall_context=merged_recall,
            )
            if (
                frozen.interaction_mode == "companion" and fixed_reply is None and retention_allowed
            )
            else ((), None)
        )
        evolution_artifacts: tuple[ResolvedEvolutionArtifact, ...] = ()
        if (
            evolution_protocol == EVOLUTION_PROTOCOL_V1
            and frozen.interaction_mode == "companion"
            and fixed_reply is None
            and settings.evolution_internal_token()
        ):
            try:
                evolution_artifacts = await asyncio.to_thread(
                    _evolution_resolver(request).resolve,
                    account_id=account_id,
                    session_id=body.session_id,
                    speaker_class=body.speaker_decision.classification,
                    query=body.query,
                )
            except Exception:
                # Evolution is an optional, lower-priority layer. Store or
                # resolver failures must not make the reviewed base planner
                # unavailable, and query text must never enter the log.
                logger.exception(
                    "evolution resolver unavailable session_id=%s turn_id=%s generation_id=%s",
                    body.session_id,
                    body.fence.turn_id,
                    body.fence.generation_id,
                )
            deleting = await asyncio.to_thread(evolution_store.is_account_deleting, account_id)
            if evolution_artifacts and deleting:
                # A tombstone can be committed just after the resolver's read
                # lease. Do not carry a private rule into the response plan.
                evolution_artifacts = ()
        plan = DigitalSelfResponsePlanner.plan(
            mode=frozen.interaction_mode,
            actor=PlannerActor(
                account_id=account_id,
                resource_owner_account_id=(
                    legacy_access.resource_owner_account_id if legacy_access is not None else None
                ),
                legacy_actor_role=(legacy_access.actor_role if legacy_access is not None else None),
                legacy_allowed_items=(
                    frozenset((item.kind, item.item_id) for item in legacy_access.allowed_items)
                    if legacy_access is not None
                    else frozenset()
                ),
            ),
            version=version,
            query=body.query,
            relationship_id=relationship.relationship_id if relationship else None,
            speaker_decision=PlannerSpeakerDecision(
                classification=body.speaker_decision.classification,
                reason_code=body.speaker_decision.reason_code,
                model_version=body.speaker_decision.model_version,
                profile_id=body.speaker_decision.profile_id,
                template_version=body.speaker_decision.template_version,
                authorized_scopes=frozenset(),
            ),
            companion_items=companion_items,
        )
        plan = _with_fixed_reply(plan, fixed_reply)
        # A custom persona (cu_*) contributes the SAME CompanionDefinition the
        # built-in catalogue would; the old bio free-text form is gone.
        custom_persona = (
            await _custom_persona_definition(
                request,
                persona_id=frozen.companion_style_id,
                owner_person_id=account_id,
            )
            if script is None
            else None
        )
        try:
            with account_gate.sync_read(account_id):
                deleting = await asyncio.to_thread(evolution_store.is_account_deleting, account_id)
                if evolution_artifacts and deleting:
                    evolution_artifacts = ()
                payload = _response_plan_payload(
                    body=body,
                    frozen=frozen,
                    version=version,
                    relationship=relationship,
                    plan=plan,
                    persona_capsule=persona_capsule,
                    now=now,
                    evolution_artifacts=evolution_artifacts,
                    evolution_account_id=account_id,
                    evolution_receipt_secret=settings.evolution_internal_token(),
                    evolution_protocol=evolution_protocol,
                    tutor_directive=tutor_directive,
                    custom_persona=custom_persona,
                )
                cached_payload = await cache.put(key, fingerprint, payload)
        except AccountDeletingError as exc:
            # The plan may already contain private memory/persona context. Do
            # not downgrade to a public-looking response after deletion has
            # begun; force the caller to retry after the account operation.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if legacy_access is not None:
            await append_legacy_plan_audit(
                request,
                body=body,
                access=legacy_access,
                plan=plan,
            )
        return cached_payload


@router.post("/context-prefetch")
async def context_prefetch(
    body: ContextPrefetchRequest,
    request: Request,
    _: Annotated[None, Depends(_require_response_plan_token)],
) -> dict[str, Any]:
    """Prepare bounded context data without creating an authoritative turn."""

    frozen, account_id, _version, _relationship, _legacy_access = await resolve_frozen_turn(
        request, body.session_id
    )
    scope = (
        await _resolve_subject_memory_scope(
            request,
            session_id=body.session_id,
            account_id=account_id,
        )
        if frozen.interaction_mode == "companion"
        else None
    )
    body = body.model_copy(
        update={"speaker_decision": _trusted_speaker_decision(body.speaker_decision, scope)}
    )
    if scope is None or not scope.memory_readable or scope.subject_id is None:
        # The same contract as /response-plan: a non-companion turn, a subject the
        # authority cannot read, a blank or unknown subject, or a minor without
        # retention consent gets no catalog read and no persona capsule.  A named
        # subject that is not the login account still reads, but only through
        # that subject's evidence lineage.
        return {
            "speaker_class": body.speaker_decision.classification,
            "grounded_items": [],
            "persona_version_id": None,
            "persona_version_number": None,
        }
    items, persona = await _companion_items(
        request=request,
        frozen=frozen,
        account_id=account_id,
        scope=scope,
        query=body.query,
        speaker=body.speaker_decision,
        now=_local_now(cast(ControlSettings, request.app.state.settings)),
    )
    return {
        "speaker_class": body.speaker_decision.classification,
        "grounded_items": [
            {
                "kind": item.kind,
                "item_id": item.item_id,
                "content": item.content,
                "use_as": "style" if item.kind == "persona_trait" else "fact",
                "source_event_ids": [
                    str(source_event_id)[:128]
                    for ref in item.source_refs[:4]
                    for source_event_id in ref.source_event_ids[:8]
                ][:16],
                "confidence": None,
                "sharing_scope": None,
            }
            for item in items[:32]
        ],
        "persona_version_id": persona.version_id if persona is not None else None,
        "persona_version_number": persona.version_number if persona is not None else None,
    }
