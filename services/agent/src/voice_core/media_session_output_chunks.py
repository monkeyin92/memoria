"""Reply-to-PCM chunk mapping for Media Voice output streaming.

``MediaOutputChunksMixin`` is composed into ``MediaOutputStreamMixin``; the
streaming loop stays in ``media_session_output_stream`` and only consumes the
async chunk iterators this module yields.  Keeping the mapping here separates
wire-order concerns (frame padding, first/final flags, AIGC marker cues and
sample-clock offsets) from the session state machine that decides when to send.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.media_session_types import (
    MediaReplyChunk,
    MediaTextSpan,
)
from services.agent.src.voice_core.media_session_types import (
    OutputWork as _OutputWork,
)

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

# Echo of the reply's tail can outlive the playback completion event by the
# ASR pipeline's segmentation lag, so the boundary that gates early follow-up
# endpoints carries a margin over the freshest uplink evidence.
_PLAYBACK_ECHO_TAIL_MARGIN_SAMPLES = 12_800  # 0.8 s at 16 kHz


def _shift_reply_chunk(chunk: MediaReplyChunk, offset: int) -> MediaReplyChunk:
    """Place a reply chunk after audio this generation already carried."""

    if not offset:
        return chunk
    return replace(
        chunk,
        source_start_sample=chunk.source_start_sample + offset,
        text_audio_start_sample=(
            None
            if chunk.text_audio_start_sample is None
            else chunk.text_audio_start_sample + offset
        ),
        text_audio_end_sample=(
            None if chunk.text_audio_end_sample is None else chunk.text_audio_end_sample + offset
        ),
        text_spans=tuple(
            MediaTextSpan(
                span.text,
                span.audio_start_sample + offset,
                span.audio_end_sample + offset,
            )
            for span in chunk.text_spans
        ),
    )


class MediaOutputChunksMixin:
    """Map one reply or output work item into paced PCM reply chunks."""

    async def _conversation_chunks(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
        text: str,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        context.output.reply_audio_origin = (fence, source_start_sample)
        async for reply in context.provider.generate_reply(context.identity, text, fence):
            yield _shift_reply_chunk(reply, source_start_sample)

    async def _output_chunks(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        if work.conversation_text is not None:
            offset = source_start_sample
            if work.lead_pcm_s16le:
                frame_samples = int(getattr(context.provider, "output_frame_samples", 480))
                if frame_samples <= 0:
                    raise RuntimeError("media provider has an invalid output frame size")
                frame_bytes = frame_samples * 2
                pcm = work.lead_pcm_s16le
                if len(pcm) % 2:
                    raise ValueError("opening AIGC cue must be 16-bit PCM")
                for start in range(0, len(pcm), frame_bytes):
                    frame = pcm[start : start + frame_bytes]
                    if len(frame) < frame_bytes:
                        frame += b"\x00" * (frame_bytes - len(frame))
                    yield MediaReplyChunk(
                        pcm_s16le=frame,
                        source_start_sample=offset,
                        first=start == 0,
                        final=False,
                        aigc_marker=True,
                    )
                    offset += frame_samples
            end = offset
            async for chunk in self._conversation_chunks(
                context, work.fence, work.conversation_text, offset
            ):
                end = chunk.source_start_sample + chunk.frame_samples
                # The closing cue rides this fence, so no source frame may
                # carry final_frame before the cue has been sent.
                yield replace(chunk, final=False) if work.tail_pcm_s16le else chunk
            if work.tail_pcm_s16le:
                async for tail in self._tail_marker_chunks(context, work.tail_pcm_s16le, end):
                    yield tail
            return
        renderer = getattr(context.provider, "generate_output", None)
        if callable(renderer):
            produced = renderer(
                context.identity,
                work.intent,
                work.fence,
                work_id=work.intent_id,
                source_start_sample=source_start_sample,
            )
            if inspect.isawaitable(produced):
                produced = await produced
            end = source_start_sample
            async for chunk in produced:
                end = chunk.source_start_sample + chunk.frame_samples
                yield replace(chunk, final=False) if work.tail_pcm_s16le else chunk
            if work.tail_pcm_s16le:
                async for tail in self._tail_marker_chunks(context, work.tail_pcm_s16le, end):
                    yield tail
            return
        if getattr(work.intent, "WhichOneof", lambda _name: None)("source") != "pcm_s16le":
            raise RuntimeError("media provider cannot render an output text source")
        pcm = bytes(getattr(work.intent, "pcm_s16le", b""))
        if not pcm or len(pcm) % 2:
            raise ValueError("output PCM source must be non-empty 16-bit audio")
        frame_samples = int(getattr(context.provider, "output_frame_samples", 480))
        if frame_samples <= 0:
            raise RuntimeError("media provider has an invalid output frame size")
        frame_bytes = frame_samples * 2
        sample = source_start_sample
        for offset in range(0, len(pcm), frame_bytes):
            frame = pcm[offset : offset + frame_bytes]
            final = offset + frame_bytes >= len(pcm) and not work.tail_pcm_s16le
            if len(frame) < frame_bytes:
                frame += b"\x00" * (frame_bytes - len(frame))
            yield MediaReplyChunk(
                pcm_s16le=frame,
                source_start_sample=sample,
                first=offset == 0,
                final=final,
            )
            sample += frame_samples
        if work.tail_pcm_s16le:
            async for tail in self._tail_marker_chunks(context, work.tail_pcm_s16le, sample):
                yield tail

    async def _tail_marker_chunks(
        self,
        context: _MediaVoiceSession,
        pcm: bytes,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        """Append the closing cue to the round's own generation.

        The device only leaves SPEAKING on this generation's
        ``generation.completed``, so the cue must not open a successor
        generation. Its frames are the last ones and carry ``final_frame``.
        """

        frame_samples = int(getattr(context.provider, "output_frame_samples", 480))
        if frame_samples <= 0:
            raise RuntimeError("media provider has an invalid output frame size")
        if not pcm or len(pcm) % 2:
            raise ValueError("closing AIGC cue must be 16-bit PCM")
        frame_bytes = frame_samples * 2
        context.aigc_closing_marker_state = "playing"
        context.aigc_closing_marker_started_at = time.monotonic()
        sample = source_start_sample
        completed = False
        try:
            for offset in range(0, len(pcm), frame_bytes):
                frame = pcm[offset : offset + frame_bytes]
                final = offset + frame_bytes >= len(pcm)
                if len(frame) < frame_bytes:
                    frame += b"\x00" * (frame_bytes - len(frame))
                yield MediaReplyChunk(
                    pcm_s16le=frame,
                    source_start_sample=sample,
                    first=False,
                    final=final,
                    aigc_marker=True,
                )
                sample += frame_samples
            completed = True
        finally:
            # A stop that cancels this stream mid-cue leaves the marker
            # unfinished; the close path then plays it in its own generation.
            context.aigc_closing_marker_state = "done" if completed else "idle"

    @staticmethod
    def _record_playback_boundary(context: _MediaVoiceSession) -> tuple[int, int]:
        """Snapshot the uplink capture boundary of a just-ended playback.

        Playback completion is the only authoritative echo boundary for the
        early follow-up endpoint: the speaker window is over in wall-clock,
        but the uplink sample position that corresponds to it has to come from
        evidence (projection watermark, accepted ASR ends, PCM sent to ASR).
        The margin keeps fail-closed semantics for echo-tail finals that were
        segmented before the speaker actually stopped.
        """

        evidence = max(
            context.projection.latest_capture_sample,
            context.last_asr_evidence_end_sample,
            int(getattr(context.asr, "last_sent_sample", 0) or 0),
        )
        playback_floor = evidence + _PLAYBACK_ECHO_TAIL_MARGIN_SAMPLES
        if playback_floor > (context.last_playback_end_sample or 0):
            context.last_playback_end_sample = playback_floor
        return evidence, context.last_playback_end_sample or playback_floor
