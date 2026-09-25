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
# A v3 manifest frozen while clones were Doubao seed-icl-2.0 voices; its bytes
# are immutable, so the retired voice binding must keep decoding verbatim.
_V3_DOUBAO_VOICE_MANIFEST = b'{"compiler_version":"digital-self-compiler-v3","entries":[{"category":"life_story","claim_id":"memory-voice","confidence":0.9,"extractor_version":"extractor-v1","predicate":"prefers","sensitive_domain":"personal","source_event_id":"memory-source","subject_key":"owner","type":"memory_claim","valid_at":"2026-07-22T08:00:00+00:00","value":"tea"}],"parent_version_id":null,"policy_version":"digital-self-policy-v3","rollback_target_version_id":null,"schema_version":"digital-self-manifest-v3","source_summary":{"cognitive_claim_count":0,"decision_case_count":0,"memory_claim_count":1,"persona_trait_count":0,"persona_version_id":null,"relationship_profile_count":0,"source_summary_sha256":"72fb153a7452bc02d7f95ea6dae8fbc6a8adfbe5b7b40181336a33940058ee15","voice_profile":{"profile_id":"voice-profile-legacy","provider":"volcengine_doubao","provider_expires_at":"2027-07-23T00:00:00+00:00","resource_id":"seed-icl-2.0","speaker_sha256":"1111111111111111111111111111111111111111111111111111111111111111","target_model":"seed-icl-2.0","version_number":1}}}'
_V3_DOUBAO_VOICE_MANIFEST_SHA256 = "2e22a79c2c19fc0f4a7dfdf0cde89759d4c058e22ab684f2cbf6d07c5a51a1dc"
_V3_DOUBAO_VOICE_SOURCE_SHA256 = "72fb153a7452bc02d7f95ea6dae8fbc6a8adfbe5b7b40181336a33940058ee15"
_CURRENT_VOICE = {
    "provider": "alibaba_model_studio",
    "target_model": "qwen-audio-3.1-tts-flash",
    "resource_id": "qwen-audio-3.1-tts-flash",
}
_LEGACY_DOUBAO_VOICE = {
    "provider": "volcengine_doubao",
    "target_model": "seed-icl-2.0",
    "resource_id": "seed-icl-2.0",
}


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
        provider="alibaba_model_studio",
        target_model="qwen-audio-3.1-tts-flash",
        resource_id="qwen-audio-3.1-tts-flash",
        provider_expires_at=None,
        speaker_sha256="1" * 64,
    )
    second_ref = VoiceProfileManifestRef(
        profile_id="voice-profile-2",
        version_number=2,
        provider="alibaba_model_studio",
        target_model="qwen-audio-3.1-tts-flash",
        resource_id="qwen-audio-3.1-tts-flash",
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


def test_v3_current_voice_ref_without_expiry_round_trips() -> None:
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
    ref = VoiceProfileManifestRef(
        profile_id="voice-profile-current",
        version_number=1,
        provider_expires_at=None,
        speaker_sha256="3" * 64,
        **_CURRENT_VOICE,
    )

    manifest, manifest_bytes, digest = build_manifest(
        (entry,),
        compiler_version="digital-self-compiler-v3",
        policy_version="digital-self-policy-v3",
        persona_version_id=None,
        parent_version_id=None,
        voice_profile=ref,
    )
    decoded = decode_manifest(
        manifest_bytes,
        expected_manifest_sha256=digest,
        expected_source_summary_sha256=manifest.source_summary.source_summary_sha256,
        expected_parent_version_id=None,
        expected_rollback_target_version_id=None,
    )

    assert b'"provider_expires_at":null' in manifest_bytes
    assert decoded == manifest
    assert decoded.source_summary.voice_profile == ref


def test_v3_fixture_with_legacy_doubao_voice_ref_still_decodes_verbatim() -> None:
    manifest = decode_manifest(
        _V3_DOUBAO_VOICE_MANIFEST,
        expected_manifest_sha256=_V3_DOUBAO_VOICE_MANIFEST_SHA256,
        expected_source_summary_sha256=_V3_DOUBAO_VOICE_SOURCE_SHA256,
        expected_parent_version_id=None,
        expected_rollback_target_version_id=None,
    )

    assert manifest.schema_version == "digital-self-manifest-v3"
    assert manifest.source_summary.voice_profile == VoiceProfileManifestRef(
        profile_id="voice-profile-legacy",
        version_number=1,
        provider_expires_at="2027-07-23T00:00:00+00:00",
        speaker_sha256="1" * 64,
        **_LEGACY_DOUBAO_VOICE,
    )
    assert canonical_manifest_bytes(manifest) == _V3_DOUBAO_VOICE_MANIFEST
    assert sha256_hex(canonical_manifest_bytes(manifest)) == _V3_DOUBAO_VOICE_MANIFEST_SHA256


@pytest.mark.parametrize(
    ("identity", "field", "value"),
    (
        (_CURRENT_VOICE, "profile_id", ""),
        (_CURRENT_VOICE, "version_number", 0),
        (_CURRENT_VOICE, "provider", "volcengine_doubao"),
        (_CURRENT_VOICE, "target_model", "cosyvoice-v3.5-flash"),
        (_CURRENT_VOICE, "target_model", "seed-icl-2.0"),
        (_CURRENT_VOICE, "resource_id", "other-resource"),
        (_CURRENT_VOICE, "provider_expires_at", "2027-07-23T00:00:00"),
        (_CURRENT_VOICE, "provider_expires_at", "2027-07-23T08:00:00+08:00"),
        (_CURRENT_VOICE, "provider_expires_at", "not-a-date"),
        (_CURRENT_VOICE, "speaker_sha256", "provider-secret-id"),
        (_LEGACY_DOUBAO_VOICE, "provider", "alibaba_model_studio"),
        (_LEGACY_DOUBAO_VOICE, "target_model", "cosyvoice-v3.5-flash"),
        (_LEGACY_DOUBAO_VOICE, "target_model", "qwen-audio-3.1-tts-flash"),
        (_LEGACY_DOUBAO_VOICE, "resource_id", "other-resource"),
        (_LEGACY_DOUBAO_VOICE, "provider_expires_at", None),
        (_LEGACY_DOUBAO_VOICE, "provider_expires_at", "2027-07-23T00:00:00"),
        (_LEGACY_DOUBAO_VOICE, "provider_expires_at", "2027-07-23T08:00:00+08:00"),
        (_LEGACY_DOUBAO_VOICE, "speaker_sha256", "provider-secret-id"),
    ),
)
def test_v3_voice_profile_ref_rejects_invalid_binding_fields(
    identity: dict[str, str],
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "profile_id": "voice-profile-1",
        "version_number": 1,
        **identity,
        "provider_expires_at": "2027-07-23T00:00:00+00:00",
        "speaker_sha256": "1" * 64,
    }
    VoiceProfileManifestRef(**values)  # type: ignore[arg-type]
    values[field] = value

    with pytest.raises(ValueError):
        VoiceProfileManifestRef(**values)  # type: ignore[arg-type]
