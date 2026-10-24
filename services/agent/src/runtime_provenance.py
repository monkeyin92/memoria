"""Response-provenance runtime mixin: bounded planner IDs and applied voice.

``DuplexRuntimeProvenanceMixin`` is composed into ``DuplexRuntime``; shared
state stays owned by the runtime dataclass and is only declared here for type
checking.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.generation_output_policy import (
    frozen_companion_clone_permitted,
    generation_voice_allowed,
)
from services.common.tts_defaults import STEPFUN_ARCHIVE_MODELS, STEPFUN_TTS_MODEL
from services.guardian.child_memory import TRANSCRIPT_KEYS

if TYPE_CHECKING:
    from services.agent.src.generation_records import GenerationRecords
    from services.agent.src.mode_policy_client import ModePolicy

RESPONSE_PROVENANCE_MAX_BYTES = 16 * 1024
_TTS_SENTENCES_MAX = 64
# Engines control-api will store on one playout. Anything else stays unlabeled.
_ARCHIVE_TTS_ENGINES = frozenset({"stepfun", "volcengine_doubao"})
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
) | {key.lower() for key in TRANSCRIPT_KEYS}


def project_spoken_tts_provider(provenance: dict[str, Any]) -> dict[str, Any]:
    """One provider for the turn the child heard, without the sentence list.

    The sentence list is how the runtime remembers a failover. Control-api
    rejects that list, so the archive keeps the engine of the sentence that
    finished the turn.
    """

    sentences = provenance.pop("tts_sentences", None)
    chosen: dict[str, Any] | None = None
    if isinstance(sentences, list):
        for item in sentences:
            if not isinstance(item, dict):
                continue
            engine = item.get("engine")
            if engine in _ARCHIVE_TTS_ENGINES:
                chosen = item
    if chosen is not None:
        provenance["tts_provider"] = chosen["engine"]
        model = chosen.get("model")
        if isinstance(model, str) and model:
            provenance["tts_model"] = model
        return provenance
    if provenance.get("tts_provider") == "stepfun":
        model = provenance.get("tts_model")
        if not isinstance(model, str) or model not in STEPFUN_ARCHIVE_MODELS:
            provenance["tts_model"] = STEPFUN_TTS_MODEL
    return provenance


@dataclass(frozen=True, slots=True)
class GenerationVoiceSnapshot:
    """Applied TTS identity frozen to one exact generation fence."""

    profile_id: str | None
    resource_id: str
    speaker_sha256: str
    voice_kind: Literal["designed", "personal"]


class DuplexRuntimeProvenanceMixin:
    """Freeze planner provenance and applied voice per generation fence."""

    # Re-key a cached response plan when output work moves to a successor
    # generation. The reply pipeline that owns the cache installs it.
    _response_plan_rebinder: Callable[[GenerationFence, GenerationFence], None] | None = None

    if TYPE_CHECKING:
        # Shared state owned by the DuplexRuntime dataclass.
        session_id: str
        _generation_records: GenerationRecords

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

    def set_response_plan_rebinder(
        self,
        rebinder: Callable[[GenerationFence, GenerationFence], None] | None,
    ) -> None:
        self._response_plan_rebinder = rebinder

    def rebind_response_plan(
        self,
        previous: GenerationFence,
        current: GenerationFence,
    ) -> None:
        """Move this session's cached plan to the generation that will speak it."""

        if self._response_plan_rebinder is not None:
            self._response_plan_rebinder(previous, current)

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
        self._generation_records.bind_response_provenance(fence, json.loads(encoded))
        return True

    def record_tts_sentence_provenance(
        self,
        fence: GenerationFence,
        *,
        index: int,
        engine: str,
        model: str,
        voice: str,
        switched: bool,
    ) -> bool:
        """Append one spoken sentence's actual engine to the generation record."""

        stored = self._generation_records.response_provenance_for(fence)
        if stored is None:
            return False
        existing = stored.get("tts_sentences")
        sentences = list(existing) if isinstance(existing, list) else []
        sentences.append(
            {
                "index": index,
                "engine": engine,
                "model": model,
                "voice": voice,
                "switched": bool(switched),
            }
        )
        payload = dict(stored)
        payload["tts_sentences"] = sentences[:_TTS_SENTENCES_MAX]
        return self.bind_response_provenance(fence, payload)

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
                personal_voice_permitted=self.profile_permits(fence, capability="voice_clone_use")
                or frozen_companion_clone_permitted(policy),
                profile_id=profile_id,
                resource_id=resource_id,
                speaker_sha256=speaker_sha256,
                voice_kind=voice_kind,
            )
        ):
            return False
        self._generation_records.bind_voice_snapshot(
            fence,
            GenerationVoiceSnapshot(
                profile_id=profile_id,
                resource_id=resource_id,
                speaker_sha256=speaker_sha256,
                voice_kind=voice_kind,
            ),
        )
        return True

    def generation_voice_for(
        self,
        fence: GenerationFence,
    ) -> GenerationVoiceSnapshot | None:
        return self._generation_records.voice_snapshot_for(fence)

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
        stored = self._generation_records.response_provenance_for(fence)
        if stored is None:
            return None
        provenance = cast(dict[str, Any], json.loads(json.dumps(stored)))
        voice = self.generation_voice_for(fence)
        if voice is not None:
            planned_model = provenance.get("tts_model")
            applied: dict[str, Any] = {
                "actual_voice_profile_id": voice.profile_id,
                "actual_voice_resource_id": voice.resource_id,
                "actual_voice_speaker_sha256": voice.speaker_sha256,
            }
            if not (
                provenance.get("tts_provider") == "stepfun"
                and isinstance(planned_model, str)
                and planned_model in STEPFUN_ARCHIVE_MODELS
            ):
                applied["tts_model"] = voice.resource_id
            provenance.update(applied)
        return project_spoken_tts_provider(provenance)
