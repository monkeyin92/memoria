"""POST session-end child summaries to Control API; never ships raw transcripts."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from services.agent.src.session_recap_llm import (
    SessionSummaryModelConfig,
    generate_parent_session_summary,
)
from services.guardian.session_recap import (
    SessionTurn,
    draft_session_recap,
    skip_session_recap,
)

logger = logging.getLogger(__name__)

SummaryGenerator = Callable[[Sequence[SessionTurn]], Awaitable[str | None]]


class SessionRecapConfig(Protocol):
    @property
    def endpoint(self) -> str: ...

    @property
    def internal_token(self) -> str: ...

    @property
    def timeout_s(self) -> float: ...


@dataclass(frozen=True, slots=True)
class HttpSessionRecapConfig:
    endpoint: str
    internal_token: str
    timeout_s: float = 5.0


@dataclass(slots=True)
class SessionRecapSink:
    """Draft a session recap from in-memory turns, then POST only the paraphrase."""

    _config: SessionRecapConfig
    _client: httpx.AsyncClient
    _summary_generator: SummaryGenerator | None = None

    @classmethod
    def from_config(
        cls,
        config: SessionRecapConfig,
        *,
        summary_model: SessionSummaryModelConfig | None = None,
    ) -> SessionRecapSink:
        generator: SummaryGenerator | None = None
        if summary_model is not None:
            model = summary_model

            async def _generate(turns: Sequence[SessionTurn]) -> str | None:
                return await generate_parent_session_summary(turns, config=model)

            generator = _generate
        return cls(
            _config=config,
            _client=httpx.AsyncClient(timeout=config.timeout_s),
            _summary_generator=generator,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def publish_session_end(
        self,
        *,
        subject_id: str,
        resource_owner_id: str,
        session_id: str,
        turns: list[SessionTurn],
        source_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        """LLM-summarize ephemeral turns. Control API consent-gates archive writes."""

        event_id = source_event_id or f"session-end:{session_id}"
        early_skip = skip_session_recap(turns)
        summary_text: str | None = None
        model_skip: str | None = early_skip
        if early_skip is None:
            if self._summary_generator is None:
                model_skip = "summary_model_unavailable"
            else:
                summary_text = await self._summary_generator(turns)
                if summary_text is None:
                    model_skip = "summary_model_failed"
        draft = draft_session_recap(
            subject_id=subject_id,
            session_id=session_id,
            source_event_id=event_id,
            turns=turns,
            archive_consent=True,
            summary_text=summary_text,
            skipped_reason=model_skip,
        )
        if draft.skipped_reason is not None or draft.summary is None:
            logger.info(
                "session recap skipped session=%s reason=%s user_turns=%s",
                session_id,
                draft.skipped_reason or "invalid_summary",
                draft.user_turn_count,
            )
            return {
                "skipped": True,
                "reason": draft.skipped_reason or "invalid_summary",
                "summary_record_id": None,
                "archive_record_ids": [],
            }
        summary = draft.summary
        payload = {
            "subject_id": summary.subject_id,
            "resource_owner_id": resource_owner_id,
            "session_id": summary.session_id,
            "source_event_id": summary.source_event_id,
            "summary_text": summary.text,
            "archive_facts": [
                {"field": fact.field, "text": fact.text} for fact in draft.archive_facts
            ],
            "skipped_reason": None,
        }
        return await self._post(payload)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = await self._client.post(
                self._config.endpoint,
                headers={
                    "X-Memoria-Internal-Token": self._config.internal_token,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.warning("session recap post failed: %s", exc)
            return None
        if response.status_code in {200, 201}:
            try:
                return dict(response.json())
            except ValueError:
                return {"ok": True}
        logger.warning(
            "session recap rejected status=%s body=%s",
            response.status_code,
            response.text[:200],
        )
        return None


def snapshot_context_turns(context_turns: list[Any]) -> list[SessionTurn]:
    """Copy role/content pairs out of ContextManager before the transcript is cleared."""

    snapped: list[SessionTurn] = []
    for turn in context_turns:
        role = getattr(turn, "role", None)
        content = getattr(turn, "content", None)
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        snapped.append(SessionTurn(role=role, text=text))
    return snapped
