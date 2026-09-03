from __future__ import annotations

import asyncio
import sqlite3
from math import sqrt
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from services.agent.src.orchestration.utterance_router import route_target_speaker
from services.speaker.authority import SpeakerAuthority
from services.speaker.domain import (
    EmbeddingResult,
    EnrollmentQualityError,
    EnrollmentRequest,
    EnrollmentSample,
    RevokeSpeakerProfile,
    SpeakerEvaluation,
    SpeakerSample,
)


class FakeEmbeddingAdapter:
    model_version = "campplus-test-v1"

    async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
        _ = sample_rate
        vectors = {
            b"owner-1": (1.0, 0.0, 0.0),
            b"owner-2": (0.98, 0.02, 0.0),
            b"owner-3": (0.99, 0.01, 0.0),
            b"owner-live": (0.97, 0.03, 0.0),
            b"guest-live": (0.0, 1.0, 0.0),
            b"ambiguous": (0.7, 0.7, 0.0),
            b"short": (1.0, 0.0, 0.0),
            b"synthetic-live": (0.97, 0.03, 0.0),
            b"wrong-dimension": (1.0, 0.0),
        }
        return EmbeddingResult(
            vector=vectors[pcm],
            speech_ms=300 if pcm == b"short" else 2200,
            snr_db=20.0,
            quality_score=0.95,
            replay_risk=0.05,
            synthetic_risk=0.9 if pcm == b"synthetic-live" else 0.05,
        )


def _request(account_id: str = "account-001") -> EnrollmentRequest:
    return EnrollmentRequest(
        account_id=account_id,
        consent_grant_id=f"consent-{account_id}",
        samples=tuple(
            EnrollmentSample(pcm=value, sample_rate=16000)
            for value in (b"owner-1", b"owner-2", b"owner-3")
        ),
    )


def _evaluation(ref: str = "eval-authorized-001") -> SpeakerEvaluation:
    return SpeakerEvaluation(
        report_ref=ref,
        sample_count=200,
        far=0.02,
        frr=0.08,
        eer=0.05,
        unknown_rejection=0.93,
        passed=True,
    )


@pytest.mark.asyncio
async def test_three_state_classification_and_fail_closed_permissions(tmp_path: Path) -> None:
    authority = SpeakerAuthority.sqlite(
        tmp_path / "speakers.sqlite3",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=FakeEmbeddingAdapter(),
        owner_threshold=0.80,
        guest_threshold=0.40,
    )
    enrollment = await authority.enroll(_request())
    assert enrollment.status == "active"

    owner = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"owner-live", sample_rate=16000)
    )
    guest = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"guest-live", sample_rate=16000)
    )
    uncertain = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"short", sample_rate=16000)
    )
    synthetic = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"synthetic-live", sample_rate=16000)
    )
    wrong_dimension = await authority.classify(
        SpeakerSample(
            account_id="account-001",
            pcm=b"wrong-dimension",
            sample_rate=16000,
        )
    )

    assert owner.classification == "owner"
    assert owner.permissions.read_private_memory is True
    assert owner.permissions.write_long_term_memory is True
    assert owner.permissions.sensitive_actions is False
    assert guest.classification == "guest"
    assert guest.permissions.normal_conversation is True
    assert guest.permissions.read_private_memory is False
    assert guest.permissions.write_long_term_memory is False
    assert uncertain.classification == "uncertain"
    assert uncertain.reason_code == "insufficient_speech"
    assert uncertain.permissions.sensitive_actions is False
    assert (synthetic.classification, synthetic.reason_code) == (
        "uncertain",
        "synthetic_risk",
    )
    assert synthetic.permissions.read_private_memory is False
    assert (wrong_dimension.classification, wrong_dimension.reason_code) == (
        "uncertain",
        "embedding_dimension_mismatch",
    )


