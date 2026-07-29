"""Ephemeral, conservative emotion observations for one live voice session."""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

EMOTION_LABELS = frozenset(
    {"neutral", "happy", "sad", "angry", "fearful", "disgusted", "surprised"}
)
_EXPLICIT_SELF_REPORTS = {
    "happy": (
        "我很开心",
        "我好开心",
        "我现在很开心",
        "我真的很开心",
        "我现在真的很开心",
        "我很高兴",
    ),
    "sad": ("我很难过", "我好难过", "我很伤心", "我现在很低落"),
    "angry": ("我很生气", "我现在很愤怒", "我气死了"),
    "fearful": ("我很害怕", "我现在很怕", "我好恐惧"),
    "surprised": ("我很惊讶", "我没想到", "太意外了"),
}
_QUOTED_SPEECH = re.compile(r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|\"[^\"]*\"|'[^']*'")
_LAUGHTER = re.compile(r"(?:哈){2,}|(?:呵){2,}|(?:嘿){2,}|[（(\[]笑(?:声)?[）)\]]")
_REPORTED_SPEECH_PREFIXES = (
    "她说",
    "他说",
    "别人说",
    "朋友说",
    "同事说",
    "妈妈说",
    "爸爸说",
)


def _contains_unquoted_laughter(text: str) -> bool:
    return _LAUGHTER.search(_QUOTED_SPEECH.sub("", text)) is not None


def aggregate_acoustic_segments(
    segments: Sequence[tuple[str, str]],
) -> tuple[str, str]:
    """Collapse one provider turn into one conservative acoustic observation."""

    labels = [label if label in EMOTION_LABELS else "neutral" for label, _ in segments]
    non_neutral = {label for label in labels if label != "neutral"}
    provider_label = next(iter(non_neutral)) if len(non_neutral) == 1 else "neutral"
    texts = list(dict.fromkeys(text.strip() for _, text in segments if text.strip()))
    return provider_label, " ".join(texts)


@dataclass(frozen=True)
class EmotionObservation:
    label: str
    provider_label: str
    provider_confidence: None
    evidence: tuple[str, ...]
    observed_at_ns: int
    expires_at_ns: int
    persist: bool = False


def neutral_observation(now_ns: int, ttl_ms: int) -> EmotionObservation:
    return EmotionObservation(
        label="neutral",
        provider_label="neutral",
        provider_confidence=None,
        evidence=("fallback:neutral",),
        observed_at_ns=now_ns,
        expires_at_ns=now_ns + ttl_ms * 1_000_000,
    )


@dataclass
class EmotionSmoother:
    """Requires repeated acoustic evidence unless the user explicitly self-reports."""

    ttl_ms: int = 30_000
    _provider_history: deque[tuple[int, str]] = field(
        default_factory=lambda: deque(maxlen=3)
    )
    _current: EmotionObservation | None = None

    def observe_acoustic(
        self,
        provider_label: str,
        *,
        text: str = "",
        turn_id: int,
        now_ns: int | None = None,
    ) -> EmotionObservation:
        now = now_ns if now_ns is not None else time.monotonic_ns()
        normalized = provider_label if provider_label in EMOTION_LABELS else "neutral"
        if not self._provider_history or self._provider_history[-1][0] != turn_id:
            self._provider_history.append((turn_id, normalized))
        explicit = self._explicit_label(text)
        evidence: tuple[str, ...]
        if explicit is not None:
            label = explicit
            evidence = ("explicit:self_report", "acoustic:qwen3-asr")
        elif _contains_unquoted_laughter(text):
            label = "neutral"
            evidence = ("acoustic:qwen3-asr:laughter", "fallback:neutral")
        elif normalized == "neutral":
            label = "neutral"
            evidence = ("acoustic:qwen3-asr",)
        elif len(self._provider_history) >= 2 and all(
            history_label == normalized
            for _, history_label in tuple(self._provider_history)[-2:]
        ):
            label = normalized
            evidence = ("acoustic:qwen3-asr:repeated",)
        else:
            label = "neutral"
            evidence = ("acoustic:qwen3-asr:single", "fallback:neutral")

        observation = EmotionObservation(
            label=label,
            provider_label=normalized,
            provider_confidence=None,
            evidence=evidence,
            observed_at_ns=now,
            expires_at_ns=now + self.ttl_ms * 1_000_000,
        )
        self._current = observation
        return observation

    def observe_text(
        self,
        text: str,
        *,
        acoustic: EmotionObservation | None = None,
        now_ns: int | None = None,
    ) -> EmotionObservation:
        now = now_ns if now_ns is not None else time.monotonic_ns()
        explicit = self._explicit_label(text)
        label = explicit
        evidence = ("explicit:self_report",)
        if label is None and _contains_unquoted_laughter(text):
            acoustic_laughter = (
                acoustic is not None
                and now <= acoustic.expires_at_ns
                and "acoustic:qwen3-asr:laughter" in acoustic.evidence
            )
            observation = EmotionObservation(
                label="neutral",
                provider_label=self._current.provider_label if self._current else "neutral",
                provider_confidence=None,
                evidence=(
                    (
                        "acoustic:qwen3-asr:laughter",
                        "text:laughter",
                        "fallback:neutral",
                    )
                    if acoustic_laughter
                    else ("text:laughter", "fallback:neutral")
                ),
                observed_at_ns=now,
                expires_at_ns=now + self.ttl_ms * 1_000_000,
            )
            self._current = observation
            return observation
        if label is None and acoustic is not None and now <= acoustic.expires_at_ns:
            self._current = acoustic
            return acoustic
        if label is None:
            self._current = neutral_observation(now, self.ttl_ms)
            return self._current
        observation = EmotionObservation(
            label=label,
            provider_label=self._current.provider_label if self._current else "neutral",
            provider_confidence=None,
            evidence=evidence,
            observed_at_ns=now,
            expires_at_ns=now + self.ttl_ms * 1_000_000,
        )
        self._current = observation
        return observation

    def current(self, *, now_ns: int | None = None) -> EmotionObservation:
        now = now_ns if now_ns is not None else time.monotonic_ns()
        if self._current is None or now > self._current.expires_at_ns:
            self._current = neutral_observation(now, self.ttl_ms)
        return self._current

    @staticmethod
    def _explicit_label(text: str) -> str | None:
        compact = "".join(_QUOTED_SPEECH.sub("", text).split())
        for label, phrases in _EXPLICIT_SELF_REPORTS.items():
            for phrase in phrases:
                normalized = "".join(phrase.split())
                start = compact.find(normalized)
                if start < 0:
                    continue
                prefix = compact[max(0, start - 8) : start]
                if any(prefix.endswith(marker) for marker in _REPORTED_SPEECH_PREFIXES):
                    continue
                return label
        return None
