from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from services.archive.domain import ContextQuery
from services.archive.life_archive import LifeArchive
from services.archive.object_store import EncryptedLocalObjectStore, ObjectRef
from services.voice_profile.domain import (
    EvaluationRequiredError,
    ProviderVoice,
    ProviderVoiceDeletionUnsupportedError,
    VoiceConsentRequiredError,
    VoiceEnrollmentReconciliationRequiredError,
    VoiceEnrollmentRequest,
    VoiceEvaluationRequest,
    VoiceQualityMeasurementRequest,
    objective_voice_quality_passes,
    subjective_voice_evaluation_passes,
)
from services.voice_profile.manager import VoiceProfileManager
from services.voice_profile.testing_audio import voice_sample_wav


def _sample(duration_ms: int = 12_000) -> bytes:
    """A recording that really decodes.

    Enrollment now measures the submitted audio before it stores or submits
    anything, so a placeholder byte string can no longer stand in for a
    sample: it would be rejected as ``audio_decode_failed``.
    """
    return voice_sample_wav(duration_ms)


def test_voice_profile_gates_cover_subjective_identity_and_long_sentence_stability() -> None:
    assert subjective_voice_evaluation_passes(
        candidate_preferred=True,
        similarity=4.0,
        naturalness=4.0,
        accent_similarity=4.0,
        emotion_adherence=4.0,
        instruction_adherence=4.0,
        uncanny=2.0,
    )
    assert not subjective_voice_evaluation_passes(
        candidate_preferred=True,
        similarity=4.0,
        naturalness=4.0,
        accent_similarity=3.4,
        emotion_adherence=4.0,
        instruction_adherence=4.0,
        uncanny=2.0,
    )
    assert not subjective_voice_evaluation_passes(
        candidate_preferred=None,
        similarity=4.0,
        naturalness=4.0,
        accent_similarity=4.0,
        emotion_adherence=4.0,
        instruction_adherence=4.0,
        uncanny=2.0,
    )
    assert objective_voice_quality_passes(
        first_audio_ms=700,
        cancel_tail_ms=120,
        timestamp_error_ms=90,
        long_sentence_chars=240,
        long_sentence_completion_ratio=0.99,
    )
    assert not objective_voice_quality_passes(
        first_audio_ms=700,
        cancel_tail_ms=120,
        timestamp_error_ms=90,
        long_sentence_chars=240,
        long_sentence_completion_ratio=0.97,
    )


class ProviderStub:
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []
        self.deleted: list[str] = []
        self.delete_fails = False
        self.create_entered: asyncio.Event | None = None
        self.create_release: asyncio.Event | None = None

    async def create_voice(
        self,
        *,
        target_model: str,
        prefix: str,
        sample_url: str,
    ) -> ProviderVoice:
        self.created.append(
            {
                "target_model": target_model,
                "prefix": prefix,
                "sample_url": sample_url,
            }
        )
        if self.create_entered is not None:
            self.create_entered.set()
        if self.create_release is not None:
            await self.create_release.wait()
        return ProviderVoice(
            voice_id=f"{target_model}-clone-{prefix}",
            target_model=target_model,
            expires_at=datetime.now(UTC) + timedelta(days=365),
        )

    async def delete_voice(self, *, voice_id: str) -> None:
        self.deleted.append(voice_id)
        if self.delete_fails:
            raise RuntimeError("provider unavailable")


class UnsupportedDeleteProvider(ProviderStub):
    async def delete_voice(self, *, voice_id: str) -> None:
        self.deleted.append(voice_id)
        raise ProviderVoiceDeletionUnsupportedError("manual cleanup required")


