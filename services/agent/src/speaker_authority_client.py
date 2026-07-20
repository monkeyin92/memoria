"""Internal client for session-scoped formal speaker classification."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, cast

import httpx

from services.speaker.domain import (
    SpeakerClassification,
    SpeakerDecision,
    permissions_for_speaker,
)


@dataclass(frozen=True, slots=True)
class SpeakerAuthorityClientConfig:
    endpoint: str
    internal_token: str
    timeout_s: float = 0.4

    def __post_init__(self) -> None:
        url = httpx.URL(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("speaker authority endpoint must be HTTP(S)")
        if not self.internal_token.strip() or self.timeout_s <= 0:
            raise ValueError("speaker authority token and timeout are required")


class SpeakerAuthorityClient:
    def __init__(
        self,
        config: SpeakerAuthorityClientConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client

    async def classify(
        self,
        *,
        session_id: str,
        pcm: bytes,
        sample_rate: int,
    ) -> SpeakerDecision:
        if not session_id.strip() or not pcm or sample_rate < 8000:
            raise ValueError("session_id and supported PCM audio are required")
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                self._config.endpoint,
                headers={"X-Memoria-Speaker-Token": self._config.internal_token},
                json={
                    "session_id": session_id,
                    "audio_base64": base64.b64encode(pcm).decode("ascii"),
                    "sample_rate": sample_rate,
                },
                timeout=self._config.timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if self._client is None:
                await client.aclose()
        return self._decision(payload)

    @staticmethod
    def _decision(payload: Any) -> SpeakerDecision:
        try:
            if not isinstance(payload, dict):
                raise TypeError
            raw_classification = payload["classification"]
            if raw_classification not in {"owner", "guest", "uncertain"}:
                raise ValueError
            classification = cast(SpeakerClassification, raw_classification)
            model_version = str(payload["model_version"])
            reason_code = str(payload["reason_code"])
            if not model_version or not reason_code:
                raise ValueError
            score_raw = payload.get("score")
            template_raw = payload.get("template_version")
            profile_raw = payload.get("profile_id")
            permissions = permissions_for_speaker(classification)
            if payload.get("permissions") != {
                "normal_conversation": permissions.normal_conversation,
                "read_private_memory": permissions.read_private_memory,
                "write_long_term_memory": permissions.write_long_term_memory,
                "sensitive_actions": permissions.sensitive_actions,
            }:
                raise ValueError
            return SpeakerDecision(
                classification=classification,
                score=float(score_raw) if score_raw is not None else None,
                quality_score=float(payload["quality_score"]),
                reason_code=reason_code,
                model_version=model_version,
                template_version=int(template_raw) if template_raw is not None else None,
                profile_id=str(profile_raw) if profile_raw is not None else None,
                permissions=permissions,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid speaker authority response") from exc
