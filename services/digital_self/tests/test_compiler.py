from __future__ import annotations

import pytest
from services.digital_self.compiler import (
    build_manifest,
    canonical_manifest_bytes,
    decode_manifest,
    sha256_hex,
)
from services.digital_self.domain import (
    CognitiveClaimManifestEntry,
    DecisionCaseManifestEntry,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    RelationshipProfileManifestEntry,
    VoiceProfileManifestRef,
)

_V1_MANIFEST = b'{"compiler_version":"digital-self-compiler-v1","entries":[{"category":"life_story","claim_id":"m-1","confidence":0.9,"extractor_version":"extractor-v1","predicate":"prefers","sensitive_domain":"personal","source_event_id":"e-1","subject_key":"owner","type":"memory_claim","valid_at":"2026-07-22T08:00:00+00:00","value":"tea"},{"category":"verbal_tic","confidence":0.8,"context":"conversation","counterexample":"","description":"says hi","persona_version_id":"pv-1","source_event_ids":["e-1","e-2"],"trait_id":"p-1","type":"persona_trait"}],"parent_version_id":"parent-1","policy_version":"digital-self-policy-v1","rollback_target_version_id":null,"schema_version":"digital-self-manifest-v1","source_summary":{"memory_claim_count":1,"persona_trait_count":1,"persona_version_id":"pv-1","source_summary_sha256":"d62f04dde09c11844c7b920e1f85286fe187ffabf25b9297f3e5b5ee3fd6ccd0"}}'
_V1_MANIFEST_SHA256 = "fc680286666920fd7214f6e9247e3017174e2e98ba728ed229cfc46ec178a720"
_V1_SOURCE_SHA256 = "d62f04dde09c11844c7b920e1f85286fe187ffabf25b9297f3e5b5ee3fd6ccd0"
_V2_MANIFEST = b'{"compiler_version":"digital-self-compiler-v2","entries":[{"category":"life_story","claim_id":"m-2","confidence":0.8,"extractor_version":"extractor-v2","predicate":"prefers","sensitive_domain":"personal","source_event_id":"e-2","subject_key":"owner","type":"memory_claim","valid_at":"2026-07-23T00:00:00+00:00","value":"coffee"}],"parent_version_id":null,"policy_version":"digital-self-policy-v2","rollback_target_version_id":null,"schema_version":"digital-self-manifest-v2","source_summary":{"cognitive_claim_count":0,"decision_case_count":0,"memory_claim_count":1,"persona_trait_count":0,"persona_version_id":null,"relationship_profile_count":0,"source_summary_sha256":"fc184bedbef71e503fb235a183403ed2428af3dfe74c6ed36704b99565ce2c35"}}'
_V2_MANIFEST_SHA256 = "7e7632ff2fb04a1d77331f6cdd939ae4012bfe757a0d0901be844f3fc5792631"
_V2_SOURCE_SHA256 = "fc184bedbef71e503fb235a183403ed2428af3dfe74c6ed36704b99565ce2c35"


def test_v1_fixture_round_trips_without_changing_canonical_bytes_or_digest() -> None:
    manifest = decode_manifest(
        _V1_MANIFEST,
        expected_manifest_sha256=_V1_MANIFEST_SHA256,
        expected_source_summary_sha256=_V1_SOURCE_SHA256,
        expected_parent_version_id="parent-1",
        expected_rollback_target_version_id=None,
    )

    assert manifest.schema_version == "digital-self-manifest-v1"
    assert manifest.source_summary.cognitive_claim_count == 0
    assert manifest.source_summary.decision_case_count == 0
    assert manifest.source_summary.relationship_profile_count == 0
    assert canonical_manifest_bytes(manifest) == _V1_MANIFEST
    assert sha256_hex(canonical_manifest_bytes(manifest)) == _V1_MANIFEST_SHA256


def test_v2_fixture_round_trips_without_changing_canonical_bytes_or_digest() -> None:
    manifest = decode_manifest(
        _V2_MANIFEST,
        expected_manifest_sha256=_V2_MANIFEST_SHA256,
        expected_source_summary_sha256=_V2_SOURCE_SHA256,
        expected_parent_version_id=None,
        expected_rollback_target_version_id=None,
    )

    assert manifest.schema_version == "digital-self-manifest-v2"
    assert manifest.source_summary.voice_profile is None
    assert canonical_manifest_bytes(manifest) == _V2_MANIFEST
    assert sha256_hex(canonical_manifest_bytes(manifest)) == _V2_MANIFEST_SHA256


