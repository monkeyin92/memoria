"""Strict one-shot client for the Control-issued response plan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import httpx

from services.agent.src.contracts.ids import GenerationFence
from services.speaker.domain import SpeakerDecision

EpistemicStatus = Literal["not_applicable", "fact", "inference", "unknown", "mixed"]
GroundedKind = Literal[
    "memory_claim",
    "persona_trait",
    "cognitive_claim",
    "decision_case",
    "relationship_profile",
]
GroundedUse = Literal[
    "fact",
    "style",
    "decision_precedent",
    "relationship_rule",
    "boundary",
]
Disclosure = Literal["digital_identity", "inference", "unknown", "privacy_refusal"]
VoiceTargetKind = Literal["companion", "approved_personal", "fallback"]
CANONICAL_PLANNER_POLICY_VERSION: Final = "digital-self-response-planner-v1"

_EPISTEMIC_STATUSES = frozenset(
    {"not_applicable", "fact", "inference", "unknown", "mixed"}
)
_GROUNDED_KINDS = frozenset(
    {
        "memory_claim",
        "persona_trait",
        "cognitive_claim",
        "decision_case",
        "relationship_profile",
    }
)
_GROUNDED_USES = frozenset(
    {"fact", "style", "decision_precedent", "relationship_rule", "boundary"}
)
_DISCLOSURES = frozenset(
    {"digital_identity", "inference", "unknown", "privacy_refusal"}
)
_VOICE_TARGET_KINDS = frozenset({"companion", "approved_personal", "fallback"})
_PLAN_KEYS = frozenset(
    {
        "fence",
        "instructions",
        "direct_text",
        "epistemic_status",
        "epistemic_reason_codes",
        "grounded_items",
        "disclosures",
        "voice_target",
        "provenance",
    }
)
_FENCE_KEYS = frozenset(
    {"session_id", "turn_id", "generation_id", "tool_epoch"}
)
_GROUNDED_ITEM_KEYS = frozenset(
    {
        "kind",
        "item_id",
        "content",
        "use_as",
        "source_event_ids",
        "confidence",
        "sharing_scope",
    }
)
_VOICE_TARGET_KEYS = frozenset({"kind", "profile_id", "model"})
_PROVENANCE_KEYS = frozenset(
    {
        "planner_policy_version",
        "interaction_mode",
        "mode_policy_version",
        "digital_self_version_id",
        "manifest_sha256",
        "persona_version_id",
        "persona_version_number",
        "persona_style_only",
        "relationship_profile_id",
        "relationship_profile_version",
        "speaker_class",
        "speaker_reason_code",
        "speaker_profile_id",
        "speaker_model_version",
        "speaker_template_version",
        "source_refs",
        "epistemic_status",
        "epistemic_reason_codes",
        "disclosures",
    }
)
_SOURCE_REF_KEYS = frozenset({"kind", "item_id", "source_event_ids"})


@dataclass(frozen=True, slots=True)
class ResponsePlannerClientConfig:
    endpoint: str
    internal_token: str
    timeout_s: float = 0.8

    def __post_init__(self) -> None:
        url = httpx.URL(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("response planner endpoint must be HTTP(S)")
        if not self.internal_token.strip():
            raise ValueError("response planner internal token must not be blank")
        if self.timeout_s <= 0:
            raise ValueError("response planner timeout must be positive")


@dataclass(frozen=True, slots=True)
class ResponseSourceRef:
    kind: GroundedKind
    item_id: str
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResponseGroundedItem:
    kind: GroundedKind
    item_id: str
    content: str
    use_as: GroundedUse
    source_event_ids: tuple[str, ...]
    confidence: float | None
    sharing_scope: str | None


@dataclass(frozen=True, slots=True)
class ResponseVoiceTarget:
    kind: VoiceTargetKind
    profile_id: str | None
    model: str


@dataclass(frozen=True, slots=True)
class ResponseProvenance:
    planner_policy_version: str
    interaction_mode: str
    mode_policy_version: str
    digital_self_version_id: str | None
    manifest_sha256: str | None
    persona_version_id: str | None
    persona_version_number: int | None
    persona_style_only: bool
    relationship_profile_id: str | None
    relationship_profile_version: int | None
    speaker_class: Literal["owner", "guest", "uncertain"]
    speaker_reason_code: str
    speaker_profile_id: str | None
    speaker_model_version: str
    speaker_template_version: int | None
    source_refs: tuple[ResponseSourceRef, ...]
    epistemic_status: EpistemicStatus
    epistemic_reason_codes: tuple[str, ...]
    disclosures: tuple[Disclosure, ...]

    def archive_payload(
        self,
        *,
        fence: GenerationFence,
        llm_provider: str | None,
        llm_model: str | None,
        tts_provider: str | None,
        tts_model: str | None,
        actual_voice_profile_id: str | None,
    ) -> dict[str, Any]:
        return {
            "fence": {
                "session_id": fence.session_id,
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "tool_epoch": fence.tool_epoch,
            },
            "planner_policy_version": self.planner_policy_version,
            "interaction_mode": self.interaction_mode,
            "mode_policy_version": self.mode_policy_version,
            "digital_self_version_id": self.digital_self_version_id,
            "manifest_sha256": self.manifest_sha256,
            "persona_version_id": self.persona_version_id,
            "persona_version_number": self.persona_version_number,
            "persona_style_only": self.persona_style_only,
            "relationship_profile_id": self.relationship_profile_id,
            "relationship_profile_version": self.relationship_profile_version,
            "speaker_class": self.speaker_class,
            "speaker_reason_code": self.speaker_reason_code,
            "speaker_profile_id": self.speaker_profile_id,
            "speaker_model_version": self.speaker_model_version,
            "speaker_template_version": self.speaker_template_version,
            "source_refs": [
                {
                    "kind": ref.kind,
                    "item_id": ref.item_id,
                    "source_event_ids": list(ref.source_event_ids),
                }
                for ref in self.source_refs
            ],
            "epistemic_status": self.epistemic_status,
            "epistemic_reason_codes": list(self.epistemic_reason_codes),
            "disclosures": list(self.disclosures),
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "tts_provider": tts_provider,
            "tts_model": tts_model,
            "actual_voice_profile_id": actual_voice_profile_id,
        }


@dataclass(frozen=True, slots=True)
class ResponsePlan:
    fence: GenerationFence
    instructions: str
    direct_text: str | None
    epistemic_status: EpistemicStatus
    epistemic_reason_codes: tuple[str, ...]
    grounded_items: tuple[ResponseGroundedItem, ...]
    disclosures: tuple[Disclosure, ...]
    voice_target: ResponseVoiceTarget
    provenance: ResponseProvenance


@dataclass(frozen=True, slots=True)
class ResponsePlanFetch:
    plan: ResponsePlan | None
    reason: str

    @property
    def available(self) -> bool:
        return self.plan is not None


class ResponsePlannerClient:
    """Fetch exactly one bounded plan; failures never expose cached private context."""

    def __init__(
        self,
        config: ResponsePlannerClientConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_s)
        self._owns_client = client is None

    async def fetch(
        self,
        *,
        session_id: str,
        query: str,
        fence: GenerationFence,
        speaker_decision: SpeakerDecision,
    ) -> ResponsePlanFetch:
        normalized_query = query.strip()
        if (
            not session_id.strip()
            or session_id != fence.session_id
            or not normalized_query
            or len(normalized_query) > 4000
        ):
            return ResponsePlanFetch(None, "request_invalid")
        try:
            response = await self._client.post(
                self._config.endpoint,
                headers={"X-Memoria-Internal-Token": self._config.internal_token},
                json={
                    "session_id": session_id,
                    "query": normalized_query,
                    "fence": {
                        "session_id": fence.session_id,
                        "turn_id": fence.turn_id,
                        "generation_id": fence.generation_id,
                        "tool_epoch": fence.tool_epoch,
                    },
                    "speaker_decision": {
                        "classification": speaker_decision.classification,
                        "reason_code": speaker_decision.reason_code,
                        "model_version": speaker_decision.model_version,
                        "profile_id": speaker_decision.profile_id,
                        "template_version": speaker_decision.template_version,
                    },
                },
                timeout=self._config.timeout_s,
            )
            if response.status_code != 200:
                return ResponsePlanFetch(None, f"http_{response.status_code}")
            plan = self._parse(response.json())
        except (httpx.HTTPError, TypeError, ValueError):
            return ResponsePlanFetch(None, "request_or_payload_invalid")
        if not plan.fence.matches(fence):
            return ResponsePlanFetch(None, "fence_mismatch")
        provenance = plan.provenance
        if (
            provenance.speaker_class != speaker_decision.classification
            or provenance.speaker_reason_code != speaker_decision.reason_code
            or provenance.speaker_profile_id != speaker_decision.profile_id
            or provenance.speaker_model_version != speaker_decision.model_version
            or provenance.speaker_template_version != speaker_decision.template_version
        ):
            return ResponsePlanFetch(None, "speaker_mismatch")
        return ResponsePlanFetch(plan, "ok")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @classmethod
    def _parse(cls, payload: Any) -> ResponsePlan:
        if not isinstance(payload, dict):
            raise ValueError("response plan must be an object")
        cls._require_keys(payload, _PLAN_KEYS, label="response plan")
        fence = cls._fence(payload.get("fence"))
        instructions = cls._text(payload, "instructions", 8000)
        direct_text = cls._optional_text(payload, "direct_text", 1000)
        epistemic_status = cast(
            EpistemicStatus,
            cls._enum(
                payload.get("epistemic_status"),
                _EPISTEMIC_STATUSES,
            ),
        )
        reason_codes = cls._string_tuple(
            payload.get("epistemic_reason_codes"),
            max_items=16,
            max_length=64,
        )
        disclosures = tuple(
            cast(Disclosure, item)
            for item in cls._enum_tuple(
                payload.get("disclosures"),
                _DISCLOSURES,
                max_items=4,
            )
        )
        raw_items = payload.get("grounded_items")
        if not isinstance(raw_items, list) or len(raw_items) > 32:
            raise ValueError("grounded items are invalid")
        grounded_items = tuple(cls._grounded_item(item) for item in raw_items)
        voice_target = cls._voice_target(payload.get("voice_target"))
        provenance = cls._provenance(payload.get("provenance"))
        if (
            provenance.epistemic_status != epistemic_status
            or provenance.epistemic_reason_codes != reason_codes
            or provenance.disclosures != disclosures
        ):
            raise ValueError("response plan provenance disagrees with the plan")
        cls._validate_grounded_source_refs(grounded_items, provenance.source_refs)
        return ResponsePlan(
            fence=fence,
            instructions=instructions,
            direct_text=direct_text,
            epistemic_status=epistemic_status,
            epistemic_reason_codes=reason_codes,
            grounded_items=grounded_items,
            disclosures=disclosures,
            voice_target=voice_target,
            provenance=provenance,
        )

    @classmethod
    def _grounded_item(cls, value: Any) -> ResponseGroundedItem:
        if not isinstance(value, dict):
            raise ValueError("grounded item is invalid")
        cls._require_keys(value, _GROUNDED_ITEM_KEYS, label="grounded item")
        kind = cast(
            GroundedKind,
            cls._enum(value.get("kind"), _GROUNDED_KINDS),
        )
        use_as = cast(
            GroundedUse,
            cls._enum(value.get("use_as"), _GROUNDED_USES),
        )
        confidence = value.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
        ):
            raise ValueError("grounded item confidence is invalid")
        return ResponseGroundedItem(
            kind=kind,
            item_id=cls._text(value, "item_id", 128),
            content=cls._text(value, "content", 2000),
            use_as=use_as,
            source_event_ids=cls._string_tuple(
                value.get("source_event_ids"),
                max_items=32,
                max_length=128,
            ),
            confidence=float(confidence) if confidence is not None else None,
            sharing_scope=cls._optional_text(value, "sharing_scope", 128),
        )

    @classmethod
    def _voice_target(cls, value: Any) -> ResponseVoiceTarget:
        if not isinstance(value, dict):
            raise ValueError("voice target is invalid")
        cls._require_keys(value, _VOICE_TARGET_KEYS, label="voice target")
        return ResponseVoiceTarget(
            kind=cast(
                VoiceTargetKind,
                cls._enum(value.get("kind"), _VOICE_TARGET_KINDS),
            ),
            profile_id=cls._optional_text(value, "profile_id", 128),
            model=cls._text(value, "model", 128),
        )

    @classmethod
    def _provenance(cls, value: Any) -> ResponseProvenance:
        if not isinstance(value, dict):
            raise ValueError("response provenance is invalid")
        cls._require_keys(value, _PROVENANCE_KEYS, label="response provenance")
        source_refs_raw = value.get("source_refs")
        if not isinstance(source_refs_raw, list) or len(source_refs_raw) > 32:
            raise ValueError("response provenance source refs are invalid")
        source_refs = tuple(cls._source_ref(item) for item in source_refs_raw)
        speaker_class = cls._enum(
            value.get("speaker_class"),
            frozenset({"owner", "guest", "uncertain"}),
        )
        manifest_sha256 = cls._optional_text(value, "manifest_sha256", 64)
        if manifest_sha256 is not None and (
            len(manifest_sha256) != 64
            or any(char not in "0123456789abcdef" for char in manifest_sha256)
        ):
            raise ValueError("response provenance manifest digest is invalid")
        relationship_version = value.get("relationship_profile_version")
        if relationship_version is not None and (
            isinstance(relationship_version, bool)
            or not isinstance(relationship_version, int)
            or relationship_version < 1
        ):
            raise ValueError("response provenance relationship version is invalid")
        persona_version_number = value.get("persona_version_number")
        if persona_version_number is not None and (
            isinstance(persona_version_number, bool)
            or not isinstance(persona_version_number, int)
            or persona_version_number < 1
        ):
            raise ValueError("response provenance persona version is invalid")
        persona_style_only = value.get("persona_style_only")
        if not isinstance(persona_style_only, bool):
            raise ValueError("response provenance persona style marker is invalid")
        persona_version_id = cls._optional_text(value, "persona_version_id", 128)
        if (persona_version_id is None) != (persona_version_number is None):
            raise ValueError("response provenance persona version fields must be paired")
        if persona_style_only and persona_version_id is None:
            raise ValueError(
                "response provenance persona style marker requires a persona version"
            )
        planner_policy_version = cls._text(value, "planner_policy_version", 64)
        if planner_policy_version != CANONICAL_PLANNER_POLICY_VERSION:
            raise ValueError("response plan policy version is invalid")
        template_version = value.get("speaker_template_version")
        if template_version is not None and (
            isinstance(template_version, bool)
            or not isinstance(template_version, int)
            or template_version < 1
        ):
            raise ValueError("response provenance speaker template is invalid")
        return ResponseProvenance(
            planner_policy_version=planner_policy_version,
            interaction_mode=cls._text(value, "interaction_mode", 32),
            mode_policy_version=cls._text(value, "mode_policy_version", 128),
            digital_self_version_id=cls._optional_text(
                value,
                "digital_self_version_id",
                128,
            ),
            manifest_sha256=manifest_sha256,
            persona_version_id=persona_version_id,
            persona_version_number=persona_version_number,
            persona_style_only=persona_style_only,
            relationship_profile_id=cls._optional_text(
                value,
                "relationship_profile_id",
                128,
            ),
            relationship_profile_version=relationship_version,
            speaker_class=cast(
                Literal["owner", "guest", "uncertain"],
                speaker_class,
            ),
            speaker_reason_code=cls._text(value, "speaker_reason_code", 96),
            speaker_profile_id=cls._optional_text(
                value,
                "speaker_profile_id",
                128,
            ),
            speaker_model_version=cls._text(
                value,
                "speaker_model_version",
                128,
            ),
            speaker_template_version=template_version,
            source_refs=source_refs,
            epistemic_status=cast(
                EpistemicStatus,
                cls._enum(value.get("epistemic_status"), _EPISTEMIC_STATUSES),
            ),
            epistemic_reason_codes=cls._string_tuple(
                value.get("epistemic_reason_codes"),
                max_items=16,
                max_length=64,
            ),
            disclosures=tuple(
                cast(Disclosure, item)
                for item in cls._enum_tuple(
                    value.get("disclosures"),
                    _DISCLOSURES,
                    max_items=4,
                )
            ),
        )

    @classmethod
    def _source_ref(cls, value: Any) -> ResponseSourceRef:
        if not isinstance(value, dict):
            raise ValueError("response source ref is invalid")
        cls._require_keys(value, _SOURCE_REF_KEYS, label="response source ref")
        return ResponseSourceRef(
            kind=cast(
                GroundedKind,
                cls._enum(value.get("kind"), _GROUNDED_KINDS),
            ),
            item_id=cls._text(value, "item_id", 128),
            source_event_ids=cls._string_tuple(
                value.get("source_event_ids"),
                max_items=32,
                max_length=128,
            ),
        )

    @staticmethod
    def _validate_grounded_source_refs(
        grounded_items: tuple[ResponseGroundedItem, ...],
        source_refs: tuple[ResponseSourceRef, ...],
    ) -> None:
        grounded_keys = tuple(
            (item.kind, item.item_id, item.source_event_ids) for item in grounded_items
        )
        ref_keys = tuple(
            (ref.kind, ref.item_id, ref.source_event_ids) for ref in source_refs
        )
        if any(not source_ids for _, _, source_ids in grounded_keys + ref_keys):
            raise ValueError("grounded provenance source ids are required")
        if len(grounded_keys) != len(set(grounded_keys)):
            raise ValueError("grounded provenance items must be unique")
        if len(ref_keys) != len(set(ref_keys)):
            raise ValueError("grounded provenance refs must be unique")
        ref_key_set = set(ref_keys)
        if any(key not in ref_key_set for key in grounded_keys):
            raise ValueError("grounded provenance ref disagrees with grounded item")
        grounded_key_set = set(grounded_keys)
        if any(
            (kind, item_id, source_event_ids) not in grounded_key_set
            and kind not in {"persona_trait", "relationship_profile"}
            for kind, item_id, source_event_ids in ref_keys
        ):
            raise ValueError("ungrounded fact provenance is not allowed")

    @staticmethod
    def _fence(value: Any) -> GenerationFence:
        if not isinstance(value, dict):
            raise ValueError("response plan fence is invalid")
        ResponsePlannerClient._require_keys(
            value,
            _FENCE_KEYS,
            label="response plan fence",
        )
        session_id = value.get("session_id")
        turn_id = value.get("turn_id")
        generation_id = value.get("generation_id")
        tool_epoch = value.get("tool_epoch")
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 128
            or isinstance(turn_id, bool)
            or not isinstance(turn_id, int)
            or turn_id < 0
            or isinstance(generation_id, bool)
            or not isinstance(generation_id, int)
            or generation_id < 0
            or isinstance(tool_epoch, bool)
            or not isinstance(tool_epoch, int)
            or tool_epoch < 0
        ):
            raise ValueError("response plan fence is invalid")
        return GenerationFence(
            session_id=session_id,
            turn_id=turn_id,
            generation_id=generation_id,
            tool_epoch=tool_epoch,
        )

    @staticmethod
    def _enum(value: Any, allowed: frozenset[str]) -> str:
        if not isinstance(value, str) or value not in allowed:
            raise ValueError("response plan enum is invalid")
        return value

    @classmethod
    def _enum_tuple(
        cls,
        value: Any,
        allowed: frozenset[str],
        *,
        max_items: int,
    ) -> tuple[str, ...]:
        if not isinstance(value, list) or len(value) > max_items:
            raise ValueError("response plan enum list is invalid")
        result = tuple(cls._enum(item, allowed) for item in value)
        if len(result) != len(set(result)):
            raise ValueError("response plan enum list must be unique")
        return result

    @staticmethod
    def _text(value: dict[str, Any], key: str, max_length: int) -> str:
        text = value.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > max_length:
            raise ValueError(f"response plan {key} is invalid")
        return text

    @staticmethod
    def _optional_text(
        value: dict[str, Any],
        key: str,
        max_length: int,
    ) -> str | None:
        text = value.get(key)
        if text is None:
            return None
        if not isinstance(text, str) or not text.strip() or len(text) > max_length:
            raise ValueError(f"response plan {key} is invalid")
        return text

    @staticmethod
    def _string_tuple(
        value: Any,
        *,
        max_items: int,
        max_length: int,
    ) -> tuple[str, ...]:
        if not isinstance(value, list) or len(value) > max_items:
            raise ValueError("response plan string list is invalid")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item) > max_length:
                raise ValueError("response plan string list is invalid")
            result.append(item)
        if len(result) != len(set(result)):
            raise ValueError("response plan string list must be unique")
        return tuple(result)

    @staticmethod
    def _require_keys(
        value: dict[str, Any],
        expected: frozenset[str],
        *,
        label: str,
    ) -> None:
        if set(value) != expected:
            raise ValueError(f"{label} fields are invalid")
