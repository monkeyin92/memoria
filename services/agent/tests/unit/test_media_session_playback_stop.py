"""A spoken 「停」 while a device reply plays (field 2026-09-29, ESP32 story).

The device negotiated ``interrupt_assist`` with signed
``allowed_barge_in=["button","keyword"]``: firmware and Edge suppress every
playback-window vad.start, so the uplink carries audio and cloud-ASR finals but
no VAD edge.  Three 「停」 finals then did nothing until the owner tapped the
screen.  These scenarios drive the real registry/runtime over the public gRPC
stream with a reply that keeps playing until it is stopped.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.state_machine import ConversationState
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_protocol import (
    DEVICE_POST_PLAYBACK_HOLDOFF_S,
    AudioFrame,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import MediaReplyChunk
from services.agent.src.voice_core.speech_timeline import ASRResult
from services.agent.tests.unit.media_session_support import (
    _accept_media_asr_decision,
    _accept_media_asr_final,
    _feed_playback_window_finals,
    _user_turn_texts,
    _wait_until,
)
from services.agent.tests.unit.test_media_session_golden import (
    _REPLY_PCM,
    _h5_identity,
    _Harness,
    _is_action,
    _proto_identity,
    _ScriptedProvider,
)

_STORY_OPENING = "从前有一座山，山里有一座庙。"


class _LongStoryProvider(_ScriptedProvider):
    """A story reply that keeps the floor until it is cancelled.

    The first phrase streams at once; the rest waits, so the reply stays
    audible for as long as the scenario needs.  Frames may also carry a
    scripted partial result.
    """

    def __init__(self, opening: str = _STORY_OPENING, *, story_call: int = 1) -> None:
        super().__init__()
        self.opening = opening
        self.story_call = story_call
        self.partials: dict[int, tuple[str, int]] = {}
        self.release = asyncio.Event()

    async def ingest_audio(
        self,
        identity: SessionIdentity,
        frame: AudioFrame,
    ) -> Sequence[ASRResult]:
        scripted = self.partials.pop(frame.sequence, None)
        if scripted is None:
            return await super().ingest_audio(identity, frame)
        self.audio_calls.append(frame.sequence)
        text, start_sample = scripted
        return (
            ASRResult(
                task_epoch=1,
                sentence_id=f"partial-{frame.sequence}",
                revision=1,
                capture_start_sample=start_sample,
                capture_end_sample=frame.capture_end_sample,
                text=text,
                is_final=False,
                confidence=0.9,
                stream_epoch=frame.identity.stream_epoch,
            ),
        )

    def generate_reply(
        self,
        _identity: SessionIdentity,
        _user_text: str,
        _fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.reply_calls += 1
        opening = self.opening if self.reply_calls == self.story_call else "好的。"
        release = self.release if self.reply_calls == self.story_call else None

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=_REPLY_PCM,
                source_start_sample=0,
                text=opening,
                first=True,
                final=release is None,
            )
            if release is None:
                return
            await release.wait()
            yield MediaReplyChunk(
                pcm_s16le=_REPLY_PCM,
                source_start_sample=160,
                text="庙里有一个老和尚。",
                final=True,
            )

        return chunks()


def _device_identity(session_id: str) -> SessionIdentity:
    return SessionIdentity(
        session_id,
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
        audio_mode="interrupt_assist",
    )


class _StoryHarness(_Harness):
    """Production device runtime: input guard, interrupt_assist, no owner proof."""

    def __init__(self, identity: SessionIdentity, provider: _LongStoryProvider) -> None:
        super().__init__(identity, provider)
        self.story = provider
        if identity.client_type == "device":
            self.runtime = DuplexRuntime.create(
                session_id=identity.session_id,
                input_guard_enabled=True,
                barge_in_enabled=True,
                capture_release_holdoff_s=DEVICE_POST_PLAYBACK_HOLDOFF_S,
            )

    @property
    def context(self) -> Any:
        return self.registry.session_state(self.identity.session_id)

    def cancel_effects(self) -> list[Any]:
        return [
            message.realtime_effect
            for message in self.outputs
            if message.WhichOneof("event") == "realtime_effect"
            and message.realtime_effect.effect_kind
            == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
        ]

    def closed(self) -> bool:
        return self.count(
            "state", lambda state: state.state == media_pb2.CONVERSATION_STATE_CLOSED
        ) > 0

    def deliveries_for(self, fence: GenerationFence) -> list[dict[str, Any]]:
        return [
            delivery
            for delivery in self.deliveries
            if delivery["turn_id"] == fence.turn_id
            and delivery["generation_id"] == fence.generation_id
        ]

    async def reconnect(self) -> None:
        """The device's WSS was closed: it returns on the next stream epoch.

        Its uplink sample clock restarts at 0 (firmware ResetSessionState),
        exactly as ``stream_epoch=2034 sample=0`` in the 2026-09-29 log.
        """

        await self._requests.put(None)
        if self._reader is not None:
            await asyncio.wait_for(self._reader, timeout=2)
        self.identity = dataclasses.replace(
            self.identity, stream_epoch=self.identity.stream_epoch + 1
        )
        self.wire_identity = _proto_identity(self.identity)
        self._requests = asyncio.Queue()
        self._reader = asyncio.create_task(self._read(), name="story-reader-reconnected")
        accepted = self.count("accepted")
        await self.send(
            media_pb2.MediaToCore(
                hello=media_pb2.SessionHello(
                    identity=self.wire_identity,
                    interaction_authority=media_pb2.INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE,
                )
            )
        )
        await self.wait_for(lambda: self.count("accepted") == accepted + 1)
        self.sample = 0
        self._audio_sequence = 0

    async def start_story(self) -> GenerationFence:
        """The owner asks for a story; its first phrase is on the speaker."""

        await self.utterance("给我讲个故事", frames=25)
        await self.wait_for(lambda: self.count("audio") >= 1)
        assert self.runtime.assistant_speaking
        assert self.runtime.orchestrator.state is ConversationState.SPEAKING
        return self.runtime.fence

    async def assert_story_still_playing(self, story: GenerationFence) -> None:
        await asyncio.sleep(0.05)
        await self.settle()
        assert self.cancel_effects() == []
        assert self.runtime.fence.matches(story)
        assert self.runtime.orchestrator.state is ConversationState.SPEAKING
        reply_task = self.context.output.reply_task
        assert reply_task is not None and not reply_task.done()
        assert story not in self.story.cancelled

    def assert_story_stopped_session_open(self, story: GenerationFence) -> None:
        [effect] = self.cancel_effects()
        assert (effect.turn_id, effect.generation_id) == (
            story.turn_id,
            story.generation_id + 1,
        )
        assert effect.source_event_id == "voice_stop_command"
        assert story in self.story.cancelled
        terminal = [d for d in self.deliveries_for(story) if d["terminal_event"]]
        assert [d["terminal_event"] for d in terminal] == ["preempted"]
        assert self.runtime.fence.generation_id == story.generation_id + 1
        assert self.runtime.orchestrator.state is ConversationState.LISTENING
        assert not self.runtime.assistant_speaking
        assert self.phases[-1]["to"] == "listening"
        # A stop is a control turn: no LLM turn, no second reply, no close.
        assert self.story.reply_calls == 1
        assert _user_turn_texts(self.context) == ["给我讲个故事"]
        assert not self.closed()
        assert not self.context.standby_requested and not self.context.closed


async def _run_story(
    identity: SessionIdentity,
    script: Any,
    *,
    opening: str = _STORY_OPENING,
    story_call: int = 1,
) -> None:
    provider = _LongStoryProvider(opening, story_call=story_call)
    harness = _StoryHarness(identity, provider)
    await harness.start()
    try:
        await script(harness)
    finally:
        provider.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_device_stop_word_without_vad_stops_the_story_and_keeps_listening() -> None:
    """Field 2026-09-29 session ef14272f: FunASR finals 「停」 x3, nothing happened."""

    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        # No VAD edge during playback: only uplink audio carrying the final.
        await harness.audio_frames(15, final_text="停")
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        harness.assert_story_stopped_session_open(story)

        # The conversation continues: the next question gets its own reply.
        await harness.utterance("换一个故事吧", frames=25)
        await harness.wait_for(lambda: harness.story.reply_calls == 2)
        assert _user_turn_texts(harness.context) == ["给我讲个故事", "换一个故事吧"]
        assert not harness.closed()

    await _run_story(_device_identity("stop-word-story"), script)


@pytest.mark.asyncio
async def test_device_stop_word_commits_without_earlier_playback_candidates() -> None:
    """Field 2026-09-29 session d239d158: 「……停」 merged, routed as chat, held."""

    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        # Uplink speech during the story that no endpoint may ever pick up:
        # without owner proof it is a held candidate, not a user turn.
        await harness.audio_frames(20, final_text="小猫咪去哪儿了")
        await harness.audio_frames(15, final_text="停")
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        harness.assert_story_stopped_session_open(story)

    await _run_story(_device_identity("stop-word-after-candidate"), script)


@pytest.mark.asyncio
async def test_device_stop_waits_for_its_final_not_the_partial() -> None:
    """A partial 「等等」 may still grow into a question; only the final stops."""

    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        harness.story.partials[harness._audio_sequence + 9] = ("等等", harness.sample)
        await harness.audio_frames(10)
        await harness.assert_story_still_playing(story)
        await harness.audio_frames(10, final_text="等等")
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        harness.assert_story_stopped_session_open(story)

    await _run_story(_device_identity("stop-word-partial"), script)


@pytest.mark.asyncio
async def test_device_repeated_stop_word_stops_the_story() -> None:
    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        await harness.audio_frames(15, final_text="停停")
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        harness.assert_story_stopped_session_open(story)

    await _run_story(_device_identity("stop-word-repeated"), script)


@pytest.mark.asyncio
async def test_device_wait_that_grows_into_a_question_does_not_stop() -> None:
    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        harness.story.partials[harness._audio_sequence + 9] = ("等一下", harness.sample)
        await harness.audio_frames(10)
        await harness.audio_frames(10, final_text="等一下我想问下周三")
        await harness.assert_story_still_playing(story)
        assert not harness.cancel_effects()

    await _run_story(_device_identity("stop-word-grows"), script)


@pytest.mark.asyncio
async def test_device_story_saying_stop_does_not_interrupt_itself() -> None:
    """The reply's own 「停！」 on the open microphone is echo, not a command."""

    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        await harness.audio_frames(15, final_text="停！")
        await harness.assert_story_still_playing(story)
        assert harness.context.pending.turn_endpoint_sample is None

    await _run_story(
        _device_identity("stop-word-echo"),
        script,
        opening="小兔子跑到河边，大喊一声：停！",
    )