@pytest.mark.asyncio
async def test_shadow_profile_never_grants_owner_and_activation_is_versioned(
    tmp_path: Path,
) -> None:
    database = tmp_path / "speakers.sqlite3"
    authority = SpeakerAuthority.sqlite(
        database,
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=FakeEmbeddingAdapter(),
    )
    first = await authority.enroll(_request())
    first_owner = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"owner-live", sample_rate=16000)
    )
    assert first.status == "active"
    assert first_owner.classification == "owner"
    assert first_owner.permissions.read_private_memory is True

    second = await authority.enroll(_request())
    profiles = await authority.profiles("account-001")
    assert second.status == "active"
    assert [(item.template_version, item.status) for item in profiles] == [
        (2, "active"),
        (1, "shadow"),
    ]
    replaced = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"owner-live", sample_rate=16000)
    )
    assert replaced.classification == "owner"
    assert replaced.profile_id == second.profile_id


@pytest.mark.asyncio
async def test_enrollment_keeps_distinct_natural_voice_conditions_as_prototypes(
    tmp_path: Path,
) -> None:
    class VoiceConditionAdapter:
        model_version = "campplus-voice-conditions-v1"

        async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
            _ = sample_rate
            vectors = {
                b"natural": (1.0, 0.0, 0.0, 0.0),
                b"soft": (0.0, 1.0, 0.0, 0.0),
                b"bright": (0.0, 0.0, 1.0, 0.0),
                b"steady": (0.0, 0.0, 0.0, 1.0),
            }
            return EmbeddingResult(
                vector=vectors[pcm],
                speech_ms=2200,
                snr_db=20.0,
                quality_score=0.95,
                replay_risk=0.05,
                synthetic_risk=0.05,
            )

    authority = SpeakerAuthority.sqlite(
        tmp_path / "voice-conditions.sqlite3",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=VoiceConditionAdapter(),
        owner_threshold=0.80,
        guest_threshold=0.40,
    )
    await authority.enroll(
        EnrollmentRequest(
            account_id="account-voice-conditions",
            consent_grant_id="consent-voice-conditions",
            samples=tuple(
                EnrollmentSample(pcm=value, sample_rate=16000)
                for value in (b"natural", b"soft", b"bright", b"steady")
            ),
        )
    )

    decisions = [
        await authority.classify(
            SpeakerSample(
                account_id="account-voice-conditions",
                pcm=value,
                sample_rate=16000,
            )
        )
        for value in (b"natural", b"soft", b"bright", b"steady")
    ]

    assert [item.reason_code for item in decisions] == ["owner_match"] * 4
    assert [item.score for item in decisions] == pytest.approx([1.0] * 4)
    assert all(item.classification == "owner" for item in decisions)
    assert all(item.permissions.read_private_memory for item in decisions)


@pytest.mark.asyncio
async def test_shadow_short_sample_keeps_candidate_without_granting_authority(
    tmp_path: Path,
) -> None:
    authority = SpeakerAuthority.sqlite(
        tmp_path / "speakers.sqlite3",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=FakeEmbeddingAdapter(),
    )
    enrolled = await authority.enroll(_request())
    assert enrolled.status == "active"

    short = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"short", sample_rate=16000)
    )

    assert (short.classification, short.reason_code) == (
        "uncertain",
        "insufficient_speech",
    )
    assert short.permissions.read_private_memory is False


