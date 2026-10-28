"""Assistant-declared emotion tags for TTS and mascot expression.

The reply model starts its first line with one tag such as ``[emotion:happy]``.
``AssistantEmotionFilter`` removes that tag from the streaming text. The parsed
value travels on ``SpeechPlan.assistant_emotion`` so the TTS instruction and the
mascot expression read the same per-turn value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, cast

AssistantEmotion = Literal["neutral", "happy", "caring", "curious"]

ASSISTANT_EMOTIONS: Final[tuple[AssistantEmotion, ...]] = (
    "neutral",
    "happy",
    "caring",
    "curious",
)

_TAG_PREFIX = "[emotion:"
_TAG_PREFIX_LOWER = _TAG_PREFIX.lower()
_MAX_TAG_LEN = 32
_TTS_INSTRUCTION: Final[dict[AssistantEmotion, str]] = {
    "neutral": "",
    "happy": "回复情绪轻快，带一点笑意",
    "caring": "回复情绪温柔关切",
    "curious": "回复情绪好奇，语气自然",
}


def tts_instruction_for_assistant_emotion(emotion: AssistantEmotion) -> str:
    return _TTS_INSTRUCTION[emotion]


def compose_tts_instruction(base: str, emotion: AssistantEmotion) -> str:
    """Append the declared emotion to the provider-neutral TTS instruction."""
    clause = _TTS_INSTRUCTION[emotion]
    head = base.strip().rstrip("。")
    if head and clause:
        return f"{head}；{clause}"[:240]
    return (head or clause)[:240]


@dataclass
class AssistantEmotionFilter:
    """Strip one leading ``[emotion:...]`` tag from a streaming reply.

    The tag may arrive in pieces, so ``feed`` returns ``None`` until the prefix
    resolves. Unknown values are stripped and ignored. Any ``[emotion:...]``
    marker is treated as internal, so it never reaches TTS or chat history.
    """

    emotion: AssistantEmotion | None = None
    _buffer: str = ""
    _resolved: bool = False
    _drop_until_close: bool = False

    def feed(self, token: str) -> str | None:
        if self._resolved:
            if self._drop_until_close:
                end = token.find("]")
                if end < 0:
                    return None
                self._drop_until_close = False
                return token[end + 1 :].lstrip()
            return token
        self._buffer += token
        stripped = self._buffer.lstrip()
        if not stripped:
            return None
        lower = stripped.lower()
        if not lower.startswith(_TAG_PREFIX_LOWER):
            if _TAG_PREFIX_LOWER.startswith(lower):
                return None
            self._resolved = True
            text = self._buffer
            self._buffer = ""
            return text
        end = stripped.find("]")
        if end < 0:
            if len(stripped) > _MAX_TAG_LEN:
                self._resolved = True
                self._drop_until_close = True
                self._buffer = ""
            return None
        inner = stripped[1:end].strip()
        if not inner.lower().startswith("emotion:"):
            self._resolved = True
            text = self._buffer
            self._buffer = ""
            return text
        value = inner.split(":", 1)[1].strip().lower()
        if value in ASSISTANT_EMOTIONS:
            self.emotion = cast(AssistantEmotion, value)  # type: ignore[redundant-cast]
        self._resolved = True
        text = stripped[end + 1 :].lstrip()
        self._buffer = ""
        return text

    def flush(self) -> str:
        """Release pending text, or drop an incomplete reserved tag."""
        if self._resolved:
            if self._drop_until_close:
                self._buffer = ""
            return ""
        self._resolved = True
        stripped = self._buffer.lstrip()
        if not stripped:
            self._buffer = ""
            return ""
        lower = stripped.lower()
        if lower.startswith(_TAG_PREFIX_LOWER) or _TAG_PREFIX_LOWER.startswith(lower):
            self._buffer = ""
            return ""
        text = self._buffer
        self._buffer = ""
        return text
