"""Internal client for session-scoped formal speaker classification."""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import httpx

from services.speaker.domain import (
    SpeakerClassification,
    SpeakerDecision,
    permissions_for_speaker,
)

logger = logging.getLogger(__name__)

_POLICY_DENIAL_CODES = frozenset(
    {
        "minor_forbidden",
        "subject_capability_forbidden",
        "subject_category_unavailable",
    }
)


@dataclass(frozen=True, slots=True)
class SpeakerAuthorityClientConfig:
    endpoint: str
    internal_token: str
    timeout_s: float = 0.4
    enrollment_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        url = httpx.URL(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("speaker authority endpoint must be HTTP(S)")
        if (
            not self.internal_token.strip()
            or self.timeout_s <= 0
            or self.enrollment_timeout_s <= 0
        ):
            raise ValueError("speaker authority token and timeout are required")


@dataclass(frozen=True, slots=True)
class SpeakerEnrollmentSample:
    """One device-captured PCM segment; account identity stays server-side."""

    pcm: bytes
    sample_rate: int
    device: str = "esp32"
    scene: str = "device_voiceprint_setup"

    def __post_init__(self) -> None:
        if not self.pcm or len(self.pcm) % 2 or self.sample_rate < 8000:
            raise ValueError("speaker enrollment sample must contain supported PCM")


class SpeakerAuthorityClient:
    def __init__(
        self,
        config: SpeakerAuthorityClientConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self.reject_non_owner_voice = True

    async def classify(
        self,
        *,
        session_id: str,
        pcm: bytes,
        sample_rate: int,
    ) -> SpeakerDecision:
        if not session_id.strip() or not pcm or sample_rate < 8000:
            raise ValueError("session_id and supported PCM audio are required")
        # Every request starts fail-closed; only an explicit JSON boolean may relax it.
        self.reject_non_owner_voice = True
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
            policy_code = self._policy_denial_code(response)
            if policy_code is not None:
                logger.info(
                    "speaker authority policy denied code=%s session_id=%s",
                    policy_code,
                    session_id,
                )
                return self._policy_denied_decision()
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and isinstance(
                payload.get("reject_non_owner_voice"), bool
            ):
                requested_policy = payload["reject_non_owner_voice"]
            else:
                requested_policy = True
        finally:
            if self._client is None:
                await client.aclose()
        decision = self._decision(payload)
        # Apply the relaxation only after the complete trusted response passes
        # validation. A malformed authority result must leave the policy strict.
        self.reject_non_owner_voice = requested_policy
        return decision

    async def enroll(
        self,
        *,
        session_id: str,
        intent_id: str,
        samples: Sequence[SpeakerEnrollmentSample],
    ) -> dict[str, Any]:
        """Submit device samples through the session-scoped internal route.

        The request deliberately contains no account_id.  Control API resolves
        the account from the active voice session before applying the same
        adult/verified subject gate as the public enrollment route.
        """

        if not session_id.strip() or not intent_id.strip() or not 3 <= len(samples) <= 10:
            raise ValueError("speaker enrollment requires session, intent and 3 to 10 samples")
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                self._enrollment_endpoint(),
                headers={"X-Memoria-Speaker-Token": self._config.internal_token},
                json={
                    "session_id": session_id,
                    "intent_id": intent_id,
                    "samples": [
                        {
                            "audio_base64": base64.b64encode(sample.pcm).decode("ascii"),
                            "sample_rate": sample.sample_rate,
                            "device": sample.device,
                            "scene": sample.scene,
                        }
                        for sample in samples
                    ],
                },
                timeout=self._config.enrollment_timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("profile_id"), str):
                raise ValueError("invalid speaker enrollment response")
            return payload
        finally:
            if self._client is None:
                await client.aclose()

    async def enrollment_status(self, *, session_id: str) -> dict[str, Any]:
        """Read whether this active session needs device-side enrollment."""

        if not session_id.strip():
            raise ValueError("session_id is required")
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.get(
                self._status_endpoint(),
                params={"session_id": session_id},
                headers={"X-Memoria-Speaker-Token": self._config.internal_token},
                timeout=self._config.timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("enrollment"), dict):
                raise ValueError("invalid speaker enrollment status response")
            return payload
        finally:
            if self._client is None:
                await client.aclose()

    def _enrollment_endpoint(self) -> str:
        endpoint = self._config.endpoint.rstrip("/")
        if endpoint.endswith("/classify"):
            return f"{endpoint[:-len('/classify')]}/enrollments/internal"
        return f"{endpoint}/enrollments/internal"

    def _status_endpoint(self) -> str:
        endpoint = self._config.endpoint.rstrip("/")
        if endpoint.endswith("/classify"):
            return f"{endpoint[:-len('/classify')]}/status/internal"
        return f"{endpoint}/status/internal"

    @staticmethod
    def _policy_denial_code(response: httpx.Response) -> str | None:
        if response.status_code != 403:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        detail = payload.get("detail")
        if not isinstance(detail, dict):
            return None
        code = detail.get("code")
        return code if isinstance(code, str) and code in _POLICY_DENIAL_CODES else None

    @staticmethod
    def _policy_denied_decision() -> SpeakerDecision:
        return SpeakerDecision(
            classification="uncertain",
            score=None,
            quality_score=0.0,
            reason_code="authority_policy_denied",
            model_version="unavailable",
            template_version=None,
            profile_id=None,
            permissions=permissions_for_speaker("uncertain"),
        )

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
