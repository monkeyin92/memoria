"""Provider-neutral handler requests for the cascade pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from services.agent.src.contracts.ids import CancellationContext, GenerationFence


@dataclass(frozen=True, slots=True)
class LanguageModelRequest:
    user_text: str
    cancellation: CancellationContext


@dataclass(frozen=True, slots=True)
class SpeechSynthesisRequest:
    phrases: tuple[str, ...]
    cancellation: CancellationContext
    cancel_event: asyncio.Event


class LanguageModelHandler(Protocol):
    def stream(self, request: LanguageModelRequest) -> AsyncIterator[str]: ...


class SpeechSynthesisHandler(Protocol):
    async def synthesize(self, request: SpeechSynthesisRequest) -> Any: ...


LegacyLLMStream = Callable[[str, GenerationFence], AsyncIterator[str]]
LegacyTTSSynthesis = Callable[
    [list[str], GenerationFence, asyncio.Event],
    Any,
]


@dataclass(frozen=True, slots=True)
class CallableLanguageModelHandler:
    """Adapter for existing async-token callables."""

    stream_fn: LegacyLLMStream

    def stream(self, request: LanguageModelRequest) -> AsyncIterator[str]:
        return self.stream_fn(request.user_text, request.cancellation.fence)


@dataclass(frozen=True, slots=True)
class CallableSpeechSynthesisHandler:
    """Adapter for existing fenced TTS callables."""

    synthesize_fn: LegacyTTSSynthesis

    async def synthesize(self, request: SpeechSynthesisRequest) -> Any:
        return await self.synthesize_fn(
            list(request.phrases),
            request.cancellation.fence,
            request.cancel_event,
        )
