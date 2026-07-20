from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
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
    assert enrollment.status == "shadow"
    await authority.activate(
        enrollment.profile_id,
        account_id="account-001",
        evaluation=_evaluation(),
    )

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

    before_activation = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"owner-live", sample_rate=16000)
    )
    assert before_activation.classification == "uncertain"
    assert before_activation.reason_code == "shadow_owner_candidate"
    assert before_activation.score == pytest.approx(1.0, abs=0.01)

    await authority.activate(
        first.profile_id,
        account_id="account-001",
        evaluation=_evaluation("eval-far-frr-v1"),
    )
    second = await authority.enroll(_request())
    await authority.activate(
        second.profile_id,
        account_id="account-001",
        evaluation=_evaluation("eval-far-frr-v2"),
    )

    profiles = await authority.profiles("account-001")
    assert [(item.template_version, item.status) for item in profiles] == [
        (2, "active"),
        (1, "shadow"),
    ]


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

    with pytest.raises(ValueError, match="anti-spoof assessment is unavailable"):
        await authority.activate(
            enrollment.profile_id,
            account_id="account-001",
            evaluation=_evaluation(),
        )

    shadow = await authority.classify(
        SpeakerSample(account_id="account-001", pcm=b"owner-live", sample_rate=16000)
    )
    assert shadow.classification == "uncertain"
    assert shadow.reason_code == "shadow_owner_candidate"
    assert shadow.permissions.read_private_memory is False


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
