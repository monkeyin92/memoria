"""Minimal OpenAI Realtime-style event facade over the existing runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import CancellationContext

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import DuplexRuntime

RealtimeTranscriptKey = tuple[str, int, int]


class RealtimeFacadeError(ValueError):
    """Raised when an event is outside the deliberately small facade contract."""


@dataclass(frozen=True, slots=True)
class RealtimeFacadeResult:
    cancellation: CancellationContext
    events: tuple[dict[str, Any], ...]


def _fence_payload(cancellation: CancellationContext) -> dict[str, int | str]:
    return {
        "session_id": cancellation.session_id,
        "turn_id": cancellation.turn_id,
        "generation_id": cancellation.generation_id,
        "tool_epoch": cancellation.tool_epoch,
    }


def _user_text(event: dict[str, Any]) -> str:
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "message":
        raise RealtimeFacadeError("conversation.item.create requires a message item")
    if item.get("role") != "user" or not isinstance(item.get("content"), list):
        raise RealtimeFacadeError("conversation.item.create requires user text")
    parts: list[str] = []
    for content in item["content"]:
        if not isinstance(content, dict) or content.get("type") != "input_text":
            raise RealtimeFacadeError("only input_text is supported by this facade")
        text = content.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RealtimeFacadeError("input_text must be non-empty")
        parts.append(text.strip())
    if not parts:
        raise RealtimeFacadeError("conversation.item.create requires user text")
    return "\n".join(parts)


@dataclass(slots=True)
class RealtimeFacade:
    """Translate a narrow event subset without owning a second voice pipeline."""

    _assistant_snapshots: dict[RealtimeTranscriptKey, str] = field(
        default_factory=dict
    )
    _assistant_delta_blocked: set[RealtimeTranscriptKey] = field(
        default_factory=set
    )

    async def handle_client_event(
        self,
        runtime: DuplexRuntime,
        event: dict[str, Any],
    ) -> RealtimeFacadeResult:
        event_type = event.get("type") if isinstance(event, dict) else None
        if event_type == "session.update":
            cancellation = runtime.cancellation_context()
            return RealtimeFacadeResult(
                cancellation=cancellation,
                events=(
                    {
                        "type": "session.updated",
                        "session": {
                            "id": runtime.session_id,
                            "modalities": ["text"],
                            "audio_transport": "external",
                            "voice_backend": "cascade",
                        },
                        "memoria_fence": _fence_payload(cancellation),
                    },
                ),
            )
        if event_type == "response.cancel":
            fence = await runtime.on_real_interrupt(
                cause="realtime_response_cancel",
                create_user_turn=False,
                force_generation_bump=True,
            )
            cancellation = runtime.cancellation_context(fence)
            return RealtimeFacadeResult(
                cancellation=cancellation,
                events=(
                    {
                        "type": "response.done",
                        "response": {
                            "status": "cancelled",
                            "status_details": {
                                "type": "cancelled",
                                "reason": "client_cancelled",
                            },
                        },
                        "memoria_fence": _fence_payload(cancellation),
                    },
                ),
            )
        if event_type == "conversation.item.create":
            text = _user_text(event)
            route = runtime.route_user_turn(text)
            if route.should_interrupt:
                await runtime.on_real_interrupt(
                    cause="realtime_text_interrupt",
                    create_user_turn=route.enter_chat,
                    candidate_text=text,
                    utterance_route=route,
                )
            accepted, reason = runtime.accept_user_turn(
                text,
                speech_anchored=True,
            )
            fence = (
                await runtime.on_turn_committed(text)
                if accepted
                else runtime.fence
            )
            cancellation = runtime.cancellation_context(fence)
            return RealtimeFacadeResult(
                cancellation=cancellation,
                events=(
                    {
                        "type": "conversation.item.created",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "status": "completed",
                            "content": [{"type": "input_text", "text": text}],
                        },
                        "memoria": {
                            "accepted": accepted,
                            "reason": reason,
                            "intent": route.intent,
                        },
                        "memoria_fence": _fence_payload(cancellation),
                    },
                ),
            )
        if event_type in {"input_audio_buffer.append", "input_audio_buffer.commit"}:
            raise RealtimeFacadeError(
                "audio transport remains LiveKit or MiniProgramMediaGateway"
            )
        raise RealtimeFacadeError("unsupported realtime client event")

    def map_ui_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        if event.get("type") == "transcript_delta":
            speaker = event.get("speaker")
            final = event.get("final") is True
            if speaker == "user" and final:
                return {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": f"user-{event['turn_id']}",
                    "transcript": event.get("text", ""),
                    "turn_revision": event.get("turn_revision", 0),
                }
            if speaker == "assistant":
                session_id = event.get("session_id")
                turn_id = event.get("turn_id")
                generation_id = event.get("generation_id")
                text = event.get("text")
                if (
                    not isinstance(session_id, str)
                    or not session_id
                    or not isinstance(turn_id, int)
                    or not isinstance(generation_id, int)
                    or not isinstance(text, str)
                ):
                    return None
                key = (session_id, turn_id, generation_id)
                if final:
                    self._assistant_snapshots.pop(key, None)
                    self._assistant_delta_blocked.discard(key)
                    return {
                        "type": "response.audio_transcript.done",
                        "transcript": text,
                        "turn_revision": event.get("turn_revision", 0),
                    }
                if key in self._assistant_delta_blocked:
                    return None
                previous = self._assistant_snapshots.get(key, "")
                if previous and not text.startswith(previous):
                    self._assistant_snapshots.pop(key, None)
                    self._assistant_delta_blocked.add(key)
                    return None
                self._assistant_snapshots[key] = text
                delta = text[len(previous) :]
                if not delta:
                    return None
                return {
                    "type": "response.audio_transcript.delta",
                    "delta": delta,
                    "turn_revision": event.get("turn_revision", 0),
                }
            return None
        if event.get("type") == "assistant_state":
            state = event.get("state")
            if state in {"thinking", "thinking_silent", "speaking"}:
                event_type = "response.created"
            elif state in {"listening", "interrupted", "closed"}:
                event_type = "response.done"
            else:
                return None
            return {
                "type": event_type,
                "response": {
                    "status": "in_progress"
                    if event_type == "response.created"
                    else "completed",
                },
                "memoria_fence": {
                    "session_id": event.get("session_id"),
                    "turn_id": event.get("turn_id"),
                    "generation_id": event.get("generation_id"),
                    "tool_epoch": event.get("tool_epoch", 0),
                },
            }
        return None
