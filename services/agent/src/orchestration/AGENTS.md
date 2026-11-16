# Duplex Orchestration (services/agent/src/orchestration)

## OVERVIEW
26 flat modules planning and shaping a fenced voice turn: state machine, interruption guard, turn projection, prosody, tool/task management, and context snapshots. No `__init__` re-exports - import leaf modules directly.

## WHERE TO LOOK

| Area | Modules |
|------|---------|
| Turn state | `state_machine.py` (`ConversationState`, `InteractionPhase`; 46 refs), `conversation_projection.py`, `conversation_projection_range.py`, `projection_types.py`, `turn_revision.py` |
| Interruption / routing | `interruption_guard.py` (28 refs), `utterance_router.py`, `phrase_segmenter.py`, `speech_epoch_assembler.py`, `heard_text_tracker.py` |
| Reply planning | `orchestrator.py` (`Orchestrator`, `OfflinePipeline`), `prosody.py` (`SpeechPlan`), `assistant_emotion.py`, `emotion.py`, `stable_prefix.py` |
| Tools / delegation | `task_manager.py` (`TaskManager`, `ToolSpec`), `delegation_coordinator.py`, `coordination_contracts.py` (Protocols) |
| Context / speaker | `context_snapshot_manager.py`, `context_manager.py`, `speaker_verify.py` (numpy score only, never identity) |
| Handlers / fences | `handlers.py` (LLM/TTS Protocol ports), `generation_fence.py` (`FenceGate`), `interaction_plane.py`, `speech_timeline.py` |

## CONVENTIONS
- `enum.StrEnum` for every enum; value types are `@dataclass(frozen=True, slots=True)`; pluggable seams are `Protocol`s.
- Chinese domain constants (backchannel whitelist, interrupt prefixes, emotion labels) live at module top.
- One-line docstrings cite the design-doc chapter instead of restating it.
- Import cycles avoided via TYPE_CHECKING-only imports.

## ANTI-PATTERNS
- Never merge device-close phrases with interruption command lists (exact matching prevents 「我知道了怎么做」 false-close).
- Speaker verification score is never identity authority; accepted results become fenced `OutputIntent` only.
- Assistant history may contain only actually-heard text, never generated-but-unplayed output.
- `SpeechPlan.pitch` packs pitch and loudness (steps of 16) - deliberate; do not split the field.
- `conversation_projection.py` is budget-locked at 1067 lines; `interruption_guard.py` (1086) and `orchestrator.py` (998) are unbudgeted - their growth is unguarded.

Earned: score 14 (26 files, `state_machine` 46 refs, `task_manager` 29) - distinct domain.