@pytest.mark.asyncio
async def test_device_ordinary_speech_during_story_stays_held() -> None:
    """Only a lexical stop gets an early endpoint; other speech is not widened."""

    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        await harness.audio_frames(20, final_text="我们下午几点出发")
        await harness.assert_story_still_playing(story)
        assert harness.context.pending.turn_endpoint_sample is None
        # Even endpointed by VAD, unverified speech during playback is held.
        await harness.utterance("我们下午几点出发呀", frames=20)
        await harness.assert_story_still_playing(story)
        assert harness.story.reply_calls == 1
        assert _user_turn_texts(harness.context) == ["给我讲个故事"]

    await _run_story(_device_identity("ordinary-speech-held"), script)


@pytest.mark.asyncio
async def test_device_misheard_stop_read_as_farewell_cannot_block_the_next_stop() -> None:
    """Simulated trials 2026-09-29: the owner's 「停」 heard as 「行」 mid-story.

    Not a lexical stop, the final went to the semantic close classifier, which
    read it as a farewell and pinned ``media early conversation-close endpoint
    source=semantic_final``.  During playback that turn can only be held, but
    its endpoint stood while the commit resolved the merged playback-window
    text: the owner's clear 「停」 found it in place, was refused, and was
    cleared with the held turn, so the story played on to its end.
    """

    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        merged_verdict = asyncio.Event()

        async def close_verdict(text: str) -> bool:
            if "小猫咪" in text and "行" in text:
                await merged_verdict.wait()  # classifier latency on merged text
            return "行" in text

        harness.runtime.set_conversation_close_semantic_resolver(close_verdict)
        try:
            await harness.audio_frames(20, final_text="小猫咪去哪儿了")
            await harness.audio_frames(15, final_text="行。")
            assert harness.context.pending.turn_endpoint_sample is None
            await harness.audio_frames(15, final_text="停")
        finally:
            merged_verdict.set()
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        harness.assert_story_stopped_session_open(story)

    await _run_story(_device_identity("misheard-stop-as-farewell"), script)