@pytest.mark.asyncio
async def test_shadow_runtime_guest_cutoff_preserves_owner_conversation_matrix(
    tmp_path: Path,
) -> None:
    """The hotfix only tightens shadow guest candidates; it never confirms authority."""

    class ScoreMatrixAdapter:
        model_version = "campplus-score-matrix-v1"

        async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
            _ = sample_rate
            scores = {
                b"owner-1": 1.0,
                b"owner-2": 1.0,
                b"owner-3": 1.0,
                b"owner-live-421": 0.421,
                b"owner-live-474": 0.474,
                b"owner-live-438": 0.438,
                b"owner-live-470": 0.470,
                b"son-live-neg121": -0.121,
                b"son-live-332": 0.332,
                b"son-live-495": 0.495,
                b"son-live-173": 0.173,
                b"son-live-358": 0.358,
                b"son-live-143": 0.143,
                b"son-live-229": 0.229,
            }
            score = scores[pcm]
            return EmbeddingResult(
                vector=(score, sqrt(1 - score**2), 0.0),
                speech_ms=2200,
                snr_db=20.0,
                quality_score=0.95,
                replay_risk=0.05,
                synthetic_risk=0.05,
            )

    database = tmp_path / "speakers.sqlite3"
    key = Fernet.generate_key().decode("ascii")
    stored_threshold = SpeakerAuthority.sqlite(
        database,
        template_key=key,
        adapter=ScoreMatrixAdapter(),
        owner_threshold=0.78,
        guest_threshold=0.45,
    )
    enrollment = await stored_threshold.enroll(_request())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE speaker_profiles SET status = 'shadow', activated_at = NULL "
            "WHERE profile_id = ?",
            (enrollment.profile_id,),
        )
    runtime_threshold = SpeakerAuthority.sqlite(
        database,
        template_key=key,
        adapter=ScoreMatrixAdapter(),
        owner_threshold=0.78,
        guest_threshold=0.40,
    )

    owners = [
        await runtime_threshold.classify(
            SpeakerSample(account_id="account-001", pcm=pcm, sample_rate=16000)
        )
        for pcm in (
            b"owner-live-421",
            b"owner-live-474",
            b"owner-live-438",
            b"owner-live-470",
        )
    ]
    son = [
        await runtime_threshold.classify(
            SpeakerSample(account_id="account-001", pcm=pcm, sample_rate=16000)
        )
        for pcm in (
            b"son-live-neg121",
            b"son-live-332",
            b"son-live-495",
            b"son-live-173",
            b"son-live-358",
            b"son-live-143",
            b"son-live-229",
        )
    ]

    assert [item.reason_code for item in owners] == ["shadow_ambiguous_candidate"] * 4
    assert [item.reason_code for item in son] == [
        "shadow_guest_candidate",
        "shadow_guest_candidate",
        "shadow_ambiguous_candidate",
        "shadow_guest_candidate",
        "shadow_guest_candidate",
        "shadow_guest_candidate",
        "shadow_guest_candidate",
    ]
    assert all(item.classification == "uncertain" for item in (*owners, *son))
    assert all(not item.permissions.read_private_memory for item in (*owners, *son))
    assert all(not item.permissions.write_long_term_memory for item in (*owners, *son))

    owner_routes = [
        route_target_speaker(
            classification=item.classification,
            reason_code=item.reason_code,
            profile_id=item.profile_id,
            pcm_duration_ms=2200,
        )
        for item in owners
    ]
    son_routes = [
        route_target_speaker(
            classification=item.classification,
            reason_code=item.reason_code,
            profile_id=item.profile_id,
            pcm_duration_ms=2200,
        )
        for item in son
    ]

    assert all(route.allow_input for route in owner_routes)
    assert sum(not route.allow_input for route in son_routes) == 6
    assert son_routes[2].allow_input is True
    assert son_routes[2].reason == "target_unconfirmed"

    await stored_threshold.activate(
        enrollment.profile_id,
        account_id="account-001",
        evaluation=_evaluation("stored-threshold-stays-active"),
    )
    active_owner = await runtime_threshold.classify(
        SpeakerSample(account_id="account-001", pcm=b"owner-live-421", sample_rate=16000)
    )
    assert active_owner.reason_code == "owner_mismatch"


@pytest.mark.asyncio
async def test_unavailable_anti_spoof_stays_shadow_and_cannot_activate(tmp_path: Path) -> None:
    class EmbeddingOnlyAdapter(FakeEmbeddingAdapter):
        async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
            result = await super().embed(pcm, sample_rate=sample_rate)
            return EmbeddingResult(
                vector=result.vector,
                speech_ms=result.speech_ms,
                snr_db=result.snr_db,
                quality_score=result.quality_score,
                replay_risk=1.0,
                synthetic_risk=1.0,
                risk_assessment="unavailable",
            )

    authority = SpeakerAuthority.sqlite(
        tmp_path / "speakers.sqlite3",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=EmbeddingOnlyAdapter(),
    )
    enrollment = await authority.enroll(_request())
    assert enrollment.status == "active"

    with pytest.raises(ValueError, match="anti-spoof assessment is unavailable"):
        await authority.activate(
            enrollment.profile_id,
            account_id="account-001",
            evaluation=_evaluation(),
        )

    owner = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"owner-live", sample_rate=16000)
    )
    assert owner.classification == "owner"
    assert owner.reason_code == "owner_match"
    assert owner.permissions.read_private_memory is True


