"""Session-scoped voice resolver with fail-closed runtime caching."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from services.agent.src.providers.doubao_voice_catalog import (
    DOUBAO_TTS_MODEL,
    catalog_by_id,
    resolve_approved_voice,
)

DOUBAO_PERSONAL_VOICE_MODEL = "seed-icl-2.0"
DOUBAO_PROVIDER = "volcengine_doubao"


@dataclass(frozen=True, slots=True)
class VoiceProfileClientConfig:
    endpoint: str
    internal_token: str
    timeout_s: float = 0.3

    def __post_init__(self) -> None:
        url = httpx.URL(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("voice profile endpoint must be HTTP(S)")
        if not self.internal_token.strip() or self.timeout_s <= 0:
            raise ValueError("voice profile token and timeout are required")


@dataclass(frozen=True, slots=True)
class VoiceRuntimeProfile:
    profile_id: str
    model: str
    voice_id: str
    provider: str = DOUBAO_PROVIDER
    voice_kind: Literal["designed", "personal"] = "designed"
    resource_id: str = DOUBAO_TTS_MODEL
    speaker_sha256: str | None = None


class VoiceProfileClient:
    def __init__(
        self,
        config: VoiceProfileClientConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._cache: dict[str, VoiceRuntimeProfile] = {}
        self._epochs: dict[str, int] = {}

    async def refresh(self, *, session_id: str) -> bool:
        if not session_id.strip():
            raise ValueError("voice profile refresh requires session_id")
        epoch = self._epochs.get(session_id, 0) + 1
        self._epochs[session_id] = epoch
        client = self._client or httpx.AsyncClient(timeout=self._config.timeout_s)
        try:
            response = await client.post(
                self._config.endpoint,
                headers={"X-Memoria-Internal-Token": self._config.internal_token},
                json={"session_id": session_id},
                timeout=self._config.timeout_s,
            )
            response.raise_for_status()
            profile = self._parse(response.json())
        except (httpx.HTTPError, TypeError, ValueError):
            cached = self._cache.get(session_id)
            if (
                self._epochs.get(session_id) == epoch
                and cached is not None
                and cached.voice_kind == "personal"
            ):
                self._cache.pop(session_id, None)
            return False
        finally:
            if self._client is None:
                await client.aclose()
        if self._epochs.get(session_id) != epoch:
            return False
        if profile is None:
            self._cache.pop(session_id, None)
        else:
            self._cache[session_id] = profile
        return True

    def cached(self, *, session_id: str) -> VoiceRuntimeProfile | None:
        return self._cache.get(session_id)

    @staticmethod
    def _parse(payload: Any) -> VoiceRuntimeProfile | None:
        if not isinstance(payload, dict):
            raise ValueError("invalid voice profile response")
        required_fields = {
            "mode",
            "profile_id",
            "provider",
            "voice_kind",
            "model",
            "resource_id",
            "voice_id",
            "speaker_sha256",
        }
        if not required_fields.issubset(payload):
            raise ValueError("incomplete voice profile response")
        mode = payload.get("mode")
        if mode == "fallback":
            if any(
                payload.get(name) is not None
                for name in (
                    "profile_id",
                    "provider",
                    "voice_kind",
                    "model",
                    "resource_id",
                    "voice_id",
                    "speaker_sha256",
                )
            ):
                raise ValueError("invalid fallback voice response")
            return None
        if mode == "designed":
            profile_id = payload.get("profile_id")
            provider = payload.get("provider")
            voice_kind = payload.get("voice_kind")
            model = payload.get("model")
            resource_id = payload.get("resource_id")
            if payload.get("voice_id") is not None or payload.get("speaker_sha256") is not None:
                raise ValueError("invalid designed voice response")
            if (
                provider != DOUBAO_PROVIDER
                or voice_kind != "designed"
                or model != DOUBAO_TTS_MODEL
                or resource_id != DOUBAO_TTS_MODEL
                or not isinstance(profile_id, str)
                or not profile_id.strip()
            ):
                raise ValueError("invalid designed voice response")
            profile_id = profile_id.strip()
            voice_id = resolve_approved_voice(
                profile_id=str(profile_id),
                model=str(model),
            )
            if voice_id is None:
                raise ValueError("designed voice is not approved")
            return VoiceRuntimeProfile(
                profile_id=str(profile_id),
                model=str(model),
                voice_id=voice_id,
                provider=DOUBAO_PROVIDER,
                voice_kind="designed",
                resource_id=DOUBAO_TTS_MODEL,
            )
        if mode != "active":
            raise ValueError("invalid voice profile response")
        profile_id = payload.get("profile_id")
        provider = payload.get("provider")
        voice_kind = payload.get("voice_kind")
        model = payload.get("model")
        resource_id = payload.get("resource_id")
        voice_id = payload.get("voice_id")
        speaker_sha256 = payload.get("speaker_sha256")
        if (
            provider != DOUBAO_PROVIDER
            or voice_kind != "personal"
            or model != DOUBAO_PERSONAL_VOICE_MODEL
            or resource_id != DOUBAO_PERSONAL_VOICE_MODEL
            or not all(isinstance(value, str) and value for value in (profile_id, voice_id))
            or profile_id != str(profile_id).strip()
            or voice_id != str(voice_id).strip()
            or not isinstance(speaker_sha256, str)
            or speaker_sha256 != hashlib.sha256(str(voice_id).encode()).hexdigest()
        ):
            raise ValueError("invalid active voice response")
        if any(spec.speaker_id == voice_id for spec in catalog_by_id().values()):
            raise ValueError("personal voice cannot reuse a designed catalog speaker")
        return VoiceRuntimeProfile(
            profile_id=str(profile_id),
            model=str(model),
            voice_id=str(voice_id),
            provider=DOUBAO_PROVIDER,
            voice_kind="personal",
            resource_id=str(resource_id),
            speaker_sha256=speaker_sha256,
        )

    async def close(self) -> None:
        self._cache.clear()
