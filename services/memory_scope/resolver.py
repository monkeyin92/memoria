"""Fail-closed memory scope resolution (section 6.2, PR-12).

``MemoryScopeResolver`` is a pure decision function: given the current
subject, speaker state, policy decision, consent snapshot and co-subject
context it returns exactly one ``ScopeResolution``.  It never talks to
storage and never guesses; anything it cannot prove is ``unknown``.
"""

from __future__ import annotations

from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyEffect,
    SpeakerState,
    SubjectCategory,
)

from services.memory_scope.domain import (
    DURABLE_SCOPES,
    OBLIGATION_DO_NOT_PERSIST,
    OBLIGATION_PERSIST_AGGREGATE_ONLY,
    OBLIGATION_REQUIRE_SPEAKER_CONFIRMATION,
    OBLIGATION_REQUIRE_SUBJECT_APPROVAL,
    OBLIGATION_RETENTION_TTL,
    OBLIGATION_WRITE_POLICY_RECEIPT,
    CoSubjectContext,
    DeniedResolution,
    MemoryScope,
    ResolutionContext,
    RetentionPolicy,
    ScopeResolution,
)


class MemoryScopeResolver:
    """Pure scope decision table for memory writes.

    Deterministic rules only; the policy engine owns capability decisions
    (this resolver never grants anything the policy decision denied).
    """

    def resolve(self, context: ResolutionContext) -> ScopeResolution:
        """Resolve *context* to a scope, fail-closed on every unknown."""

        policy = context.policy
        if policy.effect == PolicyEffect.POLICY_EFFECT_DENY:
            return DeniedResolution.POLICY_DENIED
        if not policy.receipt_id:
            return DeniedResolution.MISSING_SOURCE

        subject = context.subject
        requested = context.requested_scope

        # Ephemeral session context: policy-gated, never durable, never
        # written into any member's long-term memory.
        if requested is MemoryScope.MEMORY_SCOPE_SESSION_EPHEMERAL:
            return self._resolve_ephemeral(context)

        if requested not in DURABLE_SCOPES:
            return DeniedResolution.MISSING_SOURCE

        # PR-12 / section 11.5: every durable write must arrive inside a
        # session/binding/profile/epoch fence.  No fence, no write.
        if context.fence is None:
            return DeniedResolution.WRITE_FENCE_MISSING

        # Anyone who cannot be tied to a registered, confirmed subject is
        # barred from every durable scope (sections 6.3, 13.2/13.4).
        if subject.active_subject_id is None:
            return DeniedResolution.UNKNOWN_SUBJECT
        if not subject.registered:
            return DeniedResolution.UNREGISTERED_GUEST
        if subject.speaker_state != SpeakerState.SPEAKER_STATE_CONFIRMED.value:
            return DeniedResolution.UNKNOWN_SUBJECT

        if requested is MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE:
            return self._resolve_private(context)
        if requested is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY:
            return self._resolve_guardian_summary(context)
        if requested is MemoryScope.MEMORY_SCOPE_FAMILY_SHARED:
            return self._resolve_family_shared(context)
        if requested is MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE:
            return self._resolve_legacy(context)
        return DeniedResolution.MISSING_SOURCE

    # -- per-scope rules -------------------------------------------------

    def _resolve_ephemeral(self, context: ResolutionContext) -> ScopeResolution:
        """Session-local context only; no consent or evidence required but
        the policy must explicitly keep it out of long-term memory."""
        policy = context.policy
        ephemeral_ok = policy.has_obligation(
            OBLIGATION_DO_NOT_PERSIST
        ) or policy.has_obligation(OBLIGATION_RETENTION_TTL)
        if not ephemeral_ok:
            return DeniedResolution.POLICY_DENIED
        retention: RetentionPolicy = (
            "ttl" if policy.has_obligation(OBLIGATION_RETENTION_TTL) else "session_only"
        )
        return ScopeResolution(
            scope=MemoryScope.MEMORY_SCOPE_SESSION_EPHEMERAL,
            persistable=False,
            reason_code="ephemeral_session_context",
            retention=retention,
        )

    def _resolve_private(self, context: ResolutionContext) -> ScopeResolution:
        subject = context.subject
        policy = context.policy
        if subject.subject_category == SubjectCategory.SUBJECT_CATEGORY_UNKNOWN.value:
            return DeniedResolution.UNKNOWN_CATEGORY
        if policy.has_obligation(OBLIGATION_PERSIST_AGGREGATE_ONLY):
            # The policy only allowed an aggregate projection, not a private
            # verbatim record; writing the full content would exceed it.
            return DeniedResolution.POLICY_DENIED
        subject_id = subject.active_subject_id
        if subject_id is None:
            return DeniedResolution.UNKNOWN_SUBJECT
        return self._require_consent(
            context,
            scope=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            covers=subject_id,
        )

    def _resolve_guardian_summary(self, context: ResolutionContext) -> ScopeResolution:
        """Guardian projection of a minor subject (sections 6.5, 8.7).

        Only an aggregate summary may be persisted; verbatim chat is never
        allowed in this scope and a guardian relationship must be active.
        """
        subject = context.subject
        policy = context.policy
        if subject.subject_category != SubjectCategory.SUBJECT_CATEGORY_MINOR.value:
            return DeniedResolution.UNKNOWN_CATEGORY
        if not subject.guardian_relationship_active:
            return DeniedResolution.GUARDIAN_NOT_ACTIVE
        if not policy.has_obligation(OBLIGATION_PERSIST_AGGREGATE_ONLY):
            return DeniedResolution.POLICY_DENIED
        subject_id = subject.active_subject_id
        if subject_id is None:
            return DeniedResolution.UNKNOWN_SUBJECT
        return self._require_consent(
            context,
            scope=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
            covers=subject_id,
        )

    def _resolve_family_shared(self, context: ResolutionContext) -> ScopeResolution:
        """Family-shared memory (section 5.4 case 3, PR-14).

        Persistence itself requires the consent snapshot to cover every
        co-subject; visibility additionally requires each co-subject's
        explicit confirmation through the proposal lifecycle (service layer).
        """
        subject = context.subject
        policy = context.policy
        if subject.family_space_id is None:
            return DeniedResolution.MISSING_SOURCE
        if policy.has_obligation(OBLIGATION_PERSIST_AGGREGATE_ONLY):
            return DeniedResolution.POLICY_DENIED
        subject_id = subject.active_subject_id
        if subject_id is None:
            return DeniedResolution.UNKNOWN_SUBJECT
        covers = tuple(dict.fromkeys((subject_id, *context.co_subjects.subject_ids)))
        return self._require_consent(
            context,
            scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
            covers=covers,
            co_subjects=context.co_subjects,
        )

    def _resolve_legacy(self, context: ResolutionContext) -> ScopeResolution:
        """Legacy archive writes need an adult category and subject approval."""
        subject = context.subject
        policy = context.policy
        if subject.subject_category != SubjectCategory.SUBJECT_CATEGORY_ADULT.value:
            return DeniedResolution.UNKNOWN_CATEGORY
        if not (
            policy.has_obligation(OBLIGATION_REQUIRE_SUBJECT_APPROVAL)
            or policy.effect == PolicyEffect.POLICY_EFFECT_ALLOW
        ):
            return DeniedResolution.POLICY_DENIED
        subject_id = subject.active_subject_id
        if subject_id is None:
            return DeniedResolution.UNKNOWN_SUBJECT
        return self._require_consent(
            context,
            scope=MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,
            covers=subject_id,
        )

    # -- shared helpers --------------------------------------------------

    def _require_consent(
        self,
        context: ResolutionContext,
        *,
        scope: MemoryScope,
        covers: str | tuple[str, ...],
        co_subjects: CoSubjectContext | None = None,
    ) -> ScopeResolution:
        consent = context.consent
        expected = (covers,) if isinstance(covers, str) else covers
        if consent.snapshot_id is None or not consent.granted:
            return DeniedResolution.CONSENT_MISSING
        covered = set(consent.covers_subjects)
        if not all(subject_id in covered for subject_id in expected):
            return DeniedResolution.CONSENT_MISSING
        if co_subjects is not None:
            required = set(co_subjects.subject_ids)
            if required and required != set(co_subjects.confirmed_subject_ids):
                return DeniedResolution.CO_SUBJECTS_UNCONFIRMED
        return ScopeResolution(
            scope=scope,
            persistable=True,
            reason_code="resolved",
            raw_transcript_allowed=scope is not MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
        )

    def speaker_confirmation_required(self, context: ResolutionContext) -> bool:
        """True when the policy or scope demands a confirmed speaker."""
        policy = context.policy
        if policy.has_obligation(OBLIGATION_REQUIRE_SPEAKER_CONFIRMATION):
            return True
        return (
            context.requested_scope in DURABLE_SCOPES
            and context.subject.speaker_state
            != SpeakerState.SPEAKER_STATE_CONFIRMED.value
        )

    def requires_receipt_write(self, context: ResolutionContext) -> bool:
        """True when the caller must persist a policy receipt first."""
        return context.policy.has_obligation(OBLIGATION_WRITE_POLICY_RECEIPT)


#: Module-level singleton; the resolver is stateless.
default_resolver = MemoryScopeResolver()
