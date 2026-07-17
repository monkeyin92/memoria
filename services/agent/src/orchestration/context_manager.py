"""Conversation context for DeepSeek: heard-text only for assistant (ch.13.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from services.common.redaction import redact_pii

Role = Literal["system", "user", "assistant"]

@dataclass
class ChatMessage:
    role: Role
    content: str


@dataclass
class ContextManager:
    system_prompt: str
    business_summary: str = ""
    turns: list[ChatMessage] = field(default_factory=list)
    max_turns: int = 16  # 8 user + 8 assistant pairs ≈ 16 messages

    def add_user(self, text: str) -> None:
        self.turns.append(ChatMessage(role="user", content=redact_pii(text.strip())))
        self._trim()

    def commit_assistant_heard(self, heard_text: str) -> ChatMessage | None:
        text = redact_pii(heard_text.strip())
        if not text:
            return None
        message = ChatMessage(role="assistant", content=text)
        self.turns.append(message)
        self._trim()
        return message

    def commit_interrupted_assistant_text(self, heard_text: str) -> ChatMessage | None:
        """History must not contain unheard suffix."""
        return self.commit_assistant_heard(heard_text)

    def refine_assistant_heard(
        self,
        message: ChatMessage | None,
        heard_text: str,
    ) -> ChatMessage | None:
        """Replace a conservative interrupt snapshot with a playout fact."""
        text = redact_pii(heard_text.strip())
        if message is not None:
            index = next(
                (i for i, turn in enumerate(self.turns) if turn is message),
                None,
            )
            if index is not None:
                if text:
                    self.turns[index].content = text
                    return self.turns[index]
                self.turns.pop(index)
                return None
        if not text:
            return None
        return self.commit_assistant_heard(text)

    def _trim(self) -> None:
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns :]

    def build_messages(
        self,
        *,
        current_user_final: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
        ]
        if self.business_summary:
            summary = self.business_summary[:600]
            messages.append(
                {
                    "role": "system",
                    "content": f"当前业务状态摘要：{summary}",
                }
            )
        for m in self.turns:
            messages.append({"role": m.role, "content": m.content})
        if current_user_final:
            messages.append({"role": "user", "content": redact_pii(current_user_final)})
        _ = tools  # tool definitions attached at API layer, not in messages
        return messages

    def asr_context_items(self, *, max_each: int = 5, max_chars: int = 400) -> list[dict[str, str]]:
        users = [m for m in self.turns if m.role == "user"][-max_each:]
        assts = [m for m in self.turns if m.role == "assistant"][-max_each:]
        items: list[dict[str, str]] = []
        for m in users + assts:
            items.append({"role": m.role, "text": m.content[:max_chars]})
        return items
