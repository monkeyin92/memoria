"""Bind accepted ASR finals to LiveKit turns without assuming 1:1 VAD callbacks."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

_TRANSCRIPT_BOUNDARY_CHARS = " \t\r\n。！？.!?，,；;：:\"'“”‘’（）()【】[]"


def _normalized(text: str) -> str:
    return "".join(char.casefold() for char in text if char.isalnum())


def _strip_matching_prefix(text: str, prefix: str) -> tuple[bool, str]:
    expected = _normalized(prefix)
    actual = _normalized(text)
    if len(expected) < 6 or len(actual) <= len(expected):
        return False, text
    matched = 0
    for index, char in enumerate(text):
        if not char.isalnum():
            continue
        folded = char.casefold()
        if len(folded) != 1 or folded != expected[matched]:
            return False, text
        matched += 1
        if matched == len(expected):
            return True, text[index + 1 :].lstrip(_TRANSCRIPT_BOUNDARY_CHARS)
    return False, text


@dataclass(frozen=True, slots=True)
class SpeechFinalSegment:
    speech_epoch: int
    text: str
    contaminated: bool
    suspected_playback_prefixes: tuple[str, ...]

    def canonical_text(self) -> str:
        if not self.contaminated:
            return self.text.strip()
        prefixes = sorted(
            self.suspected_playback_prefixes,
            key=lambda value: len(_normalized(value)),
            reverse=True,
        )
        clean = self.text
        for prefix in prefixes:
            matched, remainder = _strip_matching_prefix(clean, prefix)
            if matched:
                clean = remainder
                break
        return clean.strip()


@dataclass(frozen=True, slots=True)
class AssembledUserTurn:
    text: str | None
    speech_epoch: int | None
    snapshot_bound: bool
    discarded_epochs: tuple[int, ...] = ()


@dataclass(slots=True)
class SpeechEpochAssembler:
    """Own VAD epochs and resolve one or more of them against a committed turn."""

    _pending: deque[SpeechFinalSegment] = field(default_factory=deque)
    _current_epoch: int | None = None
    _accepted_finals: list[str] = field(default_factory=list)
    _contaminated: bool = False
    _suspected_playback_prefixes: list[str] = field(default_factory=list)
    _final_observed: bool = False
    _unanchored_contaminated: bool = False
    _unanchored_playback_prefixes: list[str] = field(default_factory=list)

    @property
    def current_epoch(self) -> int | None:
        return self._current_epoch

    @property
    def has_suspected_playback_prefix(self) -> bool:
        return bool(
            self._suspected_playback_prefixes or self._unanchored_playback_prefixes
        )

    def start_epoch(self, speech_epoch: int) -> None:
        if self._current_epoch is not None and not self._final_observed:
            self._remember_unanchored_contamination()
        self._seal_current()
        self._reset_current()
        self._current_epoch = speech_epoch
        if self._unanchored_contaminated:
            self._contaminated = True
            self._suspected_playback_prefixes.extend(self._unanchored_playback_prefixes)
            self._unanchored_contaminated = False
            self._unanchored_playback_prefixes.clear()

    def observe_final(
        self,
        text: str,
        *,
        accepted: bool,
        contaminated: bool = False,
    ) -> None:
        if self._current_epoch is None:
            return
        self._final_observed = True
        self._contaminated = self._contaminated or contaminated
        if accepted and text.strip():
            self._accepted_finals.append(text.strip())

    def mark_contaminated(self, suspected_prefix: str | None = None) -> None:
        target = (
            self._suspected_playback_prefixes
            if self._current_epoch is not None
            else self._unanchored_playback_prefixes
        )
        if self._current_epoch is None:
            self._unanchored_contaminated = True
        else:
            self._contaminated = True
        prefix = (suspected_prefix or "").strip()
        if prefix and (not target or target[-1] != prefix):
            target.append(prefix)
            del target[:-8]

    def consume(self, raw_text: str, *, fallback_epoch: int | None) -> AssembledUserTurn:
        raw = raw_text.strip()
        active_segments = self._active_segments()
        segments = list(self._pending)
        segments.extend(active_segments)
        if not segments:
            return AssembledUserTurn(
                text=raw,
                speech_epoch=fallback_epoch,
                snapshot_bound=False,
            )

        match = self._best_match(raw, segments)
        if match is None:
            discarded = self._epochs(segments)
            self._pending.clear()
            if active_segments:
                self._reset_current()
            return AssembledUserTurn(
                text=None,
                speech_epoch=None,
                snapshot_bound=True,
                discarded_epochs=discarded,
            )

        start, end = match
        selected = segments[start : end + 1]
        discarded = self._epochs(segments[:start])
        pending_count = len(self._pending)
        remove_pending = min(end + 1, pending_count)
        for _ in range(remove_pending):
            self._pending.popleft()
        if end >= pending_count:
            self._reset_current()
        text = " ".join(
            part for segment in selected if (part := segment.canonical_text())
        ).strip()
        return AssembledUserTurn(
            text=text or None,
            speech_epoch=selected[-1].speech_epoch,
            snapshot_bound=True,
            discarded_epochs=discarded,
        )

    def discard_current(self) -> None:
        self._reset_current()
        self._unanchored_contaminated = False
        self._unanchored_playback_prefixes.clear()

    def _active_segments(self) -> list[SpeechFinalSegment]:
        if self._current_epoch is None or not self._final_observed:
            return []
        texts = self._accepted_finals or [""]
        return [
            SpeechFinalSegment(
                speech_epoch=self._current_epoch,
                text=text,
                contaminated=self._contaminated,
                suspected_playback_prefixes=tuple(self._suspected_playback_prefixes),
            )
            for text in texts
        ]

    def _seal_current(self) -> None:
        self._pending.extend(self._active_segments())

    def _remember_unanchored_contamination(self) -> None:
        if not self._contaminated:
            return
        self._unanchored_contaminated = True
        for prefix in self._suspected_playback_prefixes:
            if (
                not self._unanchored_playback_prefixes
                or self._unanchored_playback_prefixes[-1] != prefix
            ):
                self._unanchored_playback_prefixes.append(prefix)
        del self._unanchored_playback_prefixes[:-8]

    def _reset_current(self) -> None:
        self._current_epoch = None
        self._accepted_finals.clear()
        self._contaminated = False
        self._suspected_playback_prefixes.clear()
        self._final_observed = False

    @staticmethod
    def _best_match(
        raw_text: str,
        segments: list[SpeechFinalSegment],
    ) -> tuple[int, int] | None:
        raw = _normalized(raw_text)
        if not raw:
            return None
        best: tuple[tuple[int, float, int, int], tuple[int, int]] | None = None
        for start in range(len(segments)):
            canonical = ""
            contaminated = False
            for end in range(start, len(segments)):
                canonical += _normalized(segments[end].canonical_text())
                contaminated = contaminated or segments[end].contaminated
                if not canonical:
                    continue
                coverage = min(len(canonical), len(raw)) / max(len(canonical), len(raw))
                if canonical == raw:
                    tier = 3
                elif canonical in raw and (contaminated or coverage >= 0.8):
                    tier = 2
                elif raw in canonical and coverage >= 0.8:
                    tier = 1
                else:
                    continue
                score = (tier, coverage, end - start + 1, -start)
                if best is None or score > best[0]:
                    best = (score, (start, end))
        return best[1] if best is not None else None

    @staticmethod
    def _epochs(segments: Iterable[SpeechFinalSegment]) -> tuple[int, ...]:
        epochs: list[int] = []
        for segment in segments:
            epoch = segment.speech_epoch
            if not epochs or epochs[-1] != epoch:
                epochs.append(epoch)
        return tuple(epochs)
