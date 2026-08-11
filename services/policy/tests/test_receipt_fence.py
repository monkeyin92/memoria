"""PolicyReceiptV2 immutability and exact-fence validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel
from services.policy.context import context_hash
from services.policy.engine import PolicyEngine
from services.policy.receipt_store import InMemoryPolicyReceiptWriter
from services.policy.receipts import (
    PolicyReceiptV2,
    exact_evidence_fence_valid,
    receipt_authorizes_relationship,
    receipt_fence_valid,
)
from services.policy.tests.fakes import (
    make_binding,
    make_consent,
    make_consent_snapshot_ref,
    make_context,
    make_relationship,
)

NOW = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)


def _wire_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, tuple):
        return [_wire_value(item) for item in value]
    return value


def _replace_receipt(
    receipt: PolicyReceiptV2, **changes: object
) -> PolicyReceiptV2:
    payload = receipt.model_dump(mode="json")
    payload.update({key: _wire_value(value) for key, value in changes.items()})
    return PolicyReceiptV2.model_validate(payload)


def _valid_receipt() -> PolicyReceiptV2:
    context = make_context(evaluated_at=NOW)
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-validation")
    decision = engine.decide(context)
    return engine.receipt_for(context, decision)


def test_receipt_references_current_global_snapshot_not_historical_source() -> None:
    consent = make_consent(
        snapshot_id="historical-source-snapshot",
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    current = make_consent_snapshot_ref(
        snapshot_id="current-global-snapshot",
        revision=9,
        canonical_hash="9" * 64,
        consents=(consent,),
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        data_classification="biometric",
        consent_evidence=(consent,),
        consent_snapshot_evidence=(current,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))

    assert consent.snapshot_id == "historical-source-snapshot"
    assert receipt.consent_snapshot_ids == ("current-global-snapshot",)
    assert receipt.consent_snapshot_revisions == (9,)
    assert exact_evidence_fence_valid(receipt, context=context, now=NOW)
    assert not exact_evidence_fence_valid(
        receipt,
        context=replace(
            context,
            consent_snapshot_evidence=(replace(current, revision=10),),
        ),
        now=NOW,
    )


def _evidence_backed_context() -> object:
    consent = make_consent(
        capability="voice_clone_use", purpose="voice_clone", now=NOW
    )
    return make_context(
        capability="voice_clone_use",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )


def test_every_allow_and_deny_produces_a_v2_receipt() -> None:
    writer = InMemoryPolicyReceiptWriter()
    engine = PolicyEngine(receipt_writer=writer)
    allowed = engine.decide(
        _evidence_backed_context()  # type: ignore[arg-type]
    )
    denied = engine.decide(
        make_context(
            capability="voice_clone_use",
            consent_evidence=(),
            binding_evidence=None,
            evaluated_at=NOW,
        )
    )
    for decision in (allowed, denied):
        receipt = writer.get(decision.receipt_id)
        assert receipt is not None
        assert isinstance(receipt, PolicyReceiptV2)
        assert receipt.effect == decision.effect
        assert receipt.context_hash == decision.context_hash
        assert receipt.receipt_id == decision.receipt_id


def test_v2_receipt_carries_full_fence_fields() -> None:
    consent = make_consent(
        consent_id="consent-1",
        snapshot_id="snap-1",
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    relationship = make_relationship(
        snapshot_id="rel-snap-1",
        target_person_id="person-adult",
        relation_type="delegate_for",
        now=NOW,
    )
    context = make_context(
        capability="voice_clone_use",
        consent_evidence=(consent,),
        relationship_evidence=(relationship,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-full")
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    assert receipt.consent_snapshot_ids == ("snap-1",)
    assert receipt.consent_snapshot_revisions == (1,)
    # The unrelated delegate did not participate in voice-clone authorization.
    assert receipt.relationship_snapshot_ids == ()
    assert receipt.binding_id == "binding-1"
    assert receipt.binding_version == 1
    assert receipt.device_trust == "trusted"
    assert receipt.data_classification == "private"
    assert receipt.safety_state == "normal"
    assert receipt.jurisdiction == "CN"
    assert receipt.exact_fence is True


def test_exact_evidence_fence_passes_for_unchanged_active_evidence() -> None:
    context = _evidence_backed_context()  # type: ignore[var-annotated]
    decision = PolicyEngine().decide(context)
    receipt = PolicyEngine().receipt_for(context, decision)
    assert exact_evidence_fence_valid(
        receipt, context=context, now=NOW + timedelta(minutes=1)
    )


def test_non_exact_receipt_passes_base_fence_but_never_exact_evidence_fence() -> None:
    context = make_context(evaluated_at=NOW)
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))

    assert receipt.exact_fence is False
    assert receipt_fence_valid(receipt, context=context, now=NOW + timedelta(minutes=1))
    assert not exact_evidence_fence_valid(
        receipt, context=context, now=NOW + timedelta(minutes=1)
    )


@pytest.mark.parametrize(
    ("context_field", "changed_value"),
    [
        ("actor_id", "person-other"),
        ("subject_id", "person-other"),
        ("resource_owner_id", "person-other"),
        ("device_id", "device-other"),
        ("capability", "voice_profile_create"),
        ("purpose", "voice_profile"),
        ("binding_id", "binding-other"),
        ("binding_version", 2),
        ("session_id", "session-other"),
        ("session_epoch", 2),
        ("runtime_profile_id", "profile-other"),
        ("subject_revision", 2),
    ],
)
def test_receipt_fence_compares_literal_authority_fields_even_with_synced_hash(
    context_field: str,
    changed_value: object,
) -> None:
    context = make_context(evaluated_at=NOW)
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))
    changed = replace(context, **{context_field: changed_value})
    forged = _replace_receipt(receipt, context_hash=context_hash(changed))

    assert not receipt_fence_valid(
        forged, context=changed, now=NOW + timedelta(minutes=1)
    )


def test_exact_fence_fails_on_context_hash_mismatch() -> None:
    context = _evidence_backed_context()  # type: ignore[var-annotated]
    decision = PolicyEngine().decide(context)
    receipt = PolicyEngine().receipt_for(context, decision)
    changed = replace(context, session_epoch=2)
    assert not receipt_fence_valid(
        receipt, context=changed, now=NOW + timedelta(minutes=1)
    )


def test_exact_fence_fails_when_receipt_expired() -> None:
    context = _evidence_backed_context()  # type: ignore[var-annotated]
    decision = PolicyEngine().decide(context)
    receipt = PolicyEngine().receipt_for(context, decision)
    assert not receipt_fence_valid(receipt, context=context, now=receipt.expires_at)
    assert not receipt_fence_valid(
        receipt, context=context, now=receipt.expires_at + timedelta(seconds=1)
    )


def test_exact_fence_fails_before_created_at() -> None:
    context = _evidence_backed_context()  # type: ignore[var-annotated]
    decision = PolicyEngine().decide(context)
    receipt = PolicyEngine().receipt_for(context, decision)
    assert not receipt_fence_valid(
        receipt,
        context=context,
        now=receipt.created_at - timedelta(seconds=1),
    )


@pytest.mark.parametrize("status", ["revoked", "disputed", "superseded", "expired"])
def test_exact_fence_fails_when_consent_snapshot_stops_being_active(
    status: str,
) -> None:
    context = _evidence_backed_context()  # type: ignore[var-annotated]
    decision = PolicyEngine().decide(context)
    receipt = PolicyEngine().receipt_for(context, decision)
    stale = replace(context, consent_evidence=())
    # A revoked/disputed/superseded/expired consent is no longer effective: the
    # engine would now deny and the old receipt must not stay valid.
    assert not exact_evidence_fence_valid(
        receipt, context=stale, now=NOW + timedelta(minutes=1)
    )
    mutated = replace(
        context,
        consent_evidence=(
            replace(context.consent_evidence[0], status=status),  # type: ignore[index]
        ),
    )
    assert not exact_evidence_fence_valid(
        receipt, context=mutated, now=NOW + timedelta(minutes=1)
    )


def test_exact_fence_fails_when_snapshot_version_changes() -> None:
    context = _evidence_backed_context()  # type: ignore[var-annotated]
    decision = PolicyEngine().decide(context)
    receipt = PolicyEngine().receipt_for(context, decision)
    bumped = replace(
        context,
        consent_evidence=(
            replace(context.consent_evidence[0], version=2),  # type: ignore[index]
        ),
    )
    assert not exact_evidence_fence_valid(
        receipt, context=bumped, now=NOW + timedelta(minutes=1)
    )


def test_exact_fence_fails_when_snapshot_id_changes() -> None:
    context = _evidence_backed_context()  # type: ignore[var-annotated]
    decision = PolicyEngine().decide(context)
    receipt = PolicyEngine().receipt_for(context, decision)
    swapped = replace(
        context,
        consent_evidence=(
            replace(context.consent_evidence[0], snapshot_id="snap-other"),  # type: ignore[index]
        ),
    )
    assert not exact_evidence_fence_valid(
        receipt, context=swapped, now=NOW + timedelta(minutes=1)
    )


def test_exact_fence_fails_when_relationship_snapshot_expires() -> None:
    relationship = make_relationship(
        snapshot_id="rel-snap-1",
        target_person_id="person-adult",
        relation_type="emergency_contact_for",
        now=NOW,
    )
    context = make_context(
        capability="crisis_notification",
        purpose="crisis_response",
        safety_state="self_crisis",
        relationship_evidence=(relationship,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    decision = PolicyEngine().decide(context)
    receipt = PolicyEngine().receipt_for(context, decision)
    assert receipt.relationship_snapshot_ids == ("rel-snap-1",)
    assert exact_evidence_fence_valid(
        receipt, context=context, now=NOW + timedelta(minutes=1)
    )
    expired = replace(context, relationship_evidence=())
    assert not exact_evidence_fence_valid(
        receipt, context=expired, now=NOW + timedelta(minutes=1)
    )


def test_base_fence_for_receipt_without_evidence_checks_hash_and_window() -> None:
    context = make_context(
        capability="chat",
        consent_evidence=(),
        relationship_evidence=(),
        binding_evidence=None,
        evaluated_at=NOW,
    )
    decision = PolicyEngine().decide(context)
    receipt = PolicyEngine().receipt_for(context, decision)
    assert receipt.exact_fence is False
    assert receipt_fence_valid(receipt, context=context, now=NOW + timedelta(minutes=1))
    assert not receipt_fence_valid(
        receipt,
        context=replace(context, session_epoch=2),
        now=NOW + timedelta(minutes=1),
    )
    assert not receipt_fence_valid(receipt, context=context, now=receipt.expires_at)


def test_exact_evidence_fence_rejects_binding_hash_change() -> None:
    context = _evidence_backed_context()  # type: ignore[var-annotated]
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))
    assert context.binding_evidence is not None
    changed = replace(
        context,
        binding_evidence=replace(context.binding_evidence, canonical_hash="d" * 64),
    )

    assert not exact_evidence_fence_valid(
        receipt, context=changed, now=NOW + timedelta(minutes=1)
    )


def test_receipt_records_only_consent_that_participated_in_authorization() -> None:
    used = make_consent(
        snapshot_id="snapshot-used",
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    unrelated = make_consent(
        consent_id="consent-unrelated",
        snapshot_id="snapshot-unrelated",
        capability="payment",
        purpose="payment",
        now=NOW,
    )
    current = make_consent_snapshot_ref(
        snapshot_id="current-global-voice-snapshot",
        revision=8,
        canonical_hash="8" * 64,
        consents=(unrelated, used),
        now=NOW,
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        consent_evidence=(unrelated, used),
        consent_snapshot_evidence=(current,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    engine = PolicyEngine()
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    assert receipt.consent_snapshot_ids == ("current-global-voice-snapshot",)
    assert receipt.consent_snapshot_revisions == (8,)
    assert current.contains(used)
    assert current.contains(unrelated)
    assert used.matches(
        context.subject_id,
        context.binding_id,
        context.binding_version,
        context.capability,
    )
    assert used.purpose == context.purpose
    assert not unrelated.matches(
        context.subject_id,
        context.binding_id,
        context.binding_version,
        context.capability,
    )

    unrelated_revoked = replace(
        context,
        consent_evidence=(replace(unrelated, status="revoked"), used),
    )
    # Snapshot references remain minimal, while the complete context hash
    # freezes even unrelated canonical evidence changes.
    assert not exact_evidence_fence_valid(
        receipt, context=unrelated_revoked, now=NOW + timedelta(minutes=1)
    )

    used_revoked = replace(
        context,
        consent_evidence=(unrelated, replace(used, status="revoked")),
    )
    assert not exact_evidence_fence_valid(
        receipt, context=used_revoked, now=NOW + timedelta(minutes=1)
    )
    assert PolicyEngine().decide(used_revoked).effect == "deny"


def test_receipt_authorizes_only_recorded_relationship_snapshot() -> None:
    receipt = _replace_receipt(
        _valid_receipt(),
        relationship_snapshot_ids=("rel-used",),
        relationship_snapshot_revisions=(1,),
        binding_canonical_hash="c" * 64,
        exact_fence=True,
    )

    assert receipt_authorizes_relationship(receipt, "rel-used")
    assert not receipt_authorizes_relationship(receipt, "rel-unused")


def test_crisis_receipt_records_only_rule_selected_relationship_and_revocation_denies() -> None:
    emergency = make_relationship(
        relationship_id="rel-emergency",
        snapshot_id="snapshot-emergency",
        relation_type="emergency_contact_for",
        target_person_id="person-adult",
        now=NOW,
    )
    guardian = make_relationship(
        relationship_id="rel-guardian",
        snapshot_id="snapshot-guardian",
        relation_type="guardian_of",
        target_person_id="person-adult",
        now=NOW,
    )
    context = make_context(
        actor_id="safety-kernel",
        capability="crisis_notification",
        purpose="crisis_response",
        safety_state="self_crisis",
        data_classification="safety_minimum",
        relationship_evidence=(guardian, emergency),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))

    assert receipt.relationship_snapshot_ids == ("snapshot-emergency",)
    assert receipt.relationship_snapshot_revisions == (1,)
    assert receipt_authorizes_relationship(receipt, "snapshot-emergency")
    assert not receipt_authorizes_relationship(receipt, "snapshot-guardian")

    unrelated_revoked = replace(
        context,
        relationship_evidence=(replace(guardian, status="revoked"), emergency),
    )
    # The guardian was not a notification target, but its canonical status was
    # still part of the complete decision context hash.
    assert not exact_evidence_fence_valid(
        receipt, context=unrelated_revoked, now=NOW + timedelta(minutes=1)
    )

    selected_revoked = replace(
        context,
        relationship_evidence=(guardian, replace(emergency, status="revoked")),
    )
    assert not exact_evidence_fence_valid(
        receipt, context=selected_revoked, now=NOW + timedelta(minutes=1)
    )
    assert PolicyEngine().decide(selected_revoked).effect == "deny"

    selected_revision_bumped = replace(
        context,
        relationship_evidence=(guardian, replace(emergency, revision=2)),
    )
    assert not exact_evidence_fence_valid(
        receipt,
        context=selected_revision_bumped,
        now=NOW + timedelta(minutes=1),
    )


def test_in_memory_writer_keeps_v2_immutability_semantics() -> None:
    writer = InMemoryPolicyReceiptWriter()
    context = _evidence_backed_context()  # type: ignore[var-annotated]
    PolicyEngine(
        receipt_id_factory=lambda: "receipt-immutable-2",
        receipt_writer=writer,
    ).decide(context)
    receipt = writer.get("receipt-immutable-2")
    assert receipt is not None
    with pytest.raises(ValueError, match="immutable"):
        writer.write(_replace_receipt(receipt, reason_code="changed"))


def test_v2_receipt_is_frozen() -> None:
    receipt = _valid_receipt()
    with pytest.raises(ValueError, match="frozen"):
        receipt.reason_code = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(
    "field",
    ["binding_version", "session_epoch", "subject_revision"],
)
def test_v2_receipt_rejects_bool_numeric_fields(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _replace_receipt(_valid_receipt(), **{field: True})


def test_v2_receipt_rejects_bool_snapshot_revision() -> None:
    with pytest.raises(ValueError, match="consent_snapshot_revisions"):
        _replace_receipt(
            _valid_receipt(),
            consent_snapshot_ids=("snapshot-1",),
            consent_snapshot_revisions=(True,),
        )


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "g" * 64])
def test_v2_receipt_rejects_invalid_binding_canonical_hash(digest: str) -> None:
    with pytest.raises(ValueError, match="binding_canonical_hash"):
        _replace_receipt(_valid_receipt(), binding_canonical_hash=digest)


def test_binding_only_chat_receipt_is_an_exact_evidence_fence() -> None:
    context = make_context(
        capability="chat",
        consent_evidence=(),
        relationship_evidence=(),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))

    assert receipt.exact_fence is True
    assert receipt.binding_canonical_hash == "c" * 64
    assert receipt.consent_snapshot_ids == ()
    assert receipt.relationship_snapshot_ids == ()
    assert exact_evidence_fence_valid(
        receipt, context=context, now=NOW + timedelta(minutes=1)
    )


@pytest.mark.parametrize(
    "changed_context",
    [
        make_context(
            binding_evidence=make_binding(status="revoked", now=NOW),
            evaluated_at=NOW,
        ),
        make_context(
            binding_version=2,
            binding_evidence=make_binding(version=2, now=NOW),
            evaluated_at=NOW,
        ),
        make_context(
            device_trust="offline",
            binding_evidence=make_binding(now=NOW),
            evaluated_at=NOW,
        ),
    ],
)
def test_binding_only_exact_fence_fails_closed_when_binding_authority_changes(
    changed_context: object,
) -> None:
    context = make_context(binding_evidence=make_binding(now=NOW), evaluated_at=NOW)
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))

    assert not exact_evidence_fence_valid(
        receipt,
        context=changed_context,  # type: ignore[arg-type]
        now=NOW + timedelta(minutes=1),
    )


def test_receipt_without_binding_is_never_an_exact_evidence_fence() -> None:
    context = make_context(binding_evidence=None, evaluated_at=NOW)
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))

    assert receipt.exact_fence is False
    assert receipt.binding_canonical_hash is None
    assert not exact_evidence_fence_valid(
        receipt, context=context, now=NOW + timedelta(minutes=1)
    )


def test_consent_without_binding_does_not_create_an_exact_evidence_fence() -> None:
    context = make_context(
        consent_evidence=(make_consent(capability="chat", now=NOW),),
        binding_evidence=None,
        evaluated_at=NOW,
    )
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))

    assert receipt.exact_fence is False
    assert receipt.binding_canonical_hash is None


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("capability", "teleport"),
        ("effect", "maybe"),
        ("purpose", "anything"),
        ("device_trust", "almost_trusted"),
        ("data_classification", "secret"),
        ("safety_state", "panic"),
    ],
)
def test_v2_receipt_rejects_invalid_enums(field: str, bad: str) -> None:
    with pytest.raises(ValueError, match=field):
        _replace_receipt(_valid_receipt(), **{field: bad})


@pytest.mark.parametrize(
    "field",
    [
        "receipt_id",
        "actor_id",
        "resource_owner_id",
        "device_id",
        "binding_id",
        "session_id",
        "runtime_profile_id",
        "reason_code",
        "policy_version",
        "jurisdiction",
    ],
)
def test_v2_receipt_rejects_empty_required_strings(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _replace_receipt(_valid_receipt(), **{field: ""})


def test_v2_receipt_rejects_empty_optional_subject_id() -> None:
    with pytest.raises(ValueError, match="subject_id"):
        _replace_receipt(_valid_receipt(), subject_id="")


def test_v2_receipt_rejects_overlong_id() -> None:
    with pytest.raises(ValueError, match="actor_id"):
        _replace_receipt(_valid_receipt(), actor_id="x" * 129)


@pytest.mark.parametrize("bad_hash", ["", "a" * 63, "A" * 64, "g" * 64])
def test_v2_receipt_rejects_bad_context_hash(bad_hash: str) -> None:
    with pytest.raises(ValueError, match="context_hash"):
        _replace_receipt(_valid_receipt(), context_hash=bad_hash)


def test_v2_receipt_preserves_null_resource_owner_id_for_denials() -> None:
    receipt = _replace_receipt(
        _valid_receipt(), resource_owner_id=None, effect="deny"
    )
    assert receipt.resource_owner_id is None


@pytest.mark.parametrize("exact_fence", [0, 1, "true", None])
def test_v2_receipt_rejects_non_bool_exact_fence(exact_fence: object) -> None:
    with pytest.raises(ValueError, match="exact_fence"):
        _replace_receipt(_valid_receipt(), exact_fence=exact_fence)


def test_v2_receipt_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="created_at"):
        _replace_receipt(
            _valid_receipt(), created_at=datetime(2026, 8, 9, 8, 0)
        )


def test_v2_receipt_rejects_non_increasing_expiry() -> None:
    receipt = _valid_receipt()
    with pytest.raises(ValueError, match="time window"):
        _replace_receipt(receipt, expires_at=receipt.created_at)


def test_v2_receipt_rejects_non_obligation_items() -> None:
    with pytest.raises(ValueError, match="obligations"):
        _replace_receipt(_valid_receipt(), obligations=("NO_MODEL_TRAINING",))
