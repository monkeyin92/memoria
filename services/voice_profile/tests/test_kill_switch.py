from __future__ import annotations

import importlib
import inspect

import pytest
from services.archive.object_store import ObjectRef
from services.voice_profile.clone_authority import (
    VoiceCloneAuthority,
    VoiceCloneDisabledError,
)
from services.voice_profile.closed_enrollment import ClosedVoiceEnrollmentProvider
from services.voice_profile.domain import ProviderVoice, VoiceEnrollmentRequest, VoiceProfilePort
from services.voice_profile.postgres_manager import PostgresVoiceProfileManager

_DSN = "postgresql://refused.invalid/voice"

CLONE_CREATING = frozenset(
    {
        "grant_consent",
        "enroll",
        "accept_enrollment",
        "complete_enrollment",
        "provider_sample",
        "reconcile_enrollment",
    }
)
CLONE_NEUTRAL = frozenset(
    {
        "revoke_consent",
        "consent",
        "evaluate",
        "create_blind_trial",
        "blind_preview_target",
        "resolve_blind_preference",
        "record_quality_measurement",
        "account_id_for_profile",
        "activate",
        "resolve",
        "profiles",
        "pending_enrollments",
        "preview_target",
        "revoke_profile",
        "confirm_provider_deletion",
    }
)


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create_voice(
        self,
        *,
        target_model: str,
        prefix: str,
        sample_url: str,
    ) -> ProviderVoice:
        self.calls.append("create")
        return ProviderVoice(voice_id="voice-1", target_model=target_model)

    async def delete_voice(self, *, voice_id: str) -> None:
        self.calls.append(f"delete:{voice_id}")


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def put(
        self,
        *,
        account_id: str,
        purpose: str,
        data: bytes,
        media_type: str,
    ) -> ObjectRef:
        self.calls.append("put")
        return ObjectRef(
            account_id=account_id,
            object_key="recorded",
            media_type=media_type,
            byte_count=len(data),
            content_sha256="a" * 64,
            encryption_key_version="k",
            backend="memory",
        )

    async def get(self, reference: ObjectRef) -> bytes:
        self.calls.append("get")
        return b""

    async def delete(self, reference: ObjectRef) -> None:
        self.calls.append("delete")


def _manager(*, enabled: bool) -> tuple[PostgresVoiceProfileManager, _RecordingProvider, _RecordingStore]:
    provider = _RecordingProvider()
    store = _RecordingStore()
    manager = PostgresVoiceProfileManager(
        _DSN,
        object_store=store,
        provider=provider,
        sample_url_factory=lambda sample_id: f"https://samples.example/{sample_id}",
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
        clone=VoiceCloneAuthority(enabled=enabled),
    )
    return manager, provider, store


def _request() -> VoiceEnrollmentRequest:
    return VoiceEnrollmentRequest(
        account_id="acct",
        audio=b"nope",
        media_type="audio/wav",
        duration_ms=12_000,
        sample_rate=16_000,
    )


@pytest.mark.hermetic
def test_every_port_method_is_creating_or_neutral() -> None:
    public = {
        name
        for name, value in VoiceProfilePort.__dict__.items()
        if inspect.isfunction(value) and not name.startswith("_")
    }
    assert public == CLONE_CREATING | CLONE_NEUTRAL
    assert CLONE_CREATING.isdisjoint(CLONE_NEUTRAL)


@pytest.mark.hermetic
async def test_disabled_clone_creating_calls_touch_nothing() -> None:
    manager, provider, store = _manager(enabled=False)
    request = _request()
    with pytest.raises(VoiceCloneDisabledError):
        await manager.grant_consent(account_id="acct", policy_version="voice-policy")
    with pytest.raises(VoiceCloneDisabledError):
        await manager.enroll(request)
    with pytest.raises(VoiceCloneDisabledError):
        await manager.accept_enrollment(request)
    with pytest.raises(VoiceCloneDisabledError):
        await manager.complete_enrollment(account_id="acct", profile_id="missing")
    with pytest.raises(VoiceCloneDisabledError):
        await manager.provider_sample(sample_id="missing")
    with pytest.raises(VoiceCloneDisabledError):
        await manager.reconcile_enrollment(
            account_id="acct",
            enrollment_key="key",
            provider_voice=ProviderVoice(voice_id="voice-1", target_model="cosyvoice-v3.5-flash"),
        )
    assert provider.calls == []
    assert store.calls == []


@pytest.mark.hermetic
async def test_enabled_grant_reaches_the_database() -> None:
    manager, provider, store = _manager(enabled=True)
    with pytest.raises(OSError):
        await manager.grant_consent(account_id="acct", policy_version="voice-policy")
    assert provider.calls == []
    assert store.calls == []


@pytest.mark.hermetic
async def test_disabled_reconcile_can_still_unwind_an_absent_sample() -> None:
    manager, provider, store = _manager(enabled=False)
    with pytest.raises(OSError):
        await manager.reconcile_enrollment(
            account_id="acct",
            enrollment_key="key",
            sample_asset_absent=True,
        )
    assert provider.calls == []
    assert store.calls == []


@pytest.mark.hermetic
def test_voice_clone_provider_modules_are_gone() -> None:
    for name in (
        "services.voice_profile.cosyvoice_enrollment",
        "services.voice_profile.doubao_voice_clone",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


@pytest.mark.hermetic
async def test_closed_provider_refuses_create_and_delete() -> None:
    closed = ClosedVoiceEnrollmentProvider()
    with pytest.raises(VoiceCloneDisabledError) as caught:
        await closed.create_voice(target_model="x", prefix="y", sample_url="z")
    assert caught.value.code == "voice_clone_disabled"
    with pytest.raises(VoiceCloneDisabledError):
        await closed.delete_voice(voice_id="voice-1")