@pytest.mark.asyncio
async def test_device_story_saying_goodbye_does_not_end_itself() -> None:
    """The reply's own 「再见！」 on the open microphone is echo, not a farewell."""

    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        await harness.audio_frames(15, final_text="再见！")
        await harness.assert_story_still_playing(story)
        assert harness.context.pending.turn_endpoint_sample is None
        assert not harness.closed()
        # Nothing is pinned, so the owner's real stop still ends the story.
        await harness.audio_frames(15, final_text="停")
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        harness.assert_story_stopped_session_open(story)

    await _run_story(
        _device_identity("farewell-echo"),
        script,
        opening="小熊挥挥手，对月亮说：再见！",
    )


@pytest.mark.asyncio
async def test_device_clock_question_during_story_cannot_pin_over_the_stop(
    device_media_session: Any,
) -> None:
    """A stable clock-fact partial during playback is held like its final.

    Its pinned endpoint would refuse the owner's 「停一下」 while the held
    commit ran, exactly like the misread farewell above.
    """

    window = await device_media_session("clock-partial-during-playback")
    for revision in (1, 2):
        await _accept_media_asr_decision(
            window.registry,
            window.identity,
            sentence_id="clock-partial",
            start_sample=16_000,
            end_sample=24_000 + revision,
            text="现在几点了",
            revision=revision,
            is_final=False,
        )
        if revision == 1:
            await asyncio.sleep(0.65)
    assert window.context.pending.turn_endpoint_sample is None

    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="owner-stop",
        start_sample=40_000,
        end_sample=46_400,
        text="停一下",
    )
    assert window.context.pending.turn_endpoint_sample == 46_400


