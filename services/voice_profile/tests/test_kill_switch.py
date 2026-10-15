"""Clone-creating calls refuse before any database, object-store, or provider IO."""

from __future__ import annotations

import inspect
import json

import httpx
import pytest
from services.archive.object_store import ObjectRef
from services.voice_profile.clone_authority import (
    VoiceCloneAuthority,
    VoiceCloneDisabledError,
)
from services.voice_profile.cosyvoice_enrollment import (
    CosyVoiceEnrollmentClient,
    CosyVoiceEnrollmentConfig,
    UnavailableVoiceEnrollmentProvider,
)
from services.voice_profile.domain import ProviderVoice, VoiceEnrollmentRequest, VoiceProfilePort
from services.voice_profile.doubao_voice_clone import (
    DoubaoVoiceCloneClient,
    DoubaoVoiceCloneConfig,
)
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
    # Undecodable bytes. A late admission would fail sample validation instead.
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


def _http(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _cosyvoice(http: httpx.AsyncClient, *, enabled: bool) -> CosyVoiceEnrollmentClient:
    return CosyVoiceEnrollmentClient(
        CosyVoiceEnrollmentConfig(endpoint="https://dashscope.example/enroll", api_key="key"),
        client=http,
        clone=VoiceCloneAuthority(enabled=enabled),
    )


@pytest.mark.hermetic
async def test_disabled_cosyvoice_create_sends_no_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url)

    http = _http(httpx.MockTransport(handler))
    client = _cosyvoice(http, enabled=False)
    try:
        with pytest.raises(VoiceCloneDisabledError) as caught:
            await client.create_voice(
                target_model="cosyvoice-v3.5-flash",
                prefix="abc",
                sample_url="https://samples.example/a.wav",
            )
        assert caught.value.code == "voice_clone_disabled"
    finally:
        await http.aclose()


@pytest.mark.hermetic
async def test_enabled_cosyvoice_create_returns_the_provider_voice() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"output": {"voice_id": "cosy-1", "target_model": "cosyvoice-v3.5-flash"}},
        )

    http = _http(httpx.MockTransport(handler))
    client = _cosyvoice(http, enabled=True)
    try:
        voice = await client.create_voice(
            target_model="cosyvoice-v3.5-flash",
            prefix="abc",
            sample_url="https://samples.example/a.wav",
        )
        assert voice.voice_id == "cosy-1"
        assert voice.target_model == "cosyvoice-v3.5-flash"
    finally:
        await http.aclose()


@pytest.mark.hermetic
async def test_disabled_cosyvoice_delete_still_posts() -> None:
    seen: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"output": {}})

    http = _http(httpx.MockTransport(handler))
    client = _cosyvoice(http, enabled=False)
    try:
        await client.delete_voice(voice_id="voice-1")
        assert seen == [
            {
                "model": "voice-enrollment",
                "input": {"action": "delete_voice", "voice_id": "voice-1"},
            }
        ]
    finally:
        await http.aclose()


def _doubao(http: httpx.AsyncClient, *, enabled: bool) -> DoubaoVoiceCloneClient:
    return DoubaoVoiceCloneClient(
        DoubaoVoiceCloneConfig(
            endpoint="https://voice.example/clone",
            query_endpoint="https://voice.example/query",
            api_key="key",
            expires_at_field="expire_time",
        ),
        client=http,
        clone=VoiceCloneAuthority(enabled=enabled),
    )


@pytest.mark.hermetic
async def test_disabled_doubao_create_sends_no_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url)

    http = _http(httpx.MockTransport(handler))
    client = _doubao(http, enabled=False)
    try:
        with pytest.raises(VoiceCloneDisabledError) as caught:
            await client.create_voice(
                target_model="seed-icl-2.0",
                prefix="speaker01",
                sample_url="https://samples.example/a.wav",
            )
        assert caught.value.code == "voice_clone_disabled"
    finally:
        await http.aclose()


@pytest.mark.hermetic
async def test_unavailable_provider_admits_before_the_configuration_error() -> None:
    closed = UnavailableVoiceEnrollmentProvider(clone=VoiceCloneAuthority(enabled=False))
    with pytest.raises(VoiceCloneDisabledError) as caught:
        await closed.create_voice(target_model="x", prefix="y", sample_url="z")
    assert caught.value.code == "voice_clone_disabled"

    open_provider = UnavailableVoiceEnrollmentProvider(clone=VoiceCloneAuthority(enabled=True))
    with pytest.raises(RuntimeError, match="not configured"):
        await open_provider.create_voice(target_model="x", prefix="y", sample_url="z")
    with pytest.raises(RuntimeError, match="not configured"):
        await closed.delete_voice(voice_id="voice-1")
