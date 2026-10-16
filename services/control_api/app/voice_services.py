from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import asyncpg

from services.archive.object_store import (
    EncryptedLocalObjectStore,
    EncryptedS3ObjectStore,
    ObjectStore,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.unconfigured_store import UnconfiguredStore
from services.voice_profile.clone_authority import VoiceCloneAuthority
from services.voice_profile.closed_enrollment import ClosedVoiceEnrollmentProvider
from services.voice_profile.domain import VoiceProfilePort
from services.voice_profile.postgres_manager import PostgresVoiceProfileManager
from services.voice_profile.sample_url import VoiceSampleURLSigner


def voice_profile_services(
    settings: ControlSettings, *, archive_pool: asyncpg.Pool | None = None
) -> tuple[VoiceProfilePort, VoiceSampleURLSigner, ObjectStore]:
    configured_key = settings.voice_sample_encryption_key.get_secret_value()
    object_key = configured_key or base64.urlsafe_b64encode(
        hashlib.sha256(b"memoria-development-voice-sample-key").digest()
    ).decode("ascii")
    object_store: ObjectStore
    if settings.voice_object_bucket:
        object_store = EncryptedS3ObjectStore.from_boto3(
            bucket=settings.voice_object_bucket,
            key=object_key,
            key_version=settings.voice_sample_key_version,
            read_keys=settings.voice_sample_read_key_map(),
            endpoint_url=settings.voice_object_endpoint or None,
            region_name=settings.voice_object_region or None,
            access_key_id=(settings.voice_object_access_key.get_secret_value().strip() or None),
            secret_access_key=(settings.voice_object_secret_key.get_secret_value().strip() or None),
            prefix=settings.voice_object_prefix,
        )
    else:
        object_store = EncryptedLocalObjectStore(
            root=Path(settings.voice_sample_store_path),
            key=object_key,
            key_version=settings.voice_sample_key_version,
            read_keys=settings.voice_sample_read_key_map(),
        )
    configured_signer = settings.voice_sample_url_secret.get_secret_value()
    signer_secret = (
        configured_signer or hashlib.sha256(b"memoria-development-voice-sample-url").hexdigest()
    )
    signer = VoiceSampleURLSigner(
        secret=signer_secret,
        public_base_url=settings.public_base_url,
        ttl_s=settings.voice_sample_url_ttl_s,
    )
    archive_url = settings.archive_database_url.get_secret_value()
    manager: VoiceProfilePort
    if archive_url:
        manager = PostgresVoiceProfileManager(
            archive_url,
            object_store=object_store,
            provider=ClosedVoiceEnrollmentProvider(),
            sample_url_factory=signer.url,
            provider_region=settings.voice_provider_region,
            target_model="seed-tts-2.0",
            provider_name="preset",
            clone=VoiceCloneAuthority(enabled=False),
            pool=archive_pool,
        )
    else:
        manager = UnconfiguredStore("voice_profile", "MEMORIA_ARCHIVE_DATABASE_URL")
    return manager, signer, object_store
