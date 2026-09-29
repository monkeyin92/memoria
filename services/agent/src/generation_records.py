"""Per-generation facts frozen to one exact ``GenerationFence``.

``DuplexRuntime`` freezes several facts per generation -- the speech plan, the
TTS reference context, the interaction policy, history/owner-projection
eligibility, input modality, planner provenance and the applied voice.  They
used to live in eight independent dicts on the runtime and its mixins; this
module is now their single owner.

Each record kind keeps its own bounded store with the historical cap and
oldest-insertion-first eviction: re-binding an already retained fence keeps
its original insertion position, and the cap is enforced right after each
capped bind.  ``inherit`` deliberately preserves the legacy uncapped copy
used when a queued auxiliary output rebinds to a fresh fence; the next capped
bind of that kind trims the store back to its cap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence

if TYPE_CHECKING:
    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.orchestration.prosody import SpeechPlan
    from services.agent.src.runtime_provenance import GenerationVoiceSnapshot

SPEECH_PLAN_MAX_FENCES = 16
TTS_REFERENCE_MAX_FENCES = 16
ELIGIBILITY_MAX_FENCES = 32
RESPONSE_PROVENANCE_MAX_FENCES = 32


def _bind[V](store: dict[GenerationFence, V], fence: GenerationFence, value: V, cap: int) -> None:
    store[fence] = value
    while len(store) > cap:
        store.pop(next(iter(store)))


class GenerationRecords:
    """Bounded per-fence records for one runtime session."""

    __slots__ = (
        "_history_eligible",
        "_input_modality",
        "_mode_policy",
        "_owner_projection_eligible",
        "_response_provenance",
        "_speech_plans",
        "_tts_references",
        "_voice_snapshots",
    )

    def __init__(self) -> None:
        self._speech_plans: dict[GenerationFence, SpeechPlan] = {}
        self._tts_references: dict[GenerationFence, tuple[str, ...]] = {}
        self._mode_policy: dict[GenerationFence, ModePolicy] = {}
        self._history_eligible: dict[GenerationFence, bool] = {}
        self._owner_projection_eligible: dict[GenerationFence, bool] = {}
        self._input_modality: dict[GenerationFence, str] = {}
        self._response_provenance: dict[GenerationFence, dict[str, Any]] = {}
        self._voice_snapshots: dict[GenerationFence, GenerationVoiceSnapshot] = {}

    # Speech plan -------------------------------------------------------

    def bind_speech_plan(self, fence: GenerationFence, plan: SpeechPlan) -> None:
        _bind(self._speech_plans, fence, plan, SPEECH_PLAN_MAX_FENCES)

    def speech_plan_for(self, fence: GenerationFence, default: SpeechPlan) -> SpeechPlan:
        """Exact fence first, then the newest plan bound to the same turn."""

        exact = self._speech_plans.get(fence)
        if exact is not None:
            return exact
        return next(
            (
                plan
                for bound_fence, plan in reversed(self._speech_plans.items())
                if bound_fence.turn_id == fence.turn_id
            ),
            default,
        )

    # TTS reference context ---------------------------------------------

    def bind_tts_references(self, fence: GenerationFence, references: tuple[str, ...]) -> None:
        _bind(self._tts_references, fence, references, TTS_REFERENCE_MAX_FENCES)

    def tts_references_for(self, fence: GenerationFence) -> tuple[str, ...]:
        return self._tts_references.get(fence, ())

    # Interaction policy ------------------------------------------------

    def bind_mode_policy(self, fence: GenerationFence, policy: ModePolicy) -> None:
        _bind(self._mode_policy, fence, policy, ELIGIBILITY_MAX_FENCES)

    def mode_policy_for(self, fence: GenerationFence) -> ModePolicy | None:
        return self._mode_policy.get(fence)

    def retain_mode_policies_for_epoch(self, session_epoch: int) -> None:
        """Drop every policy frozen to another identity epoch (order kept)."""

        self._mode_policy = {
            fence: policy
            for fence, policy in self._mode_policy.items()
            if fence.session_epoch == session_epoch
        }

    # Eligibility and modality ------------------------------------------

    def bind_history_eligible(self, fence: GenerationFence, eligible: bool) -> None:
        _bind(self._history_eligible, fence, eligible, ELIGIBILITY_MAX_FENCES)

    def history_eligible(self, fence: GenerationFence) -> bool:
        return self._history_eligible.get(fence, False)

    def bind_owner_projection_eligible(self, fence: GenerationFence, eligible: bool) -> None:
        _bind(self._owner_projection_eligible, fence, eligible, ELIGIBILITY_MAX_FENCES)

    def owner_projection_eligible(self, fence: GenerationFence) -> bool:
        return self._owner_projection_eligible.get(fence, False)

    def bind_input_modality(self, fence: GenerationFence, modality: str) -> None:
        _bind(self._input_modality, fence, modality, ELIGIBILITY_MAX_FENCES)

    def input_modality_for(self, fence: GenerationFence) -> str:
        return self._input_modality.get(fence, "audio")

    # Provenance and applied voice --------------------------------------

    def bind_response_provenance(self, fence: GenerationFence, provenance: dict[str, Any]) -> None:
        _bind(self._response_provenance, fence, provenance, RESPONSE_PROVENANCE_MAX_FENCES)

    def response_provenance_for(self, fence: GenerationFence) -> dict[str, Any] | None:
        """The stored record itself; callers copy before handing it out."""

        return self._response_provenance.get(fence)

    def bind_voice_snapshot(self, fence: GenerationFence, voice: GenerationVoiceSnapshot) -> None:
        _bind(self._voice_snapshots, fence, voice, RESPONSE_PROVENANCE_MAX_FENCES)

    def voice_snapshot_for(self, fence: GenerationFence) -> GenerationVoiceSnapshot | None:
        return self._voice_snapshots.get(fence)

    # Lifecycle ---------------------------------------------------------

    def inherit(
        self,
        source: GenerationFence,
        target: GenerationFence,
        *,
        mode_policy: ModePolicy,
        speech_plan: SpeechPlan,
    ) -> None:
        """Carry one admitted generation's facts over to its successor fence.

        Policy and eligibility use the capped binds.  The speech plan,
        provenance and voice copies keep the legacy uncapped insert.
        """

        self.bind_mode_policy(target, mode_policy)
        self.bind_history_eligible(target, self.history_eligible(source))
        self.bind_owner_projection_eligible(target, self.owner_projection_eligible(source))
        self._speech_plans[target] = speech_plan
        if source in self._response_provenance:
            self._response_provenance[target] = self._response_provenance[source]
        if source in self._voice_snapshots:
            self._voice_snapshots[target] = self._voice_snapshots[source]

    def clear(self) -> None:
        """Drop every subject-scoped record on an identity rotation.

        Interaction policies are not subject data: they are epoch-filtered by
        ``retain_mode_policies_for_epoch`` when the rotation installs a policy.
        """

        self._speech_plans.clear()
        self._tts_references.clear()
        self._history_eligible.clear()
        self._owner_projection_eligible.clear()
        self._input_modality.clear()
        self._response_provenance.clear()
        self._voice_snapshots.clear()

    def retained_fences(self) -> dict[str, tuple[GenerationFence, ...]]:
        """Diagnostic view: retained fences per record kind, oldest first."""

        return {
            "speech_plan": tuple(self._speech_plans),
            "tts_references": tuple(self._tts_references),
            "mode_policy": tuple(self._mode_policy),
            "history_eligible": tuple(self._history_eligible),
            "owner_projection_eligible": tuple(self._owner_projection_eligible),
            "input_modality": tuple(self._input_modality),
            "response_provenance": tuple(self._response_provenance),
            "voice_snapshot": tuple(self._voice_snapshots),
        }


__all__ = [
    "ELIGIBILITY_MAX_FENCES",
    "RESPONSE_PROVENANCE_MAX_FENCES",
    "SPEECH_PLAN_MAX_FENCES",
    "TTS_REFERENCE_MAX_FENCES",
    "GenerationRecords",
]
