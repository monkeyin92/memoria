"""Session-scoped voice resolver with fail-closed runtime caching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from services.agent.src.providers.doubao_voice_catalog import (
    DOUBAO_TTS_MODEL,
    catalog_by_id,
    resolve_approved_voice,
)


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
        mode = payload.get("mode")
        if mode == "fallback":
            if any(payload.get(name) is not None for name in ("profile_id", "model", "voice_id")):
                raise ValueError("invalid fallback voice response")
            return None
        if mode == "designed":
            profile_id = payload.get("profile_id")
            model = payload.get("model")
            if payload.get("voice_id") is not None:
                raise ValueError("invalid designed voice response")
            if not all(isinstance(value, str) and value for value in (profile_id, model)):
                raise ValueError("invalid designed voice response")
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
            )
        if mode != "active":
            raise ValueError("invalid voice profile response")
        profile_id = payload.get("profile_id")
        model = payload.get("model")
        voice_id = payload.get("voice_id")
        if not all(isinstance(value, str) and value for value in (profile_id, model, voice_id)):
            raise ValueError("invalid active voice response")
        approved_profile = next(
            (spec.profile_id for spec in catalog_by_id().values() if spec.speaker_id == voice_id),
            None,
        )
        if (
            model != DOUBAO_TTS_MODEL
            or approved_profile is None
            or resolve_approved_voice(
                profile_id=approved_profile,
                model=str(model),
            )
            != voice_id
        ):
            raise ValueError("unsupported active voice model")
        return VoiceRuntimeProfile(
            profile_id=str(profile_id),
            model=str(model),
            voice_id=str(voice_id),
        )

    async def close(self) -> None:
        self._cache.clear()
