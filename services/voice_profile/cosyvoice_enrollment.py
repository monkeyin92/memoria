"""CosyVoice voice-enrollment HTTP adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx

from services.voice_profile.domain import ProviderVoice


@dataclass(frozen=True, slots=True)
class CosyVoiceEnrollmentConfig:
    endpoint: str
    api_key: str
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        url = httpx.URL(self.endpoint)
        if url.scheme != "https" or not url.host:
            raise ValueError("CosyVoice enrollment endpoint must use HTTPS")
        if not self.api_key.strip() or self.timeout_s <= 0:
            raise ValueError("CosyVoice enrollment key and timeout are required")


class CosyVoiceEnrollmentClient:
    def __init__(
        self,
        config: CosyVoiceEnrollmentConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client

    async def create_voice(
        self,
        *,
        target_model: str,
        prefix: str,
        sample_url: str,
    ) -> ProviderVoice:
        if not target_model.startswith("cosyvoice-v3.5-"):
            raise ValueError("voice clone target must be a CosyVoice v3.5 model")
        if not prefix.isalnum() or not 1 <= len(prefix) <= 10:
            raise ValueError("voice clone prefix must contain 1..10 letters or digits")
        sample = httpx.URL(sample_url)
        if sample.scheme != "https" or not sample.host:
            raise ValueError("voice clone sample URL must use HTTPS")
        response = await self._post(
            {
                "model": "voice-enrollment",
                "input": {
                    "action": "create_voice",
                    "target_model": target_model,
                    "prefix": prefix,
                    "url": sample_url,
                    "language_hints": ["zh"],
                },
            }
        )
        output = self._output(response)
        voice_id = output.get("voice_id")
        returned_model = output.get("target_model") or target_model
        if not isinstance(voice_id, str) or not voice_id:
            raise RuntimeError("CosyVoice enrollment response is missing voice_id")
        if not isinstance(returned_model, str) or returned_model != target_model:
            raise RuntimeError("CosyVoice enrollment returned an unexpected target model")
        return ProviderVoice(voice_id=voice_id, target_model=returned_model)

    async def delete_voice(self, *, voice_id: str) -> None:
        if not voice_id.strip():
            raise ValueError("voice_id must not be blank")
        await self._post(
            {
                "model": "voice-enrollment",
                "input": {"action": "delete_voice", "voice_id": voice_id},
            }
        )

    async def _post(self, payload: dict[str, Any]) -> Any:
        client = self._client or httpx.AsyncClient(timeout=self._config.timeout_s)
        try:
            response = await client.post(
                self._config.endpoint,
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                json=payload,
                timeout=self._config.timeout_s,
            )
            response.raise_for_status()
            return response.json()
        finally:
            if self._client is None:
                await client.aclose()

    @staticmethod
    def _output(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("output"), dict):
            raise RuntimeError("invalid CosyVoice enrollment response")
        return cast(dict[str, Any], payload["output"])

    async def close(self) -> None:
        return None


class UnavailableVoiceEnrollmentProvider:
    async def create_voice(
        self,
        *,
        target_model: str,
        prefix: str,
        sample_url: str,
    ) -> ProviderVoice:
        del target_model, prefix, sample_url
        raise RuntimeError("voice enrollment provider is not configured")

    async def delete_voice(self, *, voice_id: str) -> None:
        del voice_id
        raise RuntimeError("voice enrollment provider is not configured")
