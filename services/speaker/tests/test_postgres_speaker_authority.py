from __future__ import annotations

import os

import asyncpg
import pytest
from cryptography.fernet import Fernet
from services.speaker.domain import (
    EmbeddingResult,
    EnrollmentRequest,
    EnrollmentSample,
    RevokeSpeakerProfile,
    SpeakerEvaluation,
    SpeakerSample,
)
from services.speaker.postgres_authority import PostgresSpeakerAuthority


class FakeEmbeddingAdapter:
    model_version = "campplus-postgres-test-v1"

    async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
        _ = sample_rate
        vectors = {
            b"owner-01": (1.0, 0.0),
            b"owner-02": (0.99, 0.01),
            b"owner-03": (0.98, 0.02),
            b"owner-live": (0.97, 0.03),
            b"guest-live": (0.0, 1.0),
        }
        return EmbeddingResult(
            vector=vectors[pcm],
            speech_ms=1800,
            snr_db=20,
            quality_score=0.95,
            replay_risk=0.02,
            synthetic_risk=0.02,
        )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL speaker contract test",
)
async def test_postgres_speaker_authority_matches_the_public_contract() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    authority = PostgresSpeakerAuthority(
        dsn,
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=FakeEmbeddingAdapter(),
        owner_threshold=0.8,
        guest_threshold=0.4,
    )
    await authority.initialize()
    account_id = "postgres-speaker-account"
    connection = await asyncpg.connect(dsn)
    await connection.execute("DELETE FROM speaker_identities WHERE account_id = $1", account_id)
    await connection.close()

    enrollment = await authority.enroll(
        EnrollmentRequest(
            account_id=account_id,
            consent_grant_id="consent-postgres-speaker",
            samples=tuple(
                EnrollmentSample(pcm=value, sample_rate=16000)
                for value in (b"owner-01", b"owner-02", b"owner-03")
            ),
        )
    )
    shadow = await authority.classify(
        SpeakerSample(account_id=account_id, pcm=b"owner-live", sample_rate=16000)
    )
    await authority.activate(
        enrollment.profile_id,
        account_id=account_id,
        evaluation=SpeakerEvaluation(
            report_ref="eval-postgres-far-frr",
            sample_count=200,
            far=0.02,
            frr=0.08,
            eer=0.05,
            unknown_rejection=0.93,
            passed=True,
        ),
    )
    owner = await authority.classify(
        SpeakerSample(account_id=account_id, pcm=b"owner-live", sample_rate=16000)
    )
    guest = await authority.classify(
        SpeakerSample(account_id=account_id, pcm=b"guest-live", sample_rate=16000)
    )
    profiles = await authority.profiles(account_id)
    await authority.revoke(
        RevokeSpeakerProfile(
            account_id=account_id,
            profile_id=enrollment.profile_id,
            reason="contract cleanup",
        )
    )
    revoked = await authority.classify(
        SpeakerSample(account_id=account_id, pcm=b"owner-live", sample_rate=16000)
    )

    assert (shadow.classification, shadow.reason_code) == (
        "uncertain",
        "shadow_owner_candidate",
    )
    assert owner.classification == "owner"
    assert guest.classification == "guest"
    assert guest.permissions.read_private_memory is False
    assert [(item.template_version, item.status) for item in profiles] == [(1, "active")]
    assert (revoked.classification, revoked.reason_code) == (
        "uncertain",
        "no_active_profile",
    )
    connection = await asyncpg.connect(dsn)
    ciphertext = await connection.fetchval(
        "SELECT template_ciphertext FROM speaker_profiles WHERE profile_id = $1",
        enrollment.profile_id,
    )
    await connection.execute("DELETE FROM speaker_identities WHERE account_id = $1", account_id)
    await connection.close()
    assert ciphertext is None
    await authority.close()