class AmbiguousPutStore:
    def __init__(self, inner: EncryptedLocalObjectStore) -> None:
        self.inner = inner
        self.reference: ObjectRef | None = None
        self.fail_after_put = True

    async def put(
        self,
        *,
        account_id: str,
        purpose: str,
        data: bytes,
        media_type: str,
    ) -> ObjectRef:
        self.reference = await self.inner.put(
            account_id=account_id,
            purpose=purpose,
            data=data,
            media_type=media_type,
        )
        if self.fail_after_put:
            raise RuntimeError("object-store acknowledgement lost")
        return self.reference

    async def get(self, reference: ObjectRef) -> bytes:
        return await self.inner.get(reference)

    async def delete(self, reference: ObjectRef) -> None:
        await self.inner.delete(reference)


def _manager(tmp_path: Path) -> tuple[VoiceProfileManager, ProviderStub, Path]:
    provider = ProviderStub()
    object_root = tmp_path / "voice-objects"
    store = EncryptedLocalObjectStore(
        root=object_root,
        key=Fernet.generate_key().decode("ascii"),
        key_version="voice-key-v1",
    )
    manager = VoiceProfileManager.sqlite(
        tmp_path / "memoria.sqlite3",
        object_store=store,
        provider=provider,
        sample_url_factory=lambda sample_id: (
            f"https://control.test/v1/voices/provider-samples/{sample_id}?token=signed"
        ),
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )
    return manager, provider, object_root


def _doubao_manager(
    tmp_path: Path,
) -> tuple[VoiceProfileManager, UnsupportedDeleteProvider]:
    provider = UnsupportedDeleteProvider()
    manager = VoiceProfileManager.sqlite(
        tmp_path / "doubao.sqlite3",
        object_store=EncryptedLocalObjectStore(
            root=tmp_path / "doubao-voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-key-v1",
        ),
        provider=provider,
        sample_url_factory=lambda sample_id: (
            f"https://control.test/v1/voices/provider-samples/{sample_id}?token=signed"
        ),
        provider_region="cn-beijing",
        target_model="seed-icl-2.0",
        provider_name="volcengine_doubao",
    )
    return manager, provider


@pytest.mark.asyncio
async def test_separate_consent_encrypts_sample_and_creates_candidate(
    tmp_path: Path,
) -> None:
    manager, provider, object_root = _manager(tmp_path)

    with pytest.raises(VoiceConsentRequiredError):
        await manager.enroll(
            VoiceEnrollmentRequest(
                account_id="voice-account",
                audio=_sample(),
                media_type="audio/wav",
                duration_ms=12_000,
                sample_rate=24_000,
            )
        )

    consent = await manager.grant_consent(
        account_id="voice-account",
        policy_version="voice-clone-v1",
    )
    candidate = await manager.enroll(
        VoiceEnrollmentRequest(
            account_id="voice-account",
            audio=_sample(),
            media_type="audio/wav",
            duration_ms=12_000,
            sample_rate=24_000,
        )
    )
    sample = await manager.provider_sample(sample_id=candidate.sample_id)
    preview = await manager.preview_target(
        account_id="voice-account",
        profile_id=candidate.profile_id,
    )

    assert consent.policy_version == "voice-clone-v1"
    assert candidate.status == "candidate"
    assert candidate.provider == "alibaba_model_studio"
    assert candidate.provider_voice_id.startswith("cosyvoice-v3.5-flash-clone-")
    assert provider.created[0]["sample_url"].endswith("?token=signed")
    assert preview.model == "cosyvoice-v3.5-flash"
    assert preview.voice_id == candidate.provider_voice_id
    assert sample.data.startswith(b"RIFF")
    ciphertexts = [path.read_bytes() for path in object_root.rglob("*.fernet")]
    assert len(ciphertexts) == 1
    assert ciphertexts[0] != _sample()

    archive = LifeArchive.sqlite(tmp_path / "memoria.sqlite3")
    evidence = await archive.context(
        ContextQuery(account_id="voice-account", speaker_class="owner", limit=20)
    )
    assert [item.event_type for item in evidence.evidence][:2] == [
        "voice_profile.enrolled",
        "voice_clone.consent_granted",
    ]


