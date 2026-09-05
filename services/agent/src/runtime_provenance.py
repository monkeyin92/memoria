"""Response-provenance runtime mixin: bounded planner IDs and applied voice.

``DuplexRuntimeProvenanceMixin`` is composed into ``DuplexRuntime``; shared
state stays owned by the runtime dataclass and is only declared here for type
checking.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.generation_output_policy import (
    frozen_companion_clone_permitted,
    generation_voice_allowed,
)

if TYPE_CHECKING:
    from services.agent.src.mode_policy_client import ModePolicy

RESPONSE_PROVENANCE_MAX_BYTES = 16 * 1024
RESPONSE_PROVENANCE_MAX_FENCES = 32
_RESPONSE_PROVENANCE_FORBIDDEN_KEYS = frozenset(
    {
        "query",
        "prompt",
        "instructions",
        "content",
        "excerpt",
        "score",
        "quality_score",
        "embedding",
        "audio",
        "token",
        "cookie",
    }
)


@dataclass(frozen=True, slots=True)
class GenerationVoiceSnapshot:
    """Applied TTS identity frozen to one exact generation fence."""

    profile_id: str | None
    resource_id: str
    speaker_sha256: str
    voice_kind: Literal["designed", "personal"]


class DuplexRuntimeProvenanceMixin:
    """Freeze planner provenance and applied voice per generation fence."""

    if TYPE_CHECKING:
        # Shared state owned by the DuplexRuntime dataclass.
        session_id: str
        _response_provenance_by_fence: dict[GenerationFence, dict[str, Any]]
        _voice_snapshot_by_fence: dict[GenerationFence, GenerationVoiceSnapshot]

        # Core runtime members consumed by this mixin.
        @property
        def fence(self) -> GenerationFence: ...

        def mode_policy_for_fence(self, fence: GenerationFence) -> ModePolicy: ...

        def profile_permits(
            self,
            fence: GenerationFence,
            *,
            capability: str | None = None,
        ) -> bool: ...

    def bind_response_provenance(
        self,
        fence: GenerationFence,
        provenance: dict[str, Any],
    ) -> bool:
        """Freeze bounded planner/model/source IDs for one exact generation."""

        if not self.fence.matches(fence) or fence.session_id != self.session_id:
            return False
        try:
            encoded = json.dumps(
                provenance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError):
            return False
        if (
            not provenance
            or len(encoded) > RESPONSE_PROVENANCE_MAX_BYTES
            or self._contains_forbidden_provenance_key(provenance)
        ):
            return False
        self._response_provenance_by_fence[fence] = json.loads(encoded)
        while len(self._response_provenance_by_fence) > RESPONSE_PROVENANCE_MAX_FENCES:
            self._response_provenance_by_fence.pop(next(iter(self._response_provenance_by_fence)))
        return True

    def bind_generation_voice(
        self,
        fence: GenerationFence,
        *,
        profile_id: str | None,
        resource_id: str,
        speaker_sha256: str,
        voice_kind: Literal["designed", "personal"],
    ) -> bool:
        """Bind or update the voice actually used by this exact generation."""

        policy = self.mode_policy_for_fence(fence)
        if (
            fence.session_id != self.session_id
            or not self.fence.matches(fence)
            or not generation_voice_allowed(
                policy,
                personal_voice_permitted=self.profile_permits(
                    fence, capability="voice_clone_use"
                )
                or frozen_companion_clone_permitted(policy),
                profile_id=profile_id,
                resource_id=resource_id,
                speaker_sha256=speaker_sha256,
                voice_kind=voice_kind,
            )
        ):
            return False
        self._voice_snapshot_by_fence[fence] = GenerationVoiceSnapshot(
            profile_id=profile_id,
            resource_id=resource_id,
            speaker_sha256=speaker_sha256,
            voice_kind=voice_kind,
        )
        while len(self._voice_snapshot_by_fence) > RESPONSE_PROVENANCE_MAX_FENCES:
            self._voice_snapshot_by_fence.pop(next(iter(self._voice_snapshot_by_fence)))
        return True

    def generation_voice_for(
        self,
        fence: GenerationFence,
    ) -> GenerationVoiceSnapshot | None:
        return self._voice_snapshot_by_fence.get(fence)

    @classmethod
    def _contains_forbidden_provenance_key(cls, value: object) -> bool:
        if isinstance(value, dict):
            return any(
                str(key).lower() in _RESPONSE_PROVENANCE_FORBIDDEN_KEYS
                or cls._contains_forbidden_provenance_key(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(cls._contains_forbidden_provenance_key(item) for item in value)
        return False

    def response_provenance_for(
        self,
        fence: GenerationFence,
    ) -> dict[str, Any] | None:
        stored = self._response_provenance_by_fence.get(fence)
        if stored is None:
            return None
        provenance = cast(dict[str, Any], json.loads(json.dumps(stored)))
        voice = self.generation_voice_for(fence)
        if voice is not None:
            provenance.update(
                {
                    "tts_model": voice.resource_id,
                    "actual_voice_profile_id": voice.profile_id,
                    "actual_voice_resource_id": voice.resource_id,
                    "actual_voice_speaker_sha256": voice.speaker_sha256,
                }
            )
        return provenance
