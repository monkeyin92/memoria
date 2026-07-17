"""HeardTextTracker: assistant history may only contain actually-heard text (ch.18)."""

from __future__ import annotations

from dataclasses import dataclass, field

from services.agent.src.contracts.events import TimedWord

DEFAULT_OUTPUT_SAFETY_MARGIN_MS = 80


@dataclass
class HeardTextTracker:
    """Tracks CosyVoice word alignments + playback window to compute heard text."""

    words: list[TimedWord] = field(default_factory=list)
    playback_started_mono_ns: int | None = None
    playback_stopped_mono_ns: int | None = None
    safety_margin_ms: int = DEFAULT_OUTPUT_SAFETY_MARGIN_MS
    alignment_degraded: bool = False
    full_text: str = ""

    def reset(self) -> None:
        self.words.clear()
        self.playback_started_mono_ns = None
        self.playback_stopped_mono_ns = None
        self.alignment_degraded = False
        self.full_text = ""

    def set_full_text(self, text: str) -> None:
        self.full_text = text

    def add_words(self, words: list[TimedWord] | tuple[TimedWord, ...]) -> None:
        self.words.extend(words)

    def mark_playback_started(self, mono_ns: int) -> None:
        self.playback_started_mono_ns = mono_ns

    def mark_playback_stopped(self, mono_ns: int) -> None:
        self.playback_stopped_mono_ns = mono_ns

    def heard_audio_ms(self) -> int:
        if self.playback_started_mono_ns is None:
            return 0
        stop = self.playback_stopped_mono_ns
        if stop is None:
            return 0
        elapsed_ms = max(0, (stop - self.playback_started_mono_ns) // 1_000_000)
        return max(0, int(elapsed_ms - self.safety_margin_ms))

    def snapshot(self) -> str:
        """Return text the user actually heard. Prefer under-counting over over-counting."""
        if not self.words and not self.full_text:
            return ""

        heard_ms = self.heard_audio_ms()
        if self.alignment_degraded or not self.words:
            return self._conservative_ratio(heard_ms)

        parts: list[str] = []
        for w in self.words:
            if w.end_ms <= heard_ms:
                parts.append(w.text + (w.punctuation or ""))
            else:
                break
        return "".join(parts)

    def _conservative_ratio(self, heard_ms: int) -> str:
        if not self.full_text:
            return ""
        # Estimate duration from last word end if available; else 80ms/char rough.
        if self.words:
            total_ms = max(1, self.words[-1].end_ms)
        else:
            total_ms = max(1, len(self.full_text) * 80)
        ratio = min(1.0, heard_ms / total_ms)
        cut = max(0, int(len(self.full_text) * ratio))
        text = self.full_text[:cut]
        # Snap back to last punctuation boundary when possible.
        for i in range(len(text) - 1, -1, -1):
            if text[i] in "，。！？；、,:;!?\n":
                return text[: i + 1]
        return text