@pytest.mark.asyncio
async def test_low_quality_enrollment_and_model_failure_fail_closed(tmp_path: Path) -> None:
    class UnreliableAdapter(FakeEmbeddingAdapter):
        async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
            if pcm == b"bad-quality":
                return EmbeddingResult(
                    vector=(1.0, 0.0),
                    speech_ms=2200,
                    snr_db=2.0,
                    quality_score=0.9,
                    replay_risk=0.05,
                    synthetic_risk=0.05,
                )
            if pcm == b"timeout":
                await asyncio.sleep(0.05)
            if pcm == b"unavailable":
                raise RuntimeError("model offline")
            return await super().embed(pcm, sample_rate=sample_rate)

    authority = SpeakerAuthority.sqlite(
        tmp_path / "speakers.sqlite3",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=UnreliableAdapter(),
        classify_timeout_s=0.001,
    )
    with pytest.raises(EnrollmentQualityError, match="low_snr"):
        await authority.enroll(
            EnrollmentRequest(
                account_id="account-001",
                consent_grant_id="consent-001",
                samples=tuple(
                    EnrollmentSample(pcm=value, sample_rate=16000)
                    for value in (b"owner-1", b"owner-2", b"bad-quality")
                ),
            )
        )
    enrollment = await authority.enroll(_request())
    await authority.activate(
        enrollment.profile_id,
        account_id="account-001",
        evaluation=_evaluation("eval-001"),
    )

    timed_out = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"timeout", sample_rate=16000)
    )
    unavailable = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"unavailable", sample_rate=16000)
    )

    assert (timed_out.classification, timed_out.reason_code) == (
        "uncertain",
        "model_timeout",
    )
    assert (unavailable.classification, unavailable.reason_code) == (
        "uncertain",
        "model_unavailable",
    )
    assert timed_out.permissions.read_private_memory is False
    assert unavailable.permissions.write_long_term_memory is False


@pytest.mark.asyncio
async def test_revoke_erases_template_and_is_account_scoped(tmp_path: Path) -> None:
    database = tmp_path / "speakers.sqlite3"
    authority = SpeakerAuthority.sqlite(
        database,
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=FakeEmbeddingAdapter(),
    )
    enrollment = await authority.enroll(_request())
    await authority.activate(
        enrollment.profile_id,
        account_id="account-001",
        evaluation=_evaluation("eval-001"),
    )

    with pytest.raises(LookupError):
        await authority.revoke(
            RevokeSpeakerProfile(
                account_id="account-other",
                profile_id=enrollment.profile_id,
                reason="cross-account attempt",
            )
        )
    await authority.revoke(
        RevokeSpeakerProfile(
            account_id="account-001",
            profile_id=enrollment.profile_id,
            reason="user request",
        )
    )

    after = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"owner-live", sample_rate=16000)
    )
    assert after.classification == "uncertain"
    assert after.reason_code == "no_active_profile"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status, template_ciphertext, revoke_reason FROM speaker_profiles "
            "WHERE profile_id = ?",
            (enrollment.profile_id,),
        ).fetchone()
    assert row == ("revoked", None, "user request")


def test_activation_evaluation_requires_real_sample_scale_and_passed_report() -> None:
    with pytest.raises(ValueError, match="at least 200"):
        SpeakerEvaluation(
            report_ref="eval-too-small",
            sample_count=199,
            far=0.02,
            frr=0.08,
            eer=0.05,
            unknown_rejection=0.93,
            passed=True,
        )
    with pytest.raises(ValueError, match="must pass"):
        SpeakerEvaluation(
            report_ref="eval-failed",
            sample_count=200,
            far=0.3,
            frr=0.3,
            eer=0.3,
            unknown_rejection=0.4,
            passed=False,
        )


