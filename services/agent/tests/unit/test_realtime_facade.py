from __future__ import annotations

import json
from pathlib import Path

import pytest
from services.agent.src.contracts.ids import CancellationContext, GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.realtime_facade import (
    RealtimeFacade,
    RealtimeFacadeError,
)
from services.agent.src.orchestration.utterance_router import (
    UtteranceRoute,
    route_utterance,
)


def test_realtime_facade_contract_has_no_second_transport() -> None:
    contract = json.loads(
        (
            Path(__file__).resolve().parents[4]
            / "packages"
            / "contracts"
            / "realtime-facade.json"
        ).read_text(encoding="utf-8")
    )
    assert contract["runtime_owner"] == "DuplexRuntime"
    assert contract["intent_owner"] == "UtteranceRouter"
    assert contract["transport_exposed"] is False


@pytest.mark.asyncio
async def test_realtime_facade_does_not_advertise_protocol_audio_transport() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-session")

    result = await RealtimeFacade().handle_client_event(
        runtime,
        {"type": "session.update"},
    )

    assert result.events[0]["session"]["modalities"] == ["text"]
    assert result.events[0]["session"]["audio_transport"] == "external"


@pytest.mark.asyncio
async def test_realtime_facade_commits_text_through_existing_runtime() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-session")
    await runtime.orchestrator.ready()
    facade = RealtimeFacade()

    result = await facade.handle_client_event(
        runtime,
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "今天过得怎么样"}],
            },
        },
    )

    assert result.cancellation.fence == runtime.fence
    assert result.events[0]["memoria"]["accepted"] is True
    assert runtime.orchestrator.context.turns[-1].content == "今天过得怎么样"


@pytest.mark.asyncio
async def test_realtime_facade_passes_the_single_router_decision_to_interrupt() -> None:
    route = route_utterance("等一下，我想问一个问题")
    seen: list[object] = []

    class Runtime:
        session_id = "realtime-session"
        fence = GenerationFence(session_id, 1, 1, 0)

        def cancellation_context(
            self,
            fence: GenerationFence | None = None,
        ) -> CancellationContext:
            return CancellationContext.capture(fence or self.fence)

        def route_user_turn(self, _text: str) -> UtteranceRoute:
            return route

        async def on_real_interrupt(self, **kwargs: object) -> GenerationFence:
            seen.append(kwargs.get("utterance_route"))
            return self.fence

        def accept_user_turn(
            self,
            _text: str,
            **_kwargs: object,
        ) -> tuple[bool, None]:
            return True, None

        async def on_turn_committed(self, _text: str) -> GenerationFence:
            return self.fence

    await RealtimeFacade().handle_client_event(
        Runtime(),  # type: ignore[arg-type]
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "等一下，我想问一个问题"}
                ],
            },
        },
    )

    assert seen == [route]


@pytest.mark.asyncio
async def test_realtime_facade_interrupt_then_chat_commits_one_user_turn() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-session")
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("先回答这个问题")
    old_fence = runtime.fence
    text = "等一下，我想问一个问题"

    result = await RealtimeFacade().handle_client_event(
        runtime,
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
    )

    matching_turns = [
        turn
        for turn in runtime.orchestrator.context.turns
        if turn.role == "user" and turn.content == text
    ]
    assert len(matching_turns) == 1
    assert runtime.fence.turn_id == old_fence.turn_id + 1
    assert runtime.fence.generation_id == old_fence.generation_id + 2
    assert result.events[0]["memoria"]["intent"] == "interrupt_then_chat"


@pytest.mark.asyncio
async def test_realtime_facade_cancel_advances_the_existing_generation() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-session")
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("先回答这个问题")
    old = runtime.fence

    result = await RealtimeFacade().handle_client_event(
        runtime,
        {"type": "response.cancel"},
    )

    assert result.cancellation.generation_id == old.generation_id + 1
    assert result.events[0]["response"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_realtime_facade_rejects_a_second_audio_transport() -> None:
    runtime = DuplexRuntime.create()
    with pytest.raises(RealtimeFacadeError, match="audio transport"):
        await RealtimeFacade().handle_client_event(
            runtime,
            {"type": "input_audio_buffer.append", "audio": "base64"},
        )


def test_realtime_facade_maps_revisioned_transcripts() -> None:
    event = RealtimeFacade().map_ui_event(
        {
            "type": "transcript_delta",
            "session_id": "session",
            "speaker": "assistant",
            "text": "修订后的回答",
            "final": True,
            "turn_id": 2,
            "generation_id": 3,
            "turn_revision": 4,
        }
    )

    assert event == {
        "type": "response.audio_transcript.done",
        "transcript": "修订后的回答",
        "turn_revision": 4,
    }


def test_realtime_facade_converts_snapshots_to_append_only_deltas() -> None:
    facade = RealtimeFacade()
    base = {
        "type": "transcript_delta",
        "session_id": "session",
        "speaker": "assistant",
        "final": False,
        "turn_id": 2,
        "generation_id": 3,
    }

    first = facade.map_ui_event(
        {**base, "text": "你", "turn_revision": 1}
    )
    second = facade.map_ui_event(
        {**base, "text": "你好", "turn_revision": 2}
    )
    corrected = facade.map_ui_event(
        {**base, "text": "您好", "turn_revision": 3}
    )
    final = facade.map_ui_event(
        {
            **base,
            "text": "您好",
            "final": True,
            "turn_revision": 4,
        }
    )

    assert first == {
        "type": "response.audio_transcript.delta",
        "delta": "你",
        "turn_revision": 1,
    }
    assert second == {
        "type": "response.audio_transcript.delta",
        "delta": "好",
        "turn_revision": 2,
    }
    assert corrected is None
    assert final == {
        "type": "response.audio_transcript.done",
        "transcript": "您好",
        "turn_revision": 4,
    }
