"""Chat types of the reply pipeline's OpenAI-compatible model port.

The bridge used to borrow these from ``livekit.agents.llm``. Only this subset
was ever exercised: an ordered message list the context assembler copies and
edits per turn, its OpenAI chat serialization, the streamed chunk shape and
``StopResponse``. Semantics kept from that library:

- ``copy()`` is shallow: the copy holds the same message objects, so editing a
  message's content through the copy edits the original too.
- ``remove()`` drops the first message with the same id and raises
  ``ValueError`` when none matches.
- serialization joins a message's text parts with ``"\\n"`` into one string.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

ChatRole = Literal["developer", "system", "user", "assistant"]


def _message_id() -> str:
    return f"item_{uuid.uuid4().hex[:12]}"


@dataclass(eq=False, slots=True)
class ChatMessage:
    role: ChatRole
    content: list[str]
    id: str = field(default_factory=_message_id)

    @property
    def text_content(self) -> str:
        return "\n".join(self.content)


class ChatContext:
    """Ordered chat messages; the model sees them in this order."""

    def __init__(self, items: list[ChatMessage] | None = None) -> None:
        self._items: list[ChatMessage] = list(items or [])

    @classmethod
    def empty(cls) -> ChatContext:
        return cls()

    @property
    def items(self) -> list[ChatMessage]:
        return self._items

    def add_message(self, *, role: ChatRole, content: str | list[str]) -> ChatMessage:
        message = ChatMessage(role=role, content=[content] if isinstance(content, str) else content)
        self._items.append(message)
        return message

    def copy(self) -> ChatContext:
        return ChatContext(self._items)

    def remove(self, item: ChatMessage) -> None:
        for index, existing in enumerate(self._items):
            if existing.id == item.id:
                del self._items[index]
                return
        raise ValueError(f"Item not found: {item!r}")

    def to_openai_messages(self) -> list[dict[str, str]]:
        return [{"role": item.role, "content": item.text_content} for item in self._items]


@dataclass(frozen=True, slots=True)
class ChoiceDelta:
    content: str | None = None


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """One streamed model chunk; ``delta`` is ``None`` for metadata-only chunks."""

    id: str
    delta: ChoiceDelta | None = None


class StopResponse(Exception):  # noqa: N818 - name kept from the retired library
    """A committed turn must not be answered (stale fence or unbindable voice)."""


__all__ = [
    "ChatChunk",
    "ChatContext",
    "ChatMessage",
    "ChatRole",
    "ChoiceDelta",
    "StopResponse",
]