@pytest.mark.asyncio
async def test_blind_trial_keeps_mapping_server_side_and_requires_both_previews(
    tmp_path: Path,
) -> None:
    manager, _provider, _object_root = _manager(tmp_path)
    await manager.grant_consent(
        account_id="voice-account",
        policy_version="voice-clone-v1",
    )
    candidate = await manager.enroll(
        VoiceEnrollmentRequest(
            account_id="voice-account",
            audio=_sample(),
            media_type="audio/wav",
            duration_ms=12_000,
            sample_rate=24_000,
        )
    )

    trial = await manager.create_blind_trial(
        account_id="voice-account",
        profile_id=candidate.profile_id,
    )
    first = await manager.blind_preview_target(
        account_id="voice-account",
        trial_id=trial.trial_id,
        slot="A",
        text="同一句盲测文本",
    )
    with pytest.raises(EvaluationRequiredError, match="both blind trial slots"):
        await manager.resolve_blind_preference(
            account_id="voice-account",
            profile_id=candidate.profile_id,
            trial_id=trial.trial_id,
            preferred_slot="A",
        )
    second = await manager.blind_preview_target(
        account_id="voice-account",
        trial_id=trial.trial_id,
        slot="B",
        text="同一句盲测文本",
    )
    candidate_slot = "A" if first.model is not None else "B"

    assert trial.slots == ("A", "B")
    assert not hasattr(trial, "candidate_slot")
    assert (first.model is None) != (second.model is None)
    assert await manager.resolve_blind_preference(
        account_id="voice-account",
        profile_id=candidate.profile_id,
        trial_id=trial.trial_id,
        preferred_slot=candidate_slot,
    )
    assert not await manager.resolve_blind_preference(
        account_id="voice-account",
        profile_id=candidate.profile_id,
        trial_id=trial.trial_id,
        preferred_slot="B" if candidate_slot == "A" else "A",
    )

    with pytest.raises(EvaluationRequiredError, match="same text"):
        await manager.blind_preview_target(
            account_id="voice-account",
            trial_id=trial.trial_id,
            slot="B",
            text="偷换另一句文本",
        )


