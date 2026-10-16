"""Enrollment provider that never calls a voice-clone API."""

from __future__ import annotations

from services.voice_profile.clone_authority import VoiceCloneDisabledError
from services.voice_profile.domain import ProviderVoice


class ClosedVoiceEnrollmentProvider:
    async def create_voice(
        self,
        *,
        target_model: str,
        prefix: str,
        sample_url: str,
    ) -> ProviderVoice:
        del target_model, prefix, sample_url
        raise VoiceCloneDisabledError

    async def delete_voice(self, *, voice_id: str) -> None:
        del voice_id
        raise VoiceCloneDisabledError
