"""Test-only shortcuts for DuplexRuntime state the media path reaches indirectly."""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import AsyncIterator, Callable
from typing import Any

from livekit.agents import StopResponse
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.reply_pipeline import ReplyPipeline
from services.speaker.domain import SpeakerDecision, SpeakerPermissions, permissions_for_speaker


def bind_owner_speaker(runtime: DuplexRuntime) -> SpeakerDecision:
    """Bind the current speech epoch to an authenticated owner decision."""

    decision = SpeakerDecision(
        classification="owner",
        score=1.0,
        quality_score=1.0,
        reason_code="authenticated_text_input",
        model_version="account-auth-v1",
        template_version=None,
        profile_id=None,
        permissions=permissions_for_speaker("owner"),
    )
    runtime._speaker_class = "owner"
    runtime._speaker_decision = decision
    runtime._speaker_pcm.clear()
    return decision


def set_pending_assistant_text(runtime: DuplexRuntime, text: str) -> None:
    """Stage assistant text as if a reply were being synthesized."""

    runtime._voice_floor.update(pending_assistant_text=text)
    runtime.orchestrator.heard_tracker.set_full_text(text)


def set_floor(runtime: DuplexRuntime, **changes: Any) -> None:
    """Write runtime floor scalars through the ``VoiceFloorState`` mutation API.

    Keyword names are those of ``VoiceFloorState.update`` (e.g.
    ``assistant_speaking=True`` for the playback latch).
    """

    runtime._voice_floor.update(**changes)


def speaker_permissions(runtime: DuplexRuntime) -> SpeakerPermissions:
    """Permissions the current speaker decision grants (uncertain when unbound)."""

    decision = runtime._speaker_decision
    if decision is not None:
        return decision.permissions
    return permissions_for_speaker("owner" if runtime._speaker_class == "owner" else "uncertain")


async def commit_media_turn(agent: ReplyPipeline, message: object) -> GenerationFence:
    """Commit one endpointed audio turn the way the media session does.

    Mirrors ``MediaSessionCommit``: await the speaker decision, route the text
    through ``accept_user_turn``, then prepare the turn on the pipeline. A rejected
    turn raises ``StopResponse`` so callers can assert the rejection.
    """

    text_content = getattr(message, "text_content", None)
    text = str(text_content() if callable(text_content) else message).strip()
    runtime = agent.runtime
    await runtime.await_speaker_classification()
    accepted, reason = runtime.accept_user_turn(
        text,
        input_modality="audio",
        speech_anchored=True,
        canonical_speech_epoch=runtime.consumed_canonical_speech_epoch,
        canonical_snapshot_bound=True,
    )
    if not accepted:
        raise StopResponse(reason)
    return await agent.prepare_committed_turn(text)


class ScriptedChatModel:
    """Chat-model double for ``ReplyPipeline.language_model``.

    ``respond(chat_ctx, tools)`` returns an async iterator of chunks (or an
    awaitable of one); like ``livekit.plugins.openai.LLM`` the stream is closed when the
    ``chat()`` context exits.
    """

    def __init__(self, respond: Callable[[Any], Any]) -> None:
        self.respond = respond

    @contextlib.asynccontextmanager
    async def chat(self, *, chat_ctx: Any, tools: list[Any]) -> AsyncIterator[Any]:
        stream = self.respond(chat_ctx, tools)
        if inspect.isawaitable(stream):
            stream = await stream
        try:
            yield stream
        finally:
            close = getattr(stream, "aclose", None)
            if callable(close):
                await close()