@pytest.mark.asyncio
async def test_activation_requires_passed_ab_evaluation_and_is_versioned(
    tmp_path: Path,
) -> None:
    manager, _provider, _object_root = _manager(tmp_path)
    await manager.grant_consent(
        account_id="voice-account",
        policy_version="voice-clone-v1",
    )
    candidate = await manager.enroll(
        VoiceEnrollmentRequest(
            account_id="voice-account",
            audio=_sample(),
            media_type="audio/wav",
            duration_ms=12_000,
            sample_rate=24_000,
        )
    )

    with pytest.raises(EvaluationRequiredError):
        await manager.activate(
            account_id="voice-account",
            profile_id=candidate.profile_id,
        )

    baseline_preferred = await manager.evaluate(
        VoiceEvaluationRequest(
            account_id="voice-account",
            profile_id=candidate.profile_id,
            similarity=5.0,
            naturalness=5.0,
            accent_similarity=5.0,
            emotion_adherence=5.0,
            instruction_adherence=5.0,
            uncanny=1.0,
            candidate_preferred=False,
        )
    )
    with pytest.raises(EvaluationRequiredError, match="candidate evaluation"):
        await manager.activate(
            account_id="voice-account",
            profile_id=candidate.profile_id,
        )

    weak_accent = await manager.evaluate(
        VoiceEvaluationRequest(
            account_id="voice-account",
            profile_id=candidate.profile_id,
            similarity=4.2,
            naturalness=4.4,
            accent_similarity=3.4,
            emotion_adherence=4.2,
            instruction_adherence=4.1,
            uncanny=1.5,
            candidate_preferred=True,
        )
    )
    evaluation = await manager.evaluate(
        VoiceEvaluationRequest(
            account_id="voice-account",
            profile_id=candidate.profile_id,
            similarity=4.2,
            naturalness=4.4,
            accent_similarity=4.0,
            emotion_adherence=4.2,
            instruction_adherence=4.1,
            uncanny=1.5,
            candidate_preferred=True,
        )
    )
    with pytest.raises(EvaluationRequiredError, match="quality measurement"):
        await manager.activate(
            account_id="voice-account",
            profile_id=candidate.profile_id,
        )
    weak_long_sentence = await manager.record_quality_measurement(
        VoiceQualityMeasurementRequest(
            account_id="voice-account",
            profile_id=candidate.profile_id,
            source_run_id="probe-run-short-coverage",
            first_audio_ms=620,
            cancel_tail_ms=120,
            timestamp_error_ms=90,
            long_sentence_chars=240,
            long_sentence_completion_ratio=0.97,
        )
    )
    with pytest.raises(EvaluationRequiredError, match="quality measurement"):
        await manager.activate(
            account_id="voice-account",
            profile_id=candidate.profile_id,
        )
    quality = await manager.record_quality_measurement(
        VoiceQualityMeasurementRequest(
            account_id="voice-account",
            profile_id=candidate.profile_id,
            source_run_id="probe-run-001",
            first_audio_ms=620,
            cancel_tail_ms=120,
            timestamp_error_ms=90,
            long_sentence_chars=240,
            long_sentence_completion_ratio=0.99,
        )
    )
    active = await manager.activate(
        account_id="voice-account",
        profile_id=candidate.profile_id,
    )
    resolution = await manager.resolve(account_id="voice-account")

    assert baseline_preferred.status == "failed"
    assert weak_accent.status == "failed"
    assert evaluation.status == "passed"
    assert weak_long_sentence.status == "failed"
    assert quality.status == "passed"
    assert active.status == "active"
    assert active.version_number == 1
    assert resolution.mode == "active"
    assert resolution.profile_id == candidate.profile_id
    assert resolution.provider == "alibaba_model_studio"
    assert resolution.voice_kind == "personal"
    assert resolution.voice_id == candidate.provider_voice_id

    await manager.evaluate(
        VoiceEvaluationRequest(
            account_id="voice-account",
            profile_id=candidate.profile_id,
            similarity=3.0,
            naturalness=4.0,
            accent_similarity=4.0,
            emotion_adherence=4.0,
            instruction_adherence=4.0,
            uncanny=2.0,
            candidate_preferred=True,
        )
    )
    assert (await manager.resolve(account_id="voice-account")).mode == "fallback"


