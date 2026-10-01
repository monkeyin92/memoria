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
from services.agent.src.voice_core import media_session_projection
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
        # playback.flush made the device wait for the replacement generation's audio. A stop is followed by
        # no reply, so that generation must be ended too, or the device stays in SPEAKING (its mic state, its
        # screen) until the next reply or the 30 s silence close, and the child's next sentence is lost
        # (2026-10-01 soak: 5 of 5 spoken stops).
        ended = [
            message.generation
            for message in self.outputs
            if message.WhichOneof("event") == "generation"
            and message.generation.action == media_pb2.GENERATION_ACTION_COMPLETE
            and message.generation.generation_id == story.generation_id + 1
        ]
        if self.identity.client_type == "device":
            [terminal_generation] = ended
            assert terminal_generation.turn_id == story.turn_id
            assert terminal_generation.reason == "voice_stop_command"
        else:
            assert ended == []  # only the firmware waits for the replacement's audio
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
async def test_a_stop_that_needed_no_device_flush_ends_no_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terminal belongs to the replacement the flush installed: without a flush there is none."""

    async def script(harness: _StoryHarness) -> None:
        monkeypatch.setattr(
            type(harness.registry),
            "_device_playback_flush_required",
            staticmethod(lambda _context, _fence: False),
        )
        story = await harness.start_story()
        await harness.audio_frames(15, final_text="停")
        await harness.wait_for(
            lambda: harness.runtime.fence.generation_id == story.generation_id + 1
        )
        await harness.settle()
        assert harness.cancel_effects() == []
        assert not [
            message
            for message in harness.outputs
            if message.WhichOneof("event") == "generation"
            and message.generation.action == media_pb2.GENERATION_ACTION_COMPLETE
        ]

    await _run_story(_device_identity("stop-without-flush"), script)


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
async def test_device_farewell_after_a_held_candidate_still_ends_the_session() -> None:
    """A farewell during playback commits on its own interval, like a stop.

    Any playback-window candidate held before it (echo, or speech without
    owner authority) merged into 「小猫咪去哪儿了 再见」, routed as chat and was
    held for missing owner authority: the story played on.
    """

    async def script(harness: _StoryHarness) -> None:
        await harness.start_story()
        context = harness.context
        await harness.audio_frames(20, final_text="小猫咪去哪儿了")
        await harness.audio_frames(15, final_text="再见")
        await harness.wait_for(harness.closed)
        assert context.standby_requested
        assert context.standby_reason == "conversation_end_explicit"
        assert harness.cancel_effects() == []
        assert harness.story.reply_calls == 1

    await _run_story(_device_identity("farewell-after-candidate"), script)


@pytest.mark.asyncio
async def test_device_farewell_partial_after_a_held_candidate_still_ends_the_session() -> None:
    async def script(harness: _StoryHarness) -> None:
        await harness.start_story()
        context = harness.context
        await harness.audio_frames(20, final_text="小猫咪去哪儿了")
        harness.story.partials[harness._audio_sequence + 9] = ("再见", harness.sample)
        await harness.audio_frames(10)
        await harness.wait_for(harness.closed)
        assert context.standby_reason == "conversation_end_explicit"
        assert harness.story.reply_calls == 1

    await _run_story(_device_identity("farewell-partial-after-candidate"), script)


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


class _TaskScriptProvider(_LongStoryProvider):
    """Adapter-shaped results: provider task epochs advance, sentence ids restart.

    FunASR numbers sentences from 1 in every task, so after a VAD-end task
    rotation the next task's first result carries sentence id 1 again.
    """

    def __init__(self, *, story_call: int = 1) -> None:
        super().__init__(story_call=story_call)
        self.by_end: dict[tuple[int, int], ASRResult] = {}

    async def ingest_audio(
        self,
        identity: SessionIdentity,
        frame: AudioFrame,
    ) -> Sequence[ASRResult]:
        scripted = self.by_end.pop(
            (frame.identity.stream_epoch, frame.capture_end_sample), None
        )
        if scripted is None:
            return await super().ingest_audio(identity, frame)
        self.audio_calls.append(frame.sequence)
        return (scripted,)


async def _frames_to(harness: _StoryHarness, end_sample: int) -> None:
    await harness.audio_frames((end_sample - harness.sample) // 320)


@pytest.mark.asyncio
async def test_device_request_after_reconnect_survives_the_next_task_reusing_its_id() -> None:
    """Field 2026-09-29 session 9c8bee20: a recognized request retired as empty.

    After a spoken stop the device came back on stream epoch 2051.  The
    request's final (FunASR sentence 1, samples 80960-109760) arrived before
    the device VAD end (voiced end 108800).  That VAD end rotated FunASR, and
    the new task's first result, sentence 1 again, erased the pending final
    from the timeline; the endpoint commit found no text and the ASR tail
    timeout discarded the turn as empty.
    """

    request = "讲一个短一点的故事。"
    provider = _TaskScriptProvider(story_call=2)
    harness = _StoryHarness(_device_identity("next-task-sentence-id"), provider)
    await harness.start()
    try:
        await harness.utterance("你好", frames=25)
        await harness.wait_for(lambda: harness.count("audio") >= 1)
        await harness.playback_ended()
        await harness.utterance("给我讲个故事", frames=25)
        await harness.wait_for(lambda: harness.count("audio") >= 2)
        story = harness.runtime.fence
        await harness.audio_frames(15, final_text="停")
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        assert story in harness.story.cancelled

        await harness.reconnect()
        epoch = harness.identity.stream_epoch
        # The production endpoint grace, so the next task can answer inside it.
        harness.registry.turn_endpoint_grace_s = 0.9
        await _frames_to(harness, 81_280)
        await harness.vad_start(81_280)
        provider.by_end[(epoch, 120_960)] = ASRResult(
            task_epoch=2,
            sentence_id="1",
            revision=2,
            capture_start_sample=80_960,
            capture_end_sample=109_760,
            text=request,
            is_final=True,
            confidence=0.9,
            stream_epoch=epoch,
        )
        await _frames_to(harness, 123_200)
        # The final is already in; the device VAD end comes after it.
        await harness.send(
            media_pb2.MediaToCore(
                vad=media_pb2.VadEvent(
                    identity=harness.wire_identity,
                    type=media_pb2.VAD_EVENT_SPEECH_END,
                    sample_position=123_200,
                    probability=0.99,
                    rms=0.05,
                    voiced_end_sample=108_800,
                )
            )
        )
        provider.by_end[(epoch, 126_400)] = ASRResult(
            task_epoch=3,
            sentence_id="1",
            revision=1,
            capture_start_sample=124_800,
            capture_end_sample=126_400,
            text="",
            is_final=False,
            stream_epoch=epoch,
        )
        await _frames_to(harness, 126_400)

        await harness.wait_for(lambda: len(_user_turn_texts(harness.context)) == 3, timeout=4.0)
        assert _user_turn_texts(harness.context)[2] == request
    finally:
        provider.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_device_stop_reusing_a_held_candidates_sentence_id_still_stops() -> None:
    """A stop from a later provider task may reuse a held candidate's id.

    The stop's own interval is committed by evicting the candidates before
    it; by id, that eviction would take the stop final with them.
    """

    provider = _TaskScriptProvider()
    harness = _StoryHarness(_device_identity("stop-reuses-sentence-id"), provider)
    await harness.start()
    try:
        story = await harness.start_story()
        epoch = harness.identity.stream_epoch
        candidate_start = harness.sample
        provider.by_end[(epoch, candidate_start + 20 * 320)] = ASRResult(
            task_epoch=2,
            sentence_id="1",
            revision=1,
            capture_start_sample=candidate_start,
            capture_end_sample=candidate_start + 20 * 320,
            text="小猫咪去哪儿了",
            is_final=True,
            confidence=0.9,
            stream_epoch=epoch,
        )
        await harness.audio_frames(20)
        stop_start = harness.sample
        provider.by_end[(epoch, stop_start + 15 * 320)] = ASRResult(
            task_epoch=3,
            sentence_id="1",
            revision=1,
            capture_start_sample=stop_start,
            capture_end_sample=stop_start + 15 * 320,
            text="停",
            is_final=True,
            confidence=0.9,
            stream_epoch=epoch,
        )
        await harness.audio_frames(15)
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        harness.assert_story_stopped_session_open(story)
    finally:
        provider.release.set()
        await harness.close()


_NIGHT_GREETING = "这么晚还醒着，我在。"
# The four characters of the field result are not logged (privacy); any
# lexical farewell that is not the greeting's own text closes the same way.
_ECHO_READ_AS_FAREWELL = "就这样吧"


class _WakeGreetingProvider(_TaskScriptProvider):
    """Speaks the allowlisted device wake greeting at connect, as on the ESP32."""

    @staticmethod
    def accept_output_intent(intent: Any) -> Any:
        return intent

    def generate_output(
        self,
        _identity: SessionIdentity,
        intent: Any,
        _fence: GenerationFence,
        *,
        work_id: str,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        _ = work_id

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=_REPLY_PCM,
                source_start_sample=source_start_sample,
                text=str(intent.tts_source),
                first=True,
                final=True,
            )

        return chunks()


def _window_final(
    epoch: int,
    *,
    sentence_id: str,
    start_sample: int,
    end_sample: int,
    text: str,
    rescue: bool = False,
    task_epoch: int = 2,
) -> ASRResult:
    return ASRResult(
        task_epoch=task_epoch,
        sentence_id=sentence_id,
        revision=1,
        capture_start_sample=start_sample,
        capture_end_sample=end_sample,
        text=text,
        is_final=True,
        confidence=0.9,
        stream_epoch=epoch,
        rescue_synthesized=rescue,
        rescue_rms=4_122 if rescue else None,
        rescue_peak_abs=24_000 if rescue else None,
    )


async def _run_night_greeting(
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
    script: Any,
) -> None:
    """The device wakes at night: the greeting plays while the uplink streams."""

    monkeypatch.setattr(
        media_session_projection,
        "device_wake_phrase",
        lambda *_args, **_kwargs: _NIGHT_GREETING,
    )
    provider = _WakeGreetingProvider()
    harness = _StoryHarness(_device_identity(session_id), provider)
    await harness.start()
    try:
        # The first uplink frame opens the media session, which greets.
        await harness.audio_frames(1)
        await harness.wait_for(lambda: harness.count("audio") >= 1)
        assert harness.runtime.assistant_speaking
        assert harness.context.output.assistant_text == _NIGHT_GREETING
        await script(harness, provider)
    finally:
        provider.release.set()
        await harness.close()


async def _end_greeting_playback(harness: _StoryHarness, *, uplink_end: int) -> int:
    """The uplink reaches ``uplink_end``, then the greeting's playback ack."""

    await _frames_to(harness, uplink_end)
    assert harness.runtime.assistant_speaking
    await harness.playback_ended()
    assert not harness.runtime.assistant_speaking
    boundary = harness.context.last_playback_end_sample
    assert boundary is not None
    return int(boundary)


