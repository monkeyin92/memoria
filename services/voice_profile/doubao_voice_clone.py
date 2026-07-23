"""Doubao Voice Clone 2.0 enrollment adapter."""

from __future__ import annotations

import asyncio
import base64
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import httpx

from services.voice_profile.domain import (
    ProviderVoice,
    ProviderVoiceDeletionUnsupportedError,
)

DOUBAO_VOICE_CLONE_MODEL = "seed-icl-2.0"
_MAX_SAMPLE_BYTES = 10 * 1024 * 1024
_MEDIA_FORMATS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
}
_SUCCESS_STATUSES = {2, 4}


@dataclass(frozen=True, slots=True)
class DoubaoVoiceCloneConfig:
    endpoint: str
    query_endpoint: str
    api_key: str
    timeout_s: float = 120.0
    poll_interval_s: float = 1.0
    synth_ready_id_mode: Literal["unverified", "custom_speaker_id", "response_field"] = (
        "unverified"
    )
    synth_ready_id_field: str = ""
    expires_at_field: str = ""
    expires_at_format: Literal["epoch_ms", "rfc3339"] = "epoch_ms"

    def __post_init__(self) -> None:
        for label, endpoint in (
            ("Doubao voice clone endpoint", self.endpoint),
            ("Doubao voice status endpoint", self.query_endpoint),
        ):
            url = httpx.URL(endpoint)
            if url.scheme != "https" or not url.host:
                raise ValueError(f"{label} must use HTTPS")
        if not self.api_key.strip() or self.timeout_s <= 0 or self.poll_interval_s <= 0:
            raise ValueError("Doubao voice clone key, timeout and poll interval are required")
        if self.synth_ready_id_mode == "response_field" and not self.synth_ready_id_field.strip():
            raise ValueError("Doubao response-field synth ID mode requires a field path")
        if self.synth_ready_id_mode != "response_field" and self.synth_ready_id_field.strip():
            raise ValueError("Doubao synth ID field path requires response-field mode")
        if not self.expires_at_field.strip():
            raise ValueError("Doubao provider expiry field path is required")


class DoubaoVoiceCloneClient:
    def __init__(
        self,
        config: DoubaoVoiceCloneConfig,
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
        if target_model != DOUBAO_VOICE_CLONE_MODEL:
            raise ValueError("Doubao voice clone target must be seed-icl-2.0")
        if not 8 <= len(prefix) <= 256 or not prefix[0].isalpha() or not all(
            char.isascii() and (char.isalnum() or char in "-_") for char in prefix
        ):
            raise ValueError("Doubao custom speaker ID must be 8..256 ASCII letters, digits, - or _")
        sample, media_format = await self._sample(sample_url)
        await self._post(
            self._config.endpoint,
            {
                "speaker_id": "custom_speaker_id",
                "custom_speaker_id": prefix,
                "model_type": 5,
                "audio": {
                    "data": base64.b64encode(sample).decode("ascii"),
                    "format": media_format,
                },
                "language": 1,
            },
        )
        status_payload = await self._poll(prefix)
        status = status_payload.get("status")
        if status == 3:
            raise RuntimeError("Doubao voice clone training failed")
        return ProviderVoice(
            voice_id=self._synth_ready_id(status_payload, prefix),
            target_model=target_model,
            expires_at=self._expires_at(status_payload),
        )

    async def delete_voice(self, *, voice_id: str) -> None:
        if not voice_id.strip():
            raise ValueError("voice_id must not be blank")
        raise ProviderVoiceDeletionUnsupportedError(
            "Doubao Voice Clone 2.0 provider deletion requires manual reconciliation"
        )

    async def _sample(self, sample_url: str) -> tuple[bytes, str]:
        url = httpx.URL(sample_url)
        if url.scheme != "https" or not url.host:
            raise ValueError("Doubao voice clone sample URL must use HTTPS")
        client = self._client or httpx.AsyncClient(timeout=self._config.timeout_s)
        try:
            async with client.stream(
                "GET",
                sample_url,
                headers={"Accept": ", ".join(_MEDIA_FORMATS)},
                follow_redirects=False,
                timeout=self._config.timeout_s,
            ) as response:
                response.raise_for_status()
                media_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                media_format = _MEDIA_FORMATS.get(media_type)
                if media_format is None:
                    raise ValueError("Doubao voice clone sample has an unsupported media type")
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > _MAX_SAMPLE_BYTES:
                    raise ValueError("Doubao voice clone sample must not exceed 10 MiB")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_SAMPLE_BYTES:
                        raise ValueError("Doubao voice clone sample must not exceed 10 MiB")
                    chunks.append(chunk)
                if not total:
                    raise ValueError("Doubao voice clone sample must not be empty")
                return b"".join(chunks), media_format
        finally:
            if self._client is None:
                await client.aclose()

    async def _poll(self, prefix: str) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self._config.timeout_s
        while True:
            payload = self._payload(
                await self._post(
                    self._config.query_endpoint,
                    {
                        "speaker_id": "custom_speaker_id",
                        "custom_speaker_id": prefix,
                        "model_type": 5,
                    },
                )
            )
            status = payload.get("status")
            if not isinstance(status, int):
                raise RuntimeError("Doubao voice status response is missing status")
            if status in _SUCCESS_STATUSES or status == 3:
                return payload
            if status not in {0, 1}:
                raise RuntimeError("Doubao voice status response has an unknown status")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Doubao voice clone training timed out")
            await asyncio.sleep(min(self._config.poll_interval_s, deadline - asyncio.get_running_loop().time()))

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> Any:
        client = self._client or httpx.AsyncClient(timeout=self._config.timeout_s)
        try:
            response = await client.post(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "X-Api-Key": self._config.api_key,
                    "X-Api-Request-Id": str(uuid.uuid4()),
                },
                json=payload,
                timeout=self._config.timeout_s,
            )
            response.raise_for_status()
            return response.json()
        finally:
            if self._client is None:
                await client.aclose()

    def _synth_ready_id(self, payload: dict[str, Any], custom_speaker_id: str) -> str:
        if self._config.synth_ready_id_mode == "unverified":
            raise RuntimeError(
                "Doubao synth-ready speaker ID is unverified; configure a smoke-verified mapping"
            )
        if self._config.synth_ready_id_mode == "custom_speaker_id":
            return custom_speaker_id
        value = self._field(payload, self._config.synth_ready_id_field)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("Doubao synth-ready ID response field is missing")
        return value

    def _expires_at(self, payload: dict[str, Any]) -> datetime:
        value = self._field(payload, self._config.expires_at_field)
        try:
            if self._config.expires_at_format == "epoch_ms":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError
                expires_at = datetime.fromtimestamp(value / 1000, tz=UTC)
            else:
                if not isinstance(value, str) or not value.strip():
                    raise TypeError
                normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
                expires_at = datetime.fromisoformat(normalized)
                if expires_at.tzinfo is None:
                    raise ValueError
                expires_at = expires_at.astimezone(UTC)
        except (OverflowError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError("Doubao provider expiry response field is invalid") from exc
        if expires_at <= datetime.now(UTC):
            raise RuntimeError("Doubao provider voice is already expired")
        return expires_at

    @staticmethod
    def _field(payload: dict[str, Any], field_path: str) -> Any:
        value: Any = payload
        for segment in field_path.split("."):
            if not isinstance(value, dict) or not segment or segment not in value:
                raise RuntimeError("Doubao provider response field is missing")
            value = value[segment]
        return value

    @staticmethod
    def _payload(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError("invalid Doubao voice clone response")
        return cast(dict[str, Any], payload)

    async def close(self) -> None:
        return None