@pytest.mark.asyncio
async def test_doubao_profile_resolves_personal_resource_and_keeps_cleanup_pending(
    tmp_path: Path,
) -> None:
    manager, provider = _doubao_manager(tmp_path)
    await manager.grant_consent(account_id="voice-account", policy_version="voice-clone-v1")
    candidate = await manager.enroll(
        VoiceEnrollmentRequest(
            account_id="voice-account",
            audio=_sample(),
            media_type="audio/wav",
            duration_ms=12_000,
            sample_rate=24_000,
        )
    )
    await manager.evaluate(
        VoiceEvaluationRequest(
            account_id="voice-account",
            profile_id=candidate.profile_id,
            similarity=4.0,
            naturalness=4.0,
            accent_similarity=4.0,
            emotion_adherence=4.0,
            instruction_adherence=4.0,
            uncanny=2.0,
            candidate_preferred=True,
        )
    )
    await manager.record_quality_measurement(
        VoiceQualityMeasurementRequest(
            account_id="voice-account",
            profile_id=candidate.profile_id,
            source_run_id="probe-run-doubao",
            first_audio_ms=700,
            cancel_tail_ms=100,
            timestamp_error_ms=100,
            long_sentence_chars=240,
            long_sentence_completion_ratio=0.99,
        )
    )
    await manager.activate(account_id="voice-account", profile_id=candidate.profile_id)

    resolution = await manager.resolve(account_id="voice-account")
    with sqlite3.connect(tmp_path / "doubao.sqlite3") as connection:
        connection.execute(
            "UPDATE voice_profiles SET provider_expires_at = NULL WHERE profile_id = ?",
            (candidate.profile_id,),
        )
    assert (await manager.resolve(account_id="voice-account")).mode == "fallback"
    assert candidate.provider_expires_at is not None
    with sqlite3.connect(tmp_path / "doubao.sqlite3") as connection:
        connection.execute(
            "UPDATE voice_profiles SET provider_expires_at = ? WHERE profile_id = ?",
            (candidate.provider_expires_at.isoformat(), candidate.profile_id),
        )
    revoked = await manager.revoke_profile(
        account_id="voice-account",
        profile_id=candidate.profile_id,
    )

    assert candidate.provider == "volcengine_doubao"
    assert resolution.provider == "volcengine_doubao"
    assert resolution.voice_kind == "personal"
    assert resolution.model == "seed-icl-2.0"
    assert resolution.resource_id == "seed-icl-2.0"
    assert revoked.status == "revoked"
    assert revoked.deletion_status == "pending"
    assert (await manager.resolve(account_id="voice-account")).mode == "fallback"
    assert provider.deleted == [candidate.provider_voice_id]

    confirmed = await manager.confirm_provider_deletion(
        account_id="voice-account",
        profile_id=candidate.profile_id,
        evidence_reference="doubao-console-ticket/cleanup-001",
    )
    assert confirmed.deletion_status == "completed"
    evidence = await LifeArchive.sqlite(tmp_path / "doubao.sqlite3").context(
        ContextQuery(account_id="voice-account", speaker_class="owner", limit=20)
    )
    confirmation = next(
        event
        for event in evidence.evidence
        if event.event_type == "voice_profile.provider_deletion_confirmed"
    )
    assert confirmation.payload["evidence_reference"] == "doubao-console-ticket/cleanup-001"
    assert "provider_voice_id" not in confirmation.payload


@pytest.mark.asyncio
async def test_doubao_activation_requires_a_known_future_provider_expiry(
    tmp_path: Path,
) -> None:
    manager, _provider = _doubao_manager(tmp_path)
    await manager.grant_consent(
        account_id="voice-expiry-account",
        policy_version="voice-clone-v1",
    )
    candidate = await manager.enroll(
        VoiceEnrollmentRequest(
            account_id="voice-expiry-account",
            audio=_sample(),
            media_type="audio/wav",
            duration_ms=12_000,
            sample_rate=24_000,
        )
    )
    with sqlite3.connect(tmp_path / "doubao.sqlite3") as connection:
        connection.execute(
            """
            UPDATE voice_profiles
            SET evaluation_status = 'passed', quality_status = 'passed',
                provider_expires_at = NULL
            WHERE profile_id = ?
            """,
            (candidate.profile_id,),
        )
    with pytest.raises(EvaluationRequiredError, match="unexpired Doubao"):
        await manager.activate(
            account_id="voice-expiry-account",
            profile_id=candidate.profile_id,
        )

    with sqlite3.connect(tmp_path / "doubao.sqlite3") as connection:
        connection.execute(
            "UPDATE voice_profiles SET provider_expires_at = ? WHERE profile_id = ?",
            (datetime(2000, 1, 1, tzinfo=UTC).isoformat(), candidate.profile_id),
        )
    with pytest.raises(EvaluationRequiredError, match="unexpired Doubao"):
        await manager.activate(
            account_id="voice-expiry-account",
            profile_id=candidate.profile_id,
        )


