"""Conversation context for DeepSeek: heard-text only for assistant (ch.13.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from services.common.redaction import redact_pii

Role = Literal["system", "user", "assistant"]
SpeakerScope = Literal["owner", "public"]


@dataclass
class ChatMessage:
    role: Role
    content: str
    speaker_scope: SpeakerScope = "public"


@dataclass
class ContextManager:
    system_prompt: str
    business_summary: str = ""
    rolling_summary: str = ""
    turns: list[ChatMessage] = field(default_factory=list)
    max_turns: int = 16  # 8 user + 8 assistant pairs ≈ 16 messages
    max_rolling_summary_chars: int = 1_200

    def add_user(self, text: str, *, speaker_scope: SpeakerScope = "public") -> None:
        self.turns.append(
            ChatMessage(
                role="user",
                content=redact_pii(text.strip()),
                speaker_scope=speaker_scope,
            )
        )
        self._trim()

    def commit_assistant_heard(
        self,
        heard_text: str,
        *,
        speaker_scope: SpeakerScope,
    ) -> ChatMessage | None:
        text = redact_pii(heard_text.strip())
        if not text:
            return None
        message = ChatMessage(
            role="assistant",
            content=text,
            speaker_scope=speaker_scope,
        )
        self.turns.append(message)
        self._trim()
        return message

    def commit_interrupted_assistant_text(
        self,
        heard_text: str,
        *,
        speaker_scope: SpeakerScope,
    ) -> ChatMessage | None:
        """History must not contain unheard suffix."""
        return self.commit_assistant_heard(heard_text, speaker_scope=speaker_scope)

    def refine_assistant_heard(
        self,
        message: ChatMessage | None,
        heard_text: str,
        *,
        speaker_scope: SpeakerScope,
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
        return self.commit_assistant_heard(text, speaker_scope=speaker_scope)

    def _trim(self) -> None:
        overflow = len(self.turns) - self.max_turns
        if overflow <= 0:
            return
        dropped = self.turns[:overflow]
        self.turns = self.turns[overflow:]
        additions = [
            f"{'用户' if message.role == 'user' else '助手'}：{message.content[:240]}"
            for message in dropped
            if (
                message.speaker_scope == "owner"
                and message.role in {"user", "assistant"}
                and message.content
            )
        ]
        combined = "\n".join(
            part for part in (self.rolling_summary, *additions) if part
        )
        self.rolling_summary = combined[-self.max_rolling_summary_chars :]

    def clear_turns(self) -> None:
        """Drop the whole working context (subject-switch fence, PR-08)."""

        self.turns = []

    def reset_identity(self) -> None:
        """Atomic identity reset: turns and both summaries belong to the old
        subject and must never leak into the new one (P0-3)."""

        self.turns = []
        self.rolling_summary = ""
        self.business_summary = ""

    def context_summary(self) -> str:
        parts = []
        if self.business_summary:
            parts.append(f"当前业务状态摘要：{self.business_summary[:600]}")
        if self.rolling_summary:
            parts.append(f"较早会话原文摘录：{self.rolling_summary}")
        return "\n".join(parts)

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
        if self.rolling_summary:
            messages.append(
                {
                    "role": "system",
                    "content": f"较早会话原文摘录：{self.rolling_summary}",
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

    def tts_reference_context(
        self,
        *,
        current_user_final: str,
        speaker_scope: SpeakerScope,
        max_prior_messages: int = 3,
        max_each_chars: int = 160,
        max_total_chars: int = 320,
    ) -> tuple[str, ...]:
        """Return bounded actual-heard context without crossing a speaker boundary."""
        trailing: list[ChatMessage] = []
        for message in reversed(self.turns):
            if message.speaker_scope != speaker_scope:
                break
            trailing.append(message)
            if len(trailing) >= max_prior_messages:
                break
        current = redact_pii(current_user_final.strip())[:max_each_chars]
        current_line = f"用户：{current}" if current else ""
        remaining = max_total_chars - len(current_line)
        prior_lines: list[str] = []
        for message in trailing:
            if message.role not in {"user", "assistant"} or not message.content:
                continue
            prefix = "用户：" if message.role == "user" else "助手："
            separator_cost = int(bool(prior_lines or current_line))
            available = min(
                max_each_chars,
                remaining - len(prefix) - separator_cost,
            )
            if available <= 0:
                continue
            prior_lines.append(f"{prefix}{message.content[:available]}")
            remaining -= len(prior_lines[-1]) + separator_cost
        prior_lines.reverse()
        lines = [*prior_lines, *([current_line] if current_line else [])]
        reference = "\n".join(lines)
        return (reference,) if reference else ()