@pytest.mark.asyncio
async def test_create_enrollment_intent_reuses_pending_intent_under_concurrency(
    tmp_path: Path,
) -> None:
    authority = SpeakerAuthority.sqlite(
        tmp_path / "speaker.sqlite",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=FakeEmbeddingAdapter(),
    )
    now = "2026-08-27T09:00:00+00:00"
    expires_at = "2026-08-27T10:00:00+00:00"

    intents = await asyncio.gather(
        *(
            authority.create_enrollment_intent(
                account_id="account-001",
                consent_policy_version="speaker-consent-v1",
                now=now,
                expires_at=expires_at,
            )
            for _ in range(8)
        )
    )

    assert len({intent.intent_id for intent in intents}) == 1
    assert all(intent.state == "requested" for intent in intents)


@pytest.mark.asyncio
async def test_create_enrollment_intent_revokes_expired_pending_before_reuse(
    tmp_path: Path,
) -> None:
    authority = SpeakerAuthority.sqlite(
        tmp_path / "speaker.sqlite",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=FakeEmbeddingAdapter(),
    )
    expired = await authority.create_enrollment_intent(
        account_id="account-001",
        consent_policy_version="speaker-consent-v1",
        now="2026-08-27T08:00:00+00:00",
        expires_at="2026-08-27T09:00:00+00:00",
    )
    fresh = await authority.create_enrollment_intent(
        account_id="account-001",
        consent_policy_version="speaker-consent-v2",
        now="2026-08-27T09:30:00+00:00",
        expires_at="2026-08-27T10:30:00+00:00",
    )

    assert fresh.intent_id != expired.intent_id
    assert fresh.consent_policy_version == "speaker-consent-v2"
    with sqlite3.connect(tmp_path / "speaker.sqlite") as connection:
        row = connection.execute(
            "SELECT state FROM speaker_enrollment_intents WHERE intent_id = ?",
            (expired.intent_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "revoked"


@pytest.mark.asyncio
async def test_enroll_consumes_intent_only_after_successful_embed(tmp_path: Path) -> None:
    class BoomAdapter(FakeEmbeddingAdapter):
        async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
            raise RuntimeError("speaker embedding exploded")

    boom = SpeakerAuthority.sqlite(
        tmp_path / "intent-boom.sqlite",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=BoomAdapter(),
    )
    intent = await boom.create_enrollment_intent(
        account_id="account-001",
        consent_policy_version="speaker-consent-v1",
        now="2026-08-27T09:00:00+00:00",
        expires_at="2026-08-28T09:00:00+00:00",
    )
    with pytest.raises(RuntimeError, match="exploded"):
        await boom.enroll(
            EnrollmentRequest(
                account_id="account-001",
                consent_grant_id="consent-boom",
                samples=_request().samples,
                intent_id=intent.intent_id,
            )
        )
    pending = await boom.pending_enrollment_intent(
        "account-001",
        now="2026-08-27T09:30:00+00:00",
    )
    assert pending is not None
    assert pending.intent_id == intent.intent_id

    authority = SpeakerAuthority.sqlite(
        tmp_path / "intent-ok.sqlite",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=FakeEmbeddingAdapter(),
    )
    ready = await authority.create_enrollment_intent(
        account_id="account-001",
        consent_policy_version="speaker-consent-v1",
        now="2026-08-27T09:00:00+00:00",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    with pytest.raises(EnrollmentQualityError, match="insufficient_speech"):
        await authority.enroll(
            EnrollmentRequest(
                account_id="account-001",
                consent_grant_id="consent-short",
                samples=tuple(
                    EnrollmentSample(pcm=b"short", sample_rate=16000) for _ in range(3)
                ),
                intent_id=ready.intent_id,
            )
        )
    still_pending = await authority.pending_enrollment_intent(
        "account-001",
        now="2026-08-27T09:30:00+00:00",
    )
    assert still_pending is not None
    assert still_pending.intent_id == ready.intent_id

    enrolled = await authority.enroll(
        EnrollmentRequest(
            account_id="account-001",
            consent_grant_id="consent-ok",
            samples=_request().samples,
            intent_id=ready.intent_id,
        )
    )
    assert enrolled.status == "active"
    assert (
        await authority.pending_enrollment_intent(
            "account-001",
            now="2026-08-27T09:30:00+00:00",
        )
        is None
    )