@pytest.mark.asyncio
async def test_device_question_begun_before_playback_end_is_answered_without_vad() -> None:
    """Field 2026-09-29 session 6b38ba46, over the public stream.

    The owner starts the next question 0.5 s before the reply's playback ack
    and talks on for 3.5 s.  The device sent no VAD edge (suppressed during
    playback), so only the final itself can endpoint the question.
    """

    async def script(harness: _StoryHarness) -> None:
        await harness.utterance("你好", frames=25)
        await harness.wait_for(lambda: harness.count("audio") >= 1)
        await harness.audio_frames(50)  # the reply plays, echo on the uplink
        owner_start = harness.sample
        await harness.audio_frames(25)
        await harness.playback_ended()
        boundary = harness.context.last_playback_end_sample
        assert boundary is not None and owner_start < boundary
        question = "那我明天出门要带伞吗"
        harness.story.finals[harness._audio_sequence + 174] = (question, owner_start)
        await harness.audio_frames(175)
        await harness.wait_for(lambda: harness.story.reply_calls == 2, timeout=4.0)
        assert _user_turn_texts(harness.context) == ["你好", question]

    await _run_story(_device_identity("question-before-playback-end"), script, story_call=0)


@pytest.mark.asyncio
async def test_device_farewell_during_story_still_ends_the_session() -> None:
    async def script(harness: _StoryHarness) -> None:
        await harness.start_story()
        context = harness.context
        await harness.audio_frames(15, final_text="再见")
        await harness.wait_for(harness.closed)
        assert context.standby_requested
        assert context.standby_reason == "conversation_end_explicit"
        assert harness.cancel_effects() == []
        assert harness.story.reply_calls == 1

    await _run_story(_device_identity("farewell-during-story"), script)


