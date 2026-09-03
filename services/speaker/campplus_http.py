"""HTTP boundary for a separately deployed CAM++/3D-Speaker embedding service."""

from __future__ import annotations

import base64
from typing import cast

import httpx

from services.speaker.domain import EmbeddingResult, RiskAssessment

_ENROLLMENT_EMBED_TIMEOUT_S = 5.0


class CampPlusHTTPEmbeddingAdapter:
    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        model_version: str,
        timeout_s: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        url = httpx.URL(endpoint)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("speaker embedding endpoint must be HTTP(S)")
        if not token.strip() or not model_version.strip() or timeout_s <= 0:
            raise ValueError("speaker embedding token, model version and timeout are required")
        self.endpoint = str(url)
        self.model_version = model_version
        self._token = token
        self._client = client
        self._timeout = httpx.Timeout(timeout_s)
        self._enrollment_timeout = httpx.Timeout(max(timeout_s, _ENROLLMENT_EMBED_TIMEOUT_S))

    async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
        return await self._embed(pcm, sample_rate=sample_rate, timeout=self._timeout)

    async def embed_enrollment(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
        """Enrollment may batch several longer clips; classify stays fail-fast."""

        return await self._embed(
            pcm,
            sample_rate=sample_rate,
            timeout=self._enrollment_timeout,
        )

    async def _embed(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
        timeout: httpx.Timeout,
    ) -> EmbeddingResult:
        if not pcm or len(pcm) > 16 * 1024 * 1024 or sample_rate < 8000:
            raise ValueError("speaker PCM must be non-empty, bounded and at least 8 kHz")
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "audio_base64": base64.b64encode(pcm).decode("ascii"),
                    "encoding": "pcm_s16le",
                    "sample_rate": sample_rate,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if self._client is None:
                await client.aclose()
        if not isinstance(payload, dict) or payload.get("model_version") != self.model_version:
            raise ValueError("speaker embedding model version does not match configuration")
        try:
            vector_raw = payload["embedding"]
            if not isinstance(vector_raw, list):
                raise TypeError
            risk_assessment_raw = payload["risk_assessment"]
            if risk_assessment_raw not in {"verified", "unavailable"}:
                raise ValueError
            return EmbeddingResult(
                vector=tuple(float(value) for value in vector_raw),
                speech_ms=int(payload["speech_ms"]),
                snr_db=float(payload["snr_db"]),
                quality_score=float(payload["quality_score"]),
                replay_risk=float(payload["replay_risk"]),
                synthetic_risk=float(payload["synthetic_risk"]),
                risk_assessment=cast(RiskAssessment, risk_assessment_raw),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid embedding response from speaker model") from exc


class UnavailableSpeakerEmbeddingAdapter:
    """Fail-closed development boundary when no formal model endpoint is configured."""

    def __init__(self, model_version: str = "unconfigured") -> None:
        self.model_version = model_version

    async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
        _ = (pcm, sample_rate)
        raise RuntimeError("speaker embedding service is not configured")