@pytest.mark.asyncio
async def test_consent_revocation_reports_incomplete_deletion_and_retries(
    tmp_path: Path,
) -> None:
    manager, provider, _object_root = _manager(tmp_path)
    await manager.grant_consent(
        account_id="voice-account",
        policy_version="voice-clone-v1",
    )
    candidate = await manager.enroll(
        VoiceEnrollmentRequest(
            account_id="voice-account",
            audio=_sample(),
            media_type="audio/wav",
            duration_ms=12_000,
            sample_rate=24_000,
        )
    )
    provider.delete_fails = True

    with pytest.raises(
        VoiceEnrollmentReconciliationRequiredError,
        match="voice profile deletion incomplete",
    ):
        await manager.revoke_consent(account_id="voice-account")

    revoked_consent = await manager.consent(account_id="voice-account")
    failed_profile = (await manager.profiles(account_id="voice-account"))[0]
    assert revoked_consent is not None
    assert revoked_consent.revoked_at is not None
    assert failed_profile.deletion_status == "failed"

    provider.delete_fails = False
    retried = await manager.revoke_consent(account_id="voice-account")
    completed_profile = (await manager.profiles(account_id="voice-account"))[0]

    assert retried.revoked_at == revoked_consent.revoked_at
    assert completed_profile.deletion_status == "completed"
    assert provider.deleted == [candidate.provider_voice_id, candidate.provider_voice_id]


@pytest.mark.asyncio
async def test_revoke_falls_back_before_provider_delete_and_keeps_retry_state(
    tmp_path: Path,
) -> None:
    manager, provider, _object_root = _manager(tmp_path)
    await manager.grant_consent(
        account_id="voice-account",
        policy_version="voice-clone-v1",
    )
    candidate = await manager.enroll(
        VoiceEnrollmentRequest(
            account_id="voice-account",
            audio=_sample(),
            media_type="audio/wav",
            duration_ms=12_000,
            sample_rate=24_000,
        )
    )
    await manager.evaluate(
        VoiceEvaluationRequest(
            account_id="voice-account",
            profile_id=candidate.profile_id,
            similarity=4.0,
            naturalness=4.0,
            accent_similarity=4.0,
            emotion_adherence=4.0,
            instruction_adherence=4.0,
            uncanny=2.0,
            candidate_preferred=True,
        )
    )
    await manager.record_quality_measurement(
        VoiceQualityMeasurementRequest(
            account_id="voice-account",
            profile_id=candidate.profile_id,
            source_run_id="probe-run-revoke",
            first_audio_ms=700,
            cancel_tail_ms=100,
            timestamp_error_ms=100,
            long_sentence_chars=240,
            long_sentence_completion_ratio=0.99,
        )
    )
    await manager.activate(
        account_id="voice-account",
        profile_id=candidate.profile_id,
    )
    provider.delete_fails = True

    revoked = await manager.revoke_profile(
        account_id="voice-account",
        profile_id=candidate.profile_id,
    )
    resolution = await manager.resolve(account_id="voice-account")

    assert revoked.status == "revoked"
    assert revoked.deletion_status == "failed"
    assert resolution.mode == "fallback"
    assert provider.deleted == [candidate.provider_voice_id]
    with pytest.raises(VoiceConsentRequiredError):
        await manager.provider_sample(sample_id=candidate.sample_id)


@pytest.mark.asyncio
async def test_revocation_during_enrollment_deletes_the_late_provider_asset(
    tmp_path: Path,
) -> None:
    manager, provider, _object_root = _manager(tmp_path)
    await manager.grant_consent(
        account_id="voice-account",
        policy_version="voice-clone-v1",
    )
    provider.create_entered = asyncio.Event()
    provider.create_release = asyncio.Event()
    enrollment = asyncio.create_task(
        manager.enroll(
            VoiceEnrollmentRequest(
                account_id="voice-account",
                audio=_sample(),
                media_type="audio/wav",
                duration_ms=12_000,
                sample_rate=24_000,
            )
        )
    )
    await provider.create_entered.wait()
    enrolling = (await manager.profiles(account_id="voice-account"))[0]
    await manager.revoke_profile(
        account_id="voice-account",
        profile_id=enrolling.profile_id,
    )

    provider.create_release.set()
    with pytest.raises(VoiceConsentRequiredError):
        await enrollment

    assert provider.deleted == [
        f"cosyvoice-v3.5-flash-clone-m{enrolling.profile_id.replace('-', '')[:9]}"
    ]