@pytest.mark.asyncio
async def test_h5_vad_endpointed_stop_word_stops_the_reply() -> None:
    """The commit path itself used to report listening while the reply played on."""

    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        await harness.utterance("停一下", frames=15)
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        harness.assert_story_stopped_session_open(story)
        assert harness.count(
            "generation", _is_action(media_pb2.GENERATION_ACTION_START)
        ) == 1

    await _run_story(_h5_identity("h5-stop-word"), script)


@pytest.mark.asyncio
async def test_h5_reply_echo_of_stop_word_is_not_a_command() -> None:
    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        await harness.utterance("停", frames=15)
        await harness.assert_story_still_playing(story)

    await _run_story(
        _h5_identity("h5-stop-word-echo"),
        script,
        opening="小兔子跑到河边，大喊一声：停！",
    )


@pytest.mark.asyncio
async def test_device_stop_after_playback_window_finals_is_its_own_turn(
    device_media_session: Any,
) -> None:
    """Epoch-1955 overlap window: five finals buffered while a reply owned output.

    Merged with that window the stop would read 「AAA BBBB … 停一下」, route
    as chat and be held for missing owner authority.  It commits alone and
    reaches the Router as the stop command it is.
    """

    window = await device_media_session("stop-after-overlap-window")
    await _feed_playback_window_finals(window)
    assert window.context.pending.turn_start_sample == 158_560
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="owner-stop",
        start_sample=400_000,
        end_sample=406_400,
        text="停一下",
    )
    await _wait_until(lambda: window.context.pending.turn_start_sample is None, timeout=2.0)
    metrics = window.context.runtime.orchestrator.metrics
    assert metrics.get("guarded_user_input_total", {"reason": "interrupt_command_only"}) == 1
    assert window.provider.prepared == []
    assert _user_turn_texts(window.context) == []
    assert not window.context.standby_requested


@pytest.mark.asyncio
async def test_device_utterance_after_stop_and_reconnect_is_one_answered_turn() -> None:
    """Field 2026-09-29 session 2eb6ba0f: after the spoken stop the device came back.

    Edge closed the WSS on the flushed generation's ``playback.ended`` and the
    device reconnected on stream epoch 2034, its uplink clock back at 0.  The
    previous epoch's playback boundary (90560) then endpointed the new
    request as a post-playback follow-up.  Sample positions do not cross an
    epoch: the utterance must commit whole at its VAD end and be answered.
    """

    async def script(harness: _StoryHarness) -> None:
        # The greeting plays out, leaving a playback boundary in epoch 1.
        await harness.utterance("你好", frames=25)
        await harness.wait_for(lambda: harness.count("audio") >= 1)
        await harness.playback_ended()
        assert harness.context.last_playback_end_sample is not None
        await harness.utterance("给我讲个故事", frames=25)
        await harness.wait_for(lambda: harness.count("audio") >= 2)
        story = harness.runtime.fence
        await harness.audio_frames(15, final_text="停")
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        assert story in harness.story.cancelled

        await harness.reconnect()
        # Past the old boundary on the new clock; the owner pauses mid-request
        # longer than the follow-up grace while the device VAD stays open.
        await harness.audio_frames(75)
        await harness.vad_start(harness.sample)
        await harness.audio_frames(10, final_text="讲一个")
        await asyncio.sleep(1.5)
        await harness.audio_frames(15, final_text="短一点的故事")
        await harness.vad_end(harness.sample)
        await harness.wait_for(lambda: harness.story.reply_calls == 3)

        turns = _user_turn_texts(harness.context)
        assert turns[:2] == ["你好", "给我讲个故事"]
        assert len(turns) == 3
        assert "讲一个" in turns[2] and "短一点的故事" in turns[2]
        assert not harness.closed()

    await _run_story(_device_identity("stop-then-reconnect"), script, story_call=2)
