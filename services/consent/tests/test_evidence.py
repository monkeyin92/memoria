"""Canonical evidence contracts: construction validation, canonical hashing, tamper detection."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from services.consent.evidence import (
    BindingEvidence,
    ConsentEvidence,
    ConsentOffer,
    ConsentParams,
    ConsentSnapshot,
    RelationshipEvidence,
    canonical_json,
    compute_canonical_hash,
)


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def base_params(**overrides: object) -> ConsentParams:
    values: dict[str, object] = {
        "max_session_seconds": 3600,
        "retention_ttl_seconds": 90 * 86400,
        "quiet_hours": ("22:00", "07:00"),
        "extras": (("scope", "voice_recognition"),),
    }
    values.update(overrides)
    return ConsentParams(**values)


def base_binding(**overrides: object) -> BindingEvidence:
    values: dict[str, object] = {
        "binding_id": "bd_1",
        "version": 1,
        "device_id": "dev_1",
        "status": "active",
        "declared_mode": "parent_for_child",
        "valid_from": utc("2026-08-09T00:00:00+00:00"),
        "valid_until": utc("2027-08-09T00:00:00+00:00"),
        "canonical_hash": "",
    }
    values.update(overrides)
    return BindingEvidence(**values)


def base_relationship(**overrides: object) -> RelationshipEvidence:
    values: dict[str, object] = {
        "relationship_id": "rel_1",
        "snapshot_id": "rs_1",
        "revision": 1,
        "relation_type": "guardian_of",
        "status": "active",
        "source_person_id": "person_guardian",
        "target_person_id": "person_minor",
        "binding_id": "bd_1",
        "valid_from": utc("2026-08-09T00:00:00+00:00"),
        "valid_until": utc("2027-08-09T00:00:00+00:00"),
        "canonical_hash": "",
    }
    values.update(overrides)
    return RelationshipEvidence(**values)


def base_evidence(**overrides: object) -> ConsentEvidence:
    values: dict[str, object] = {
        "consent_id": "c_1",
        "version": 1,
        "snapshot_id": "snap_1",
        "status": "active",
        "subject_id": "person_minor",
        "resource_owner_id": "person_minor",
        "actor_id": "person_guardian",
        "actor_kind": "guardian",
        "device_id": "dev_1",
        "binding_id": "bd_1",
        "binding_version": 1,
        "capability": "chat",
        "purpose": "user_request",
        "policy_version": "policy-cn-minor-v5",
        "evidence_id": "ev_1",
        "offer_id": "of_1",
        "idempotency_key": None,
        "params": base_params(),
        "valid_from": utc("2026-08-09T00:00:00+00:00"),
        "valid_until": utc("2027-08-09T00:00:00+00:00"),
        "supersedes_consent_id": None,
        "superseded_by_consent_id": None,
        "canonical_hash": "",
    }
    values.update(overrides)
    return ConsentEvidence(**values)


def base_offer(**overrides: object) -> ConsentOffer:
    values: dict[str, object] = {
        "offer_id": "of_1",
        "capability": "chat",
        "subject_id": "person_minor",
        "actor_id": "person_guardian",
        "resource_owner_id": "person_minor",
        "purpose": "user_request",
        "params": base_params(),
        "valid_from": utc("2026-08-09T00:00:00+00:00"),
        "valid_until": utc("2027-08-09T00:00:00+00:00"),
        "created_at": utc("2026-08-09T00:00:00+00:00"),
        "policy_version": "policy-cn-minor-v5",
        "version": 1,
        "status": "active",
        "issuer": "consent_authority",
        "supersedes_offer_id": None,
        "canonical_hash": "",
    }
    values.update(overrides)
    return ConsentOffer(**values)


def _base_snapshot() -> ConsentSnapshot:
    return ConsentSnapshot(
        snapshot_id="snap_1",
        version=1,
        subject_id="person_minor",
        binding_id="bd_1",
        binding_version=1,
        policy_version="policy-cn-minor-v5",
        created_at=utc("2026-08-09T00:00:00+00:00"),
        grants=(base_evidence(),),
        relationships=(base_relationship(),),
        binding=base_binding(),
    )


def _rehash(data: dict[str, object]) -> dict[str, object]:
    payload = deepcopy(data)
    payload.pop("canonical_hash", None)
    data["canonical_hash"] = compute_canonical_hash(payload)
    return data


CanonicalDecoder = Callable[[dict[str, object]], object]


@pytest.mark.parametrize(
    ("factory", "decoder", "field"),
    [
        (base_evidence, ConsentEvidence.from_canonical_dict, "version"),
        (base_relationship, RelationshipEvidence.from_canonical_dict, "revision"),
        (base_binding, BindingEvidence.from_canonical_dict, "version"),
        (_base_snapshot, ConsentSnapshot.from_canonical_dict, "version"),
        (base_offer, ConsentOffer.from_canonical_dict, "version"),
    ],
)
@pytest.mark.parametrize("bad", [True, 1.0, "1"])
def test_authoritative_decoders_require_exact_integer_types(
    factory: Callable[[], object],
    decoder: CanonicalDecoder,
    field: str,
    bad: object,
) -> None:
    source = factory()
    data = source.to_canonical_dict()  # type: ignore[attr-defined]
    data[field] = bad
    with pytest.raises(ValueError, match=rf"{field} must be an integer"):
        decoder(_rehash(data))


@pytest.mark.parametrize(
    ("factory", "decoder", "field"),
    [
        (base_evidence, ConsentEvidence.from_canonical_dict, "consent_id"),
        (base_relationship, RelationshipEvidence.from_canonical_dict, "relationship_id"),
        (base_binding, BindingEvidence.from_canonical_dict, "binding_id"),
        (_base_snapshot, ConsentSnapshot.from_canonical_dict, "snapshot_id"),
        (base_offer, ConsentOffer.from_canonical_dict, "offer_id"),
    ],
)
@pytest.mark.parametrize("bad", [1, True, 1.0, ["id"]])
def test_authoritative_decoders_never_coerce_text_fields(
    factory: Callable[[], object],
    decoder: CanonicalDecoder,
    field: str,
    bad: object,
) -> None:
    source = factory()
    data = source.to_canonical_dict()  # type: ignore[attr-defined]
    data[field] = bad
    with pytest.raises(ValueError, match=rf"{field} must be a string"):
        decoder(_rehash(data))


@pytest.mark.parametrize(
    ("factory", "decoder"),
    [
        (base_evidence, ConsentEvidence.from_canonical_dict),
        (base_relationship, RelationshipEvidence.from_canonical_dict),
        (base_binding, BindingEvidence.from_canonical_dict),
        (_base_snapshot, ConsentSnapshot.from_canonical_dict),
        (base_offer, ConsentOffer.from_canonical_dict),
    ],
)
def test_authoritative_decoders_require_canonical_hash(
    factory: Callable[[], object], decoder: CanonicalDecoder
) -> None:
    source = factory()
    data = source.to_canonical_dict()  # type: ignore[attr-defined]
    del data["canonical_hash"]
    with pytest.raises(ValueError, match="canonical_hash"):
        decoder(data)


@pytest.mark.parametrize("bad", [True, 1.0, "3600"])
def test_authoritative_params_decoder_rejects_coerced_integer(bad: object) -> None:
    data = base_evidence().to_canonical_dict()
    params = deepcopy(data["params"])
    assert isinstance(params, dict)
    params["max_session_seconds"] = bad
    data["params"] = params
    with pytest.raises(ValueError, match="max_session_seconds must be an integer"):
        ConsentEvidence.from_canonical_dict(_rehash(data))


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("device_id", 7),
        ("idempotency_key", False),
        ("supersedes_consent_id", 7),
    ],
)
def test_authoritative_decoder_rejects_non_string_optional_values(
    field: str, bad: object
) -> None:
    data = base_evidence().to_canonical_dict()
    data[field] = bad
    with pytest.raises(ValueError, match=rf"{field} must be a string or null"):
        ConsentEvidence.from_canonical_dict(_rehash(data))


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("quiet_hours", ["22:00", 700]),
        ("extras", [["scope", 7]]),
        ("extras", [["scope"]]),
    ],
)
def test_authoritative_params_decoder_rejects_malformed_sequences(
    field: str, bad: object
) -> None:
    data = base_offer().to_canonical_dict()
    params = deepcopy(data["params"])
    assert isinstance(params, dict)
    params[field] = bad
    data["params"] = params
    with pytest.raises(ValueError, match=field):
        ConsentOffer.from_canonical_dict(_rehash(data))


class TestConsentParams:
    def test_valid_params(self) -> None:
        params = base_params()
        assert params.max_session_seconds == 3600
        assert params.quiet_hours == ("22:00", "07:00")
        assert params.extras == (("scope", "voice_recognition"),)

    def test_invalid_max_session_seconds(self) -> None:
        with pytest.raises(ValueError):
            base_params(max_session_seconds=0)
        with pytest.raises(ValueError):
            base_params(max_session_seconds=-1)

    def test_invalid_retention_ttl(self) -> None:
        with pytest.raises(ValueError):
            base_params(retention_ttl_seconds=0)

    def test_invalid_quiet_hours_format(self) -> None:
        for bad in (("25:00", "07:00"), ("9:00", "07:00"), ("22:00", "7:00"), ("22:60", "07:00")):
            with pytest.raises(ValueError):
                base_params(quiet_hours=bad)

    def test_extras_sorted_and_deduplicated(self) -> None:
        params = base_params(extras=(("z", "last"), ("a", "1"), ("a", "2")))
        assert params.extras == (("a", "2"), ("z", "last"))

    def test_extras_bounded(self) -> None:
        with pytest.raises(ValueError):
            base_params(extras=(("", "x"),))
        with pytest.raises(ValueError):
            base_params(extras=(("key", ""),))
        with pytest.raises(ValueError):
            base_params(extras=(("k" * 129, "x"),))


class TestCanonicalHash:
    def test_deterministic_across_instances(self) -> None:
        a = base_evidence()
        b = base_evidence()
        assert a.canonical_hash == b.canonical_hash
        assert len(a.canonical_hash) == 64

    def test_field_change_changes_hash(self) -> None:
        a = base_evidence()
        b = base_evidence(capability="tutor")
        assert a.canonical_hash != b.canonical_hash

    def test_offer_id_cannot_spoof_hash(self) -> None:
        # Hash covers the full authoritative field set, not just the offer id.
        a = base_evidence()
        forged = base_evidence(offer_id="of_forged")
        assert a.canonical_hash != forged.canonical_hash

    def test_tamper_detection(self) -> None:
        original = base_evidence()
        forged = base_evidence(subject_id="person_attacker")
        with pytest.raises(ValueError):
            # Reuse the original hash with different authoritative fields.
            base_evidence(subject_id="person_attacker", canonical_hash=original.canonical_hash)
        assert forged.subject_id == "person_attacker"

    def test_datetime_timezone_canonicalization(self) -> None:
        a = base_evidence(valid_from=utc("2026-08-09T08:00:00+08:00"))
        b = base_evidence(valid_from=utc("2026-08-09T00:00:00+00:00"))
        assert a.canonical_hash == b.canonical_hash

    def test_compute_canonical_hash_helper(self) -> None:
        payload = {"b": 2, "a": 1}
        h1 = compute_canonical_hash(payload)
        h2 = compute_canonical_hash({"a": 1, "b": 2})
        assert h1 == h2
        assert h1 == compute_canonical_hash(payload)
        assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'


class TestConsentEvidence:
    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError):
            base_evidence(valid_from=datetime(2026, 8, 9))
        with pytest.raises(ValueError):
            base_evidence(valid_until=datetime(2026, 8, 9))

    def test_version_ge_one(self) -> None:
        with pytest.raises(ValueError):
            base_evidence(version=0)

    def test_valid_until_after_valid_from(self) -> None:
        with pytest.raises(ValueError):
            base_evidence(
                valid_from=utc("2026-08-10T00:00:00+00:00"),
                valid_until=utc("2026-08-09T00:00:00+00:00"),
            )

    def test_invalid_status(self) -> None:
        with pytest.raises(ValueError):
            base_evidence(status="bogus")  # type: ignore[arg-type]

    def test_invalid_capability(self) -> None:
        with pytest.raises(ValueError):
            base_evidence(capability="bogus")  # type: ignore[arg-type]

    def test_invalid_actor_kind(self) -> None:
        with pytest.raises(ValueError):
            base_evidence(actor_kind="bogus")  # type: ignore[arg-type]

    def test_bounded_ids(self) -> None:
        with pytest.raises(ValueError):
            base_evidence(consent_id="")
        with pytest.raises(ValueError):
            base_evidence(consent_id="x" * 129)

    def test_is_effective_at(self) -> None:
        evidence = base_evidence()
        assert evidence.is_effective_at(utc("2026-09-01T00:00:00+00:00"))
        assert not evidence.is_effective_at(utc("2026-08-08T23:59:59+00:00"))
        assert not evidence.is_effective_at(utc("2027-08-09T00:00:00+00:00"))
        revoked = base_evidence(status="revoked")
        assert not revoked.is_effective_at(utc("2026-09-01T00:00:00+00:00"))

    def test_matches(self) -> None:
        evidence = base_evidence()
        assert evidence.matches("person_minor", "bd_1", 1, "chat")
        assert not evidence.matches("person_other", "bd_1", 1, "chat")
        assert not evidence.matches("person_minor", "bd_2", 1, "chat")
        assert not evidence.matches("person_minor", "bd_1", 2, "chat")
        assert not evidence.matches("person_minor", "bd_1", 1, "tutor")


class TestRelationshipEvidence:
    def test_valid(self) -> None:
        rel = base_relationship()
        assert rel.is_active_at(utc("2026-09-01T00:00:00+00:00"))

    def test_invalid_relation_type(self) -> None:
        with pytest.raises(ValueError):
            base_relationship(relation_type="bogus")  # type: ignore[arg-type]

    def test_invalid_status(self) -> None:
        with pytest.raises(ValueError):
            base_relationship(status="bogus")  # type: ignore[arg-type]

    def test_revision_ge_one(self) -> None:
        with pytest.raises(ValueError):
            base_relationship(revision=0)

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError):
            base_relationship(valid_from=datetime(2026, 8, 9))

    def test_is_active_at(self) -> None:
        rel = base_relationship(status="suspended")
        assert not rel.is_active_at(utc("2026-09-01T00:00:00+00:00"))
        rel = base_relationship(
            valid_from=utc("2026-08-09T00:00:00+00:00"),
            valid_until=utc("2026-08-10T00:00:00+00:00"),
        )
        assert rel.is_active_at(utc("2026-08-09T12:00:00+00:00"))
        assert not rel.is_active_at(utc("2026-08-10T00:00:00+00:00"))

    def test_hash_deterministic(self) -> None:
        assert base_relationship().canonical_hash == base_relationship().canonical_hash


class TestBindingEvidence:
    def test_valid(self) -> None:
        binding = base_binding()
        assert binding.is_active_at(utc("2026-09-01T00:00:00+00:00"))

    def test_invalid_declared_mode(self) -> None:
        with pytest.raises(ValueError):
            base_binding(declared_mode="bogus")  # type: ignore[arg-type]

    def test_invalid_status(self) -> None:
        with pytest.raises(ValueError):
            base_binding(status="bogus")  # type: ignore[arg-type]

    def test_version_ge_one(self) -> None:
        with pytest.raises(ValueError):
            base_binding(version=0)

    def test_is_active_at(self) -> None:
        binding = base_binding(status="superseded")
        assert not binding.is_active_at(utc("2026-09-01T00:00:00+00:00"))
        binding = base_binding(
            valid_from=utc("2026-08-09T00:00:00+00:00"),
            valid_until=utc("2026-08-10T00:00:00+00:00"),
        )
        assert binding.is_active_at(utc("2026-08-09T12:00:00+00:00"))
        assert not binding.is_active_at(utc("2026-08-10T00:00:00+00:00"))

    def test_hash_deterministic(self) -> None:
        assert base_binding().canonical_hash == base_binding().canonical_hash


class TestConsentSnapshot:
    def _snapshot(self, **overrides: object) -> ConsentSnapshot:
        values: dict[str, object] = {
            "snapshot_id": "snap_1",
            "version": 1,
            "subject_id": "person_minor",
            "binding_id": "bd_1",
            "binding_version": 1,
            "policy_version": "policy-cn-minor-v5",
            "created_at": utc("2026-08-09T00:00:00+00:00"),
            "grants": (base_evidence(),),
            "relationships": (base_relationship(),),
            "binding": base_binding(),
            "canonical_hash": "",
        }
        values.update(overrides)
        return ConsentSnapshot(**values)

    def test_valid_snapshot(self) -> None:
        snapshot = self._snapshot()
        assert snapshot.checksum == snapshot.canonical_hash
        assert len(snapshot.canonical_hash) == 64
        assert isinstance(snapshot.grants, tuple)

    def test_version_ge_one(self) -> None:
        with pytest.raises(ValueError):
            self._snapshot(version=0)

    def test_naive_created_at_rejected(self) -> None:
        with pytest.raises(ValueError):
            self._snapshot(created_at=datetime(2026, 8, 9))

    def test_deterministic_hash(self) -> None:
        assert self._snapshot().canonical_hash == self._snapshot().canonical_hash

    def test_grants_change_hash(self) -> None:
        other = base_evidence(capability="tutor", evidence_id="ev_2", consent_id="c_2")
        assert self._snapshot().canonical_hash != self._snapshot(grants=(other,)).canonical_hash

    def test_tamper_detection(self) -> None:
        original = self._snapshot()
        with pytest.raises(ValueError):
            self._snapshot(binding_version=2, canonical_hash=original.canonical_hash)

    def test_roundtrip_dict(self) -> None:
        snapshot = self._snapshot()
        restored = ConsentSnapshot.from_canonical_dict(snapshot.to_canonical_dict())
        assert restored == snapshot
        assert restored.canonical_hash == snapshot.canonical_hash

    def test_roundtrip_rejects_unknown_keys(self) -> None:
        data = self._snapshot().to_canonical_dict()
        data["bogus_key"] = "x"
        with pytest.raises(ValueError):
            ConsentSnapshot.from_canonical_dict(data)


class TestConsentOffer:
    def test_valid_offer(self) -> None:
        offer = base_offer()
        assert offer.capability == "chat"
        assert offer.version == 1
        assert offer.status == "active"
        assert len(offer.canonical_hash) == 64

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError):
            base_offer(created_at=datetime(2026, 8, 9))
        with pytest.raises(ValueError):
            base_offer(valid_from=datetime(2026, 8, 9))

    def test_valid_until_after_valid_from(self) -> None:
        with pytest.raises(ValueError):
            base_offer(
                valid_from=utc("2026-08-10T00:00:00+00:00"),
                valid_until=utc("2026-08-09T00:00:00+00:00"),
            )

    def test_invalid_capability(self) -> None:
        with pytest.raises(ValueError):
            base_offer(capability="bogus")  # type: ignore[arg-type]

    def test_version_and_status_validate(self) -> None:
        with pytest.raises(ValueError):
            base_offer(version=0)
        with pytest.raises(ValueError):
            base_offer(status="pending")

    def test_canonical_hash_detects_offer_tampering(self) -> None:
        original = base_offer()
        with pytest.raises(ValueError, match="canonical_hash"):
            base_offer(actor_id="person_attacker", canonical_hash=original.canonical_hash)

    def test_offer_roundtrip_and_unknown_key_rejection(self) -> None:
        offer = base_offer()
        restored = ConsentOffer.from_canonical_dict(offer.to_canonical_dict())
        assert restored == offer
        data = offer.to_canonical_dict()
        data["unknown"] = "x"
        with pytest.raises(ValueError, match="unknown offer keys"):
            ConsentOffer.from_canonical_dict(data)

    def test_purpose_required(self) -> None:
        with pytest.raises(ValueError):
            base_offer(purpose="")

    def test_timedelta_window(self) -> None:
        # A very short but valid window is allowed; ordering is what matters.
        offer = base_offer(
            valid_from=utc("2026-08-09T00:00:00+00:00"),
            valid_until=utc("2026-08-09T00:00:01+00:00"),
        )
        assert offer.valid_until > offer.valid_from

    def test_offer_canonical_content(self) -> None:
        a = base_offer()
        b = base_offer()
        assert a.canonical_content() == b.canonical_content()
        c = base_offer(capability="memory_capture", purpose="memory_capture")
        assert a.canonical_content() != c.canonical_content()
