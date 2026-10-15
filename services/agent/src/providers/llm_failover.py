"""Primary→backup chat failover with first-token and inter-token deadlines.

When enabled, one turn tries the primary model first. If no visible content
arrives before ``first_token_timeout_s``, that attempt is closed and the same
messages are retried on the backup. After the first content token, a stall of
``inter_token_timeout_s`` without a new chunk ends the turn with
``LlmUnavailableError`` and does **not** call the backup (no mid-sentence
stitch). When both pre-content attempts fail, or a mid-stream stall/error
occurs after content was already yielded, raise ``LlmUnavailableError``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal

from services.agent.src.llm_types import ChatChunk, ChatContext, ChoiceDelta
from services.agent.src.providers.provider_errors import APIConnectionError, APIError
from services.common.companion_response_safety import SAFE_UNKNOWN_REPLY
from services.common.llm_failover import backup_reply_passes_forbidden_copy

logger = logging.getLogger(__name__)

ProviderRole = Literal["primary", "backup"]


class LlmUnavailableError(APIConnectionError):
    """Chat unavailable: both pre-content attempts failed, or a mid-stream stall."""

    def __init__(self, message: str = "llm unavailable after primary and backup attempts") -> None:
        super().__init__(message, retryable=False)


@dataclass(slots=True)
class FailoverEndpoint:
    role: ProviderRole
    model: Any


class FailoverChatModel:
    """Chat model port that retries one turn on the backup after first-token timeout."""

    def __init__(
        self,
        *,
        primary: Any,
        backup: Any,
        first_token_timeout_s: float,
        inter_token_timeout_s: float,
    ) -> None:
        if first_token_timeout_s <= 0:
            raise ValueError("first_token_timeout_s must be positive")
        if inter_token_timeout_s <= 0:
            raise ValueError("inter_token_timeout_s must be positive")
        self._primary = FailoverEndpoint("primary", primary)
        self._backup = FailoverEndpoint("backup", backup)
        self._first_token_timeout_s = first_token_timeout_s
        self._inter_token_timeout_s = inter_token_timeout_s
        self.last_active_role: ProviderRole | None = None

    @property
    def model(self) -> str:
        active = self.last_active_role or "primary"
        endpoint = self._primary if active == "primary" else self._backup
        return str(getattr(endpoint.model, "model", active))

    def chat(self, *, chat_ctx: ChatContext, tools: Sequence[Any] = ()) -> FailoverChatStream:
        if tools:
            raise ValueError("the reply chat model does not offer tools")
        return FailoverChatStream(self, chat_ctx)

    def prewarm(self) -> None:
        for endpoint in (self._primary, self._backup):
            warmer = getattr(endpoint.model, "prewarm", None)
            if callable(warmer):
                warmer()

    async def aclose(self) -> None:
        for endpoint in (self._primary, self._backup):
            closer = getattr(endpoint.model, "aclose", None)
            if not callable(closer):
                continue
            result = closer()
            if asyncio.iscoroutine(result):
                await result

    async def _stream_endpoint(
        self,
        endpoint: FailoverEndpoint,
        chat_ctx: ChatContext,
        *,
        apply_forbidden_copy: bool,
    ) -> AsyncGenerator[ChatChunk, None]:
        self.last_active_role = endpoint.role
        content_yielded = False
        async with endpoint.model.chat(chat_ctx=chat_ctx, tools=[]) as stream:
            async for chunk in stream:
                text = _chunk_text(chunk)
                if apply_forbidden_copy and text and not backup_reply_passes_forbidden_copy(text):
                    if not content_yielded:
                        yield ChatChunk(
                            id=getattr(chunk, "id", "") or "backup-gated",
                            delta=ChoiceDelta(content=SAFE_UNKNOWN_REPLY),
                        )
                    return
                if text:
                    content_yielded = True
                yield chunk


class FailoverChatStream:
    def __init__(self, owner: FailoverChatModel, chat_ctx: ChatContext) -> None:
        self._owner = owner
        self._chat_ctx = chat_ctx
        self._chunks = self._run()

    def __aiter__(self) -> AsyncIterator[ChatChunk]:
        return self._chunks

    async def __aenter__(self) -> FailoverChatStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._chunks.aclose()

    async def _run(self) -> AsyncGenerator[ChatChunk, None]:
        primary_error: BaseException | None = None
        content_started = False
        try:
            async for chunk in self._attempt(self._owner._primary, apply_forbidden_copy=False):
                content_started = True
                yield chunk
            return
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except BaseException as exc:
            if content_started:
                logger.warning(
                    "llm primary mid-stream failure; ending turn without backup stitch "
                    "error_type=%s",
                    type(exc).__name__,
                )
                raise LlmUnavailableError(
                    "llm unavailable after mid-stream stall or error"
                ) from exc
            primary_error = exc
            logger.warning(
                "llm primary attempt failed; retrying on backup error_type=%s",
                type(exc).__name__,
            )
        backup_content_started = False
        try:
            async for chunk in self._attempt(self._owner._backup, apply_forbidden_copy=True):
                backup_content_started = True
                yield chunk
            return
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except BaseException as backup_error:
            if backup_content_started:
                logger.warning(
                    "llm backup mid-stream failure error_type=%s",
                    type(backup_error).__name__,
                )
                raise LlmUnavailableError(
                    "llm unavailable after mid-stream stall or error"
                ) from backup_error
            logger.warning(
                "llm backup attempt failed error_type=%s primary_error_type=%s",
                type(backup_error).__name__,
                type(primary_error).__name__ if primary_error is not None else "none",
            )
            raise LlmUnavailableError() from backup_error

    async def _attempt(
        self,
        endpoint: FailoverEndpoint,
        *,
        apply_forbidden_copy: bool,
    ) -> AsyncGenerator[ChatChunk, None]:
        iterator = self._owner._stream_endpoint(
            endpoint,
            self._chat_ctx,
            apply_forbidden_copy=apply_forbidden_copy,
        )
        first_content = False
        first_deadline = asyncio.get_running_loop().time() + self._owner._first_token_timeout_s
        try:
            while True:
                if not first_content:
                    remaining = first_deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise APIConnectionError(
                            f"{endpoint.role} first token timeout",
                            retryable=True,
                        )
                    try:
                        chunk = await asyncio.wait_for(
                            iterator.__anext__(),
                            timeout=remaining,
                        )
                    except StopAsyncIteration:
                        raise APIConnectionError(
                            f"{endpoint.role} completed without content",
                            retryable=False,
                        ) from None
                    except TimeoutError as exc:
                        raise APIConnectionError(
                            f"{endpoint.role} first token timeout",
                            retryable=True,
                        ) from exc
                else:
                    try:
                        chunk = await asyncio.wait_for(
                            iterator.__anext__(),
                            timeout=self._owner._inter_token_timeout_s,
                        )
                    except StopAsyncIteration:
                        return
                    except TimeoutError as exc:
                        raise APIConnectionError(
                            f"{endpoint.role} inter-token stall",
                            retryable=False,
                        ) from exc
                text = _chunk_text(chunk)
                if text:
                    first_content = True
                if text or first_content:
                    yield chunk
        except APIError:
            await iterator.aclose()
            raise
        except (asyncio.CancelledError, GeneratorExit):
            await iterator.aclose()
            raise
        except BaseException:
            await iterator.aclose()
            raise
        finally:
            await iterator.aclose()


def _chunk_text(chunk: Any) -> str:
    delta = getattr(chunk, "delta", None)
    if delta is None:
        return ""
    content = getattr(delta, "content", None)
    return content if isinstance(content, str) else ""


__all__ = [
    "FailoverChatModel",
    "FailoverChatStream",
    "FailoverEndpoint",
    "LlmUnavailableError",
]