@pytest.mark.asyncio
async def test_retry_after_candidate_persistence_failure_reuses_provider_result(
    tmp_path: Path,
) -> None:
    manager, provider, _object_root = _manager(tmp_path)
    await manager.grant_consent(
        account_id="voice-account",
        policy_version="voice-clone-v1",
    )
    request = VoiceEnrollmentRequest(
        account_id="voice-account",
        audio=voice_sample_wav(12_400),
        media_type="audio/wav",
        duration_ms=12_000,
        sample_rate=24_000,
        enrollment_key="stable-enrollment-001",
    )
    with sqlite3.connect(tmp_path / "memoria.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TRIGGER fail_candidate_persistence
            BEFORE UPDATE OF provider_voice_id ON voice_profiles
            WHEN NEW.provider_voice_id IS NOT NULL
            BEGIN
                SELECT RAISE(FAIL, 'simulated candidate persistence failure');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="candidate persistence"):
        await manager.enroll(request)

    with sqlite3.connect(tmp_path / "memoria.sqlite3") as connection:
        connection.execute("DROP TRIGGER fail_candidate_persistence")
    candidate = await manager.enroll(request)

    assert candidate.status == "candidate"
    assert len(provider.created) == 1
    assert len(await manager.profiles(account_id="voice-account")) == 1


@pytest.mark.asyncio
async def test_ambiguous_provider_creation_is_enumerable_and_never_reissued(
    tmp_path: Path,
) -> None:
    manager, provider, _object_root = _manager(tmp_path)
    await manager.grant_consent(
        account_id="voice-account",
        policy_version="voice-clone-v1",
    )
    request = VoiceEnrollmentRequest(
        account_id="voice-account",
        audio=voice_sample_wav(12_800),
        media_type="audio/wav",
        duration_ms=12_000,
        sample_rate=24_000,
        enrollment_key="stable-enrollment-ambiguous",
    )
    with sqlite3.connect(tmp_path / "memoria.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TRIGGER fail_provider_result_persistence
            BEFORE UPDATE OF provider_voice_id ON voice_enrollment_operations
            WHEN NEW.provider_voice_id IS NOT NULL
            BEGIN
                SELECT RAISE(FAIL, 'simulated provider result persistence failure');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="provider result persistence"):
        await manager.enroll(request)
    with sqlite3.connect(tmp_path / "memoria.sqlite3") as connection:
        connection.execute("DROP TRIGGER fail_provider_result_persistence")

    with pytest.raises(VoiceEnrollmentReconciliationRequiredError):
        await manager.enroll(request)
    pending = await manager.pending_enrollments(account_id="voice-account")
    profiles = await manager.profiles(account_id="voice-account")

    assert len(provider.created) == 1
    assert len(pending) == 1
    assert pending[0].enrollment_key == "stable-enrollment-ambiguous"
    assert pending[0].provider_prefix == provider.created[0]["prefix"]
    assert profiles[0].status == "enrolling"

    recovered = ProviderVoice(
        voice_id=f"cosyvoice-v3.5-flash-clone-{pending[0].provider_prefix}",
        target_model="cosyvoice-v3.5-flash",
    )
    candidate = await manager.reconcile_enrollment(
        account_id="voice-account",
        enrollment_key=request.enrollment_key or "",
        provider_voice=recovered,
    )

    assert candidate.status == "candidate"
    assert candidate.provider_voice_id == recovered.voice_id
    assert await manager.pending_enrollments(account_id="voice-account") == ()


