"""Monotonic transcript revisions within one fenced turn."""

from __future__ import annotations

from dataclasses import dataclass, field

from services.agent.src.contracts.ids import GenerationFence

TranscriptRevisionKey = tuple[str, int, int]


@dataclass(slots=True)
class TurnRevisionTracker:
    max_entries: int = 128
    _latest: dict[TranscriptRevisionKey, int] = field(default_factory=dict)
    _finalized: set[TranscriptRevisionKey] = field(default_factory=set)

    def issue(
        self,
        *,
        speaker: str,
        fence: GenerationFence,
        requested: int | None = None,
        final: bool = False,
    ) -> int | None:
        key = (speaker, fence.turn_id, fence.generation_id)
        if key in self._finalized:
            return None
        latest = self._latest.get(key, 0)
        revision = latest + 1 if requested is None else requested
        if revision < 1:
            raise ValueError("turn revision must be positive")
        if revision <= latest:
            return None
        self._latest[key] = revision
        if final:
            self._finalized.add(key)
        while len(self._latest) > self.max_entries:
            evicted = next(iter(self._latest))
            self._latest.pop(evicted)
            self._finalized.discard(evicted)
        return revision