def _assert_session_open(harness: _StoryHarness, context: Any) -> None:
    assert not harness.closed()
    assert not context.standby_requested and not context.closed
    assert context.pending.conversation_close_endpoint_pinned is None
    assert context.pending.turn_endpoint_sample is None


async def _owner_farewell_after(
    harness: _StoryHarness,
    provider: _TaskScriptProvider,
    boundary: int,
) -> None:
    """The owner says 「再见」 on uplink audio wholly past the playback boundary."""

    epoch = harness.identity.stream_epoch
    start = boundary + 3_200
    await _frames_to(harness, start)
    provider.by_end[(epoch, start + 9_600)] = _window_final(
        epoch, sentence_id="3", start_sample=start, end_sample=start + 9_600, text="再见"
    )
    await _frames_to(harness, start + 9_600)
    await harness.wait_for(harness.closed)


@pytest.mark.asyncio
async def test_device_greeting_echo_rescued_as_farewell_does_not_end_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Field 2026-09-29 session addf5e00: the wake greeting ended its own session.

    With device VAD suppressed, FunASR re-transcribed the greeting's echo as
    「这么晚还。」 and SenseVoice rescued the whole provider task (samples
    17280-81280, wholly inside the playback window) as four characters.
    Overlapping that final, the rescue went to overlap recovery, whose
    lookups resolved just after the playback ack: the audible-playback hold
    no longer applied, the farewell pinned an endpoint, and the commit's
    close classifier read the window's text as a farewell 0.15 s after the
    greeting.  The owner's real farewell after the boundary still ends the
    session at once.
    """

    async def script(harness: _StoryHarness, provider: _WakeGreetingProvider) -> None:
        context = harness.context
        epoch = harness.identity.stream_epoch
        ack_heard = asyncio.Event()

        async def live_lookup_verdict(text: str) -> bool:
            if text == _ECHO_READ_AS_FAREWELL:
                await ack_heard.wait()  # the classifier answers after the ack
            return False

        async def close_verdict(text: str) -> bool:
            return _ECHO_READ_AS_FAREWELL in text

        harness.runtime.set_live_lookup_semantic_resolver(live_lookup_verdict)
        harness.runtime.set_conversation_close_semantic_resolver(close_verdict)
        provider.by_end[(epoch, 69_760)] = _window_final(
            epoch, sentence_id="2", start_sample=53_760, end_sample=69_760, text="这么晚还。"
        )
        provider.by_end[(epoch, 81_280)] = _window_final(
            epoch,
            sentence_id="0",
            start_sample=17_280,
            end_sample=81_280,
            text=_ECHO_READ_AS_FAREWELL,
            rescue=True,
        )
        boundary = await _end_greeting_playback(harness, uplink_end=90_880)
        assert 81_280 < boundary
        ack_heard.set()
        await asyncio.sleep(0.1)
        await harness.settle()
        _assert_session_open(harness, context)
        assert _user_turn_texts(context) == []
        assert provider.reply_calls == 0

        await _owner_farewell_after(harness, provider, boundary)
        assert context.standby_reason == "conversation_end_explicit"
        assert provider.reply_calls == 0

    await _run_night_greeting(monkeypatch, "greeting-echo-rescued-farewell", script)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["lexical", "semantic"])
async def test_device_greeting_window_farewell_final_after_the_ack_is_held(
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
) -> None:
    """The same greeting-window interval offered as a final just after the ack.

    Its audio ends before the playback boundary: whatever its text reads, it
    is the greeting's time on the uplink and cannot close the session.
    """

    text = _ECHO_READ_AS_FAREWELL if verdict == "lexical" else "早点睡吧"

    async def script(harness: _StoryHarness, provider: _WakeGreetingProvider) -> None:
        context = harness.context
        epoch = harness.identity.stream_epoch

        async def close_verdict(candidate: str) -> bool:
            return "早点睡" in candidate

        harness.runtime.set_conversation_close_semantic_resolver(close_verdict)
        boundary = await _end_greeting_playback(harness, uplink_end=90_880)
        assert 81_280 < boundary
        provider.by_end[(epoch, 91_200)] = _window_final(
            epoch, sentence_id="2", start_sample=17_280, end_sample=81_280, text=text
        )
        await _frames_to(harness, 91_200)
        await asyncio.sleep(0.1)
        await harness.settle()
        _assert_session_open(harness, context)

    await _run_night_greeting(monkeypatch, f"greeting-window-farewell-{verdict}", script)


@pytest.mark.asyncio
async def test_device_farewell_after_the_greeting_boundary_still_ends_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner speech past the playback boundary is no echo: it closes at once."""

    async def script(harness: _StoryHarness, provider: _WakeGreetingProvider) -> None:
        context = harness.context
        boundary = await _end_greeting_playback(harness, uplink_end=90_880)
        await _owner_farewell_after(harness, provider, boundary)
        assert context.standby_reason == "conversation_end_explicit"

    await _run_night_greeting(monkeypatch, "greeting-then-farewell", script)


@pytest.mark.asyncio
async def test_device_rescue_farewell_during_story_is_held() -> None:
    """A rescue spans its whole provider task, the reply's echo included.

    Heard while the reply plays, its farewell text is no owner command; the
    owner's own 「再见」 still ends the session on its own interval.
    """

    provider = _TaskScriptProvider()
    harness = _StoryHarness(_device_identity("rescue-farewell-during-story"), provider)
    await harness.start()
    try:
        story = await harness.start_story()
        context = harness.context
        epoch = harness.identity.stream_epoch
        rescue_start = harness.sample
        provider.by_end[(epoch, rescue_start + 20 * 320)] = _window_final(
            epoch,
            sentence_id="0",
            start_sample=rescue_start,
            end_sample=rescue_start + 20 * 320,
            text=_ECHO_READ_AS_FAREWELL,
            rescue=True,
            task_epoch=1,
        )
        await harness.audio_frames(20)
        await harness.assert_story_still_playing(story)
        _assert_session_open(harness, context)

        await harness.audio_frames(15, final_text="再见")
        await harness.wait_for(harness.closed)
        assert context.standby_reason == "conversation_end_explicit"
    finally:
        provider.release.set()
        await harness.close()