@pytest.mark.asyncio
async def test_ambiguous_sample_upload_is_enumerable_and_blocks_false_deletion(
    tmp_path: Path,
) -> None:
    provider = ProviderStub()
    object_root = tmp_path / "voice-objects"
    store = AmbiguousPutStore(
        EncryptedLocalObjectStore(
            root=object_root,
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-key-v1",
        )
    )
    manager = VoiceProfileManager.sqlite(
        tmp_path / "memoria.sqlite3",
        object_store=store,
        provider=provider,
        sample_url_factory=lambda sample_id: (
            f"https://control.test/v1/voices/provider-samples/{sample_id}?token=signed"
        ),
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )
    await manager.grant_consent(
        account_id="voice-account",
        policy_version="voice-clone-v1",
    )
    request = VoiceEnrollmentRequest(
        account_id="voice-account",
        audio=voice_sample_wav(13_200),
        media_type="audio/wav",
        duration_ms=12_000,
        sample_rate=24_000,
        enrollment_key="stable-upload-ambiguous",
    )

    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        await manager.enroll(request)

    pending = await manager.pending_enrollments(account_id="voice-account")
    profiles = await manager.profiles(account_id="voice-account")
    deletion = await manager.revoke_profile(
        account_id="voice-account",
        profile_id=profiles[0].profile_id,
    )

    assert pending[0].sample_purpose in (store.reference.object_key if store.reference else "")
    assert deletion.deletion_status == "failed"
    assert provider.created == []

    assert store.reference is not None
    await store.delete(store.reference)
    await manager.reconcile_enrollment(
        account_id="voice-account",
        enrollment_key="stable-upload-ambiguous",
        sample_asset_absent=True,
    )
    store.fail_after_put = False
    candidate = await manager.enroll(request)

    assert candidate.status == "candidate"
    assert len(provider.created) == 1


@pytest.mark.asyncio
async def test_consent_revocation_waits_for_orphan_sample_reconciliation(
    tmp_path: Path,
) -> None:
    provider = ProviderStub()
    store = AmbiguousPutStore(
        EncryptedLocalObjectStore(
            root=tmp_path / "voice-objects",
            key=Fernet.generate_key().decode("ascii"),
            key_version="voice-key-v1",
        )
    )
    manager = VoiceProfileManager.sqlite(
        tmp_path / "memoria.sqlite3",
        object_store=store,
        provider=provider,
        sample_url_factory=lambda sample_id: f"https://control.test/{sample_id}",
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )
    await manager.grant_consent(
        account_id="voice-account",
        policy_version="voice-clone-v1",
    )
    request = VoiceEnrollmentRequest(
        account_id="voice-account",
        audio=voice_sample_wav(13_600),
        media_type="audio/wav",
        duration_ms=12_000,
        sample_rate=24_000,
        enrollment_key="orphan-upload-consent-revoke",
    )
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        await manager.enroll(request)

    with pytest.raises(
        VoiceEnrollmentReconciliationRequiredError,
        match="voice profile deletion incomplete",
    ):
        await manager.revoke_consent(account_id="voice-account")

    revoked_consent = await manager.consent(account_id="voice-account")
    assert revoked_consent is not None
    assert revoked_consent.revoked_at is not None
    assert (await manager.profiles(account_id="voice-account"))[0].deletion_status == "failed"

    assert store.reference is not None
    await store.delete(store.reference)
    await manager.reconcile_enrollment(
        account_id="voice-account",
        enrollment_key=request.enrollment_key or "",
        sample_asset_absent=True,
    )
    retried = await manager.revoke_consent(account_id="voice-account")

    assert retried.revoked_at == revoked_consent.revoked_at
    assert await manager.pending_enrollments(account_id="voice-account") == ()
    assert await manager.profiles(account_id="voice-account") == ()