def test_v3_manifest_encodes_all_typed_self_model_entries_deterministically() -> None:
    entries = (
        RelationshipProfileManifestEntry(
            profile_id="relationship-profile-1",
            version_number=3,
            person_id="person-1",
            relationship_id="relationship-1",
            salutation="梅姐",
            tone="坦诚",
            advice_style="先听再建议",
            sharing_scope="family",
            boundaries=("不谈财务细节",),
            support_source_event_ids=("relationship-support",),
            counterexample_source_event_ids=(),
        ),
        DecisionCaseManifestEntry(
            case_id="decision-1",
            kind="real",
            context="是否接受异地工作",
            options=("接受", "拒绝"),
            constraints=("家庭", "职业成长"),
            chosen_option="拒绝",
            rejected_options=("接受",),
            outcome="留在本地",
            reflection="当时家庭稳定更重要",
            still_endorsed=True,
            sharing_scope="private",
            support_source_event_ids=("decision-support",),
            counterexample_source_event_ids=("decision-boundary",),
        ),
        CognitiveClaimManifestEntry(
            claim_id="cognitive-1",
            claim_type="value",
            statement="家庭安全高于短期收益",
            context="高风险决策",
            confidence=0.95,
            sharing_scope="private",
            support_source_event_ids=("claim-support",),
            counterexample_source_event_ids=("claim-boundary",),
        ),
        MemoryClaimManifestEntry(
            claim_id="memory-1",
            category="life_story",
            subject_key="owner",
            predicate="prefers",
            value="tea",
            confidence=0.9,
            sensitive_domain="personal",
            extractor_version="extractor-v1",
            source_event_id="memory-source",
            valid_at="2026-07-22T08:00:00+00:00",
        ),
        PersonaTraitManifestEntry(
            trait_id="persona-1",
            persona_version_id="persona-version-1",
            category="verbal_tic",
            description="常说先核实",
            context="conversation",
            counterexample="轻松聊天时不会",
            confidence=0.8,
            source_event_ids=("persona-source",),
        ),
    )

    first, first_bytes, first_digest = build_manifest(
        entries,
        compiler_version="digital-self-compiler-v2",
        policy_version="digital-self-policy-v2",
        persona_version_id="persona-version-1",
        parent_version_id=None,
    )
    second, second_bytes, second_digest = build_manifest(
        tuple(reversed(entries)),
        compiler_version="digital-self-compiler-v2",
        policy_version="digital-self-policy-v2",
        persona_version_id="persona-version-1",
        parent_version_id=None,
    )

    assert first.schema_version == "digital-self-manifest-v3"
    assert first == second
    assert first_bytes == second_bytes
    assert first_digest == second_digest
    assert first.source_summary.memory_claim_count == 1
    assert first.source_summary.persona_trait_count == 1
    assert first.source_summary.cognitive_claim_count == 1
    assert first.source_summary.decision_case_count == 1
    assert first.source_summary.relationship_profile_count == 1
    decoded = decode_manifest(
        first_bytes,
        expected_manifest_sha256=first_digest,
        expected_source_summary_sha256=first.source_summary.source_summary_sha256,
        expected_parent_version_id=None,
        expected_rollback_target_version_id=None,
    )
    assert decoded == first


def test_v3_digest_changes_when_immutable_voice_binding_changes() -> None:
    entry = MemoryClaimManifestEntry(
        claim_id="memory-voice",
        category="life_story",
        subject_key="owner",
        predicate="prefers",
        value="tea",
        confidence=0.9,
        sensitive_domain="personal",
        extractor_version="extractor-v1",
        source_event_id="memory-source",
        valid_at="2026-07-22T08:00:00+00:00",
    )
    first_ref = VoiceProfileManifestRef(
        profile_id="voice-profile-1",
        version_number=1,
        provider="volcengine_doubao",
        target_model="seed-icl-2.0",
        resource_id="seed-icl-2.0",
        provider_expires_at="2027-07-23T00:00:00+00:00",
        speaker_sha256="1" * 64,
    )
    second_ref = VoiceProfileManifestRef(
        profile_id="voice-profile-2",
        version_number=2,
        provider="volcengine_doubao",
        target_model="seed-icl-2.0",
        resource_id="seed-icl-2.0",
        provider_expires_at="2028-07-23T00:00:00+00:00",
        speaker_sha256="2" * 64,
    )

    first, _, first_digest = build_manifest(
        (entry,),
        compiler_version="digital-self-compiler-v3",
        policy_version="digital-self-policy-v3",
        persona_version_id=None,
        parent_version_id=None,
        voice_profile=first_ref,
    )
    second, _, second_digest = build_manifest(
        (entry,),
        compiler_version="digital-self-compiler-v3",
        policy_version="digital-self-policy-v3",
        persona_version_id=None,
        parent_version_id=None,
        voice_profile=second_ref,
    )

    assert first.source_summary.voice_profile == first_ref
    assert second.source_summary.voice_profile == second_ref
    assert first.source_summary.source_summary_sha256 != (
        second.source_summary.source_summary_sha256
    )
    assert first_digest != second_digest


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("profile_id", ""),
        ("version_number", 0),
        ("provider", "alibaba_model_studio"),
        ("target_model", "cosyvoice-v3.5-flash"),
        ("resource_id", "other-resource"),
        ("provider_expires_at", None),
        ("provider_expires_at", "2027-07-23T00:00:00"),
        ("provider_expires_at", "2027-07-23T08:00:00+08:00"),
        ("speaker_sha256", "provider-secret-id"),
    ),
)
def test_v3_voice_profile_ref_rejects_invalid_binding_fields(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "profile_id": "voice-profile-1",
        "version_number": 1,
        "provider": "volcengine_doubao",
        "target_model": "seed-icl-2.0",
        "resource_id": "seed-icl-2.0",
        "provider_expires_at": "2027-07-23T00:00:00+00:00",
        "speaker_sha256": "1" * 64,
    }
    values[field] = value

    with pytest.raises(ValueError):
        VoiceProfileManifestRef(**values)  # type: ignore[arg-type]
