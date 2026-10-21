"""Volcengine Doubao streaming ASR session (primary provider).

One WebSocket connection carries one recognition task, per the ``sauc``
protocol: a full client request with the JSON configuration, then audio-only
packets, then a negative packet that ends the task. The service's last-flagged
full server response is the task boundary. A VAD segment therefore maps to one
connection; the next segment opens a new one.

The session speaks raw 16-bit little-endian PCM at 16 kHz and maps provider
results onto the neutral ``FunASRServerEvent`` shape used by the voice core.

The task's sample origin is the absolute capture position of the first PCM the
task receives, or of the first replayed sample after a reconnect. It is never
assumed to be zero, because a media session can join the stream at any offset.

Credentials come from the environment only. Nothing in this module reads a
plaintext config file or logs a token.
"""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import logging
import math
import os
import uuid
from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import Any, cast

import websockets
from websockets.asyncio.client import ClientConnection

from services.agent.src.providers.funasr_protocol import FunASRSentence, FunASRServerEvent
from services.agent.src.providers.volc_protocol import (
    FULL_SERVER_RESPONSE,
    SERVER_ERROR_RESPONSE,
    VolcTranscript,
    build_audio,
    build_empty_last_audio,
    build_full_client_request,
    build_task_request,
    extract_transcript,
    parse_frame,
)

logger = logging.getLogger(__name__)

DEFAULT_VOLC_ASR_ENDPOINT = (
    "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
)
DEFAULT_VOLC_RESOURCE_ID = "volc.seedasr.sauc.duration"


class VolcASRError(RuntimeError):
    """Provider rejected a connection, session or task."""


@dataclass
class VolcASRConfig:
    app_id: str
    access_token: str
    resource_id: str = DEFAULT_VOLC_RESOURCE_ID
    ws_url: str = DEFAULT_VOLC_ASR_ENDPOINT
    model_name: str = "bigmodel"
    sample_rate: int = 16_000
    chunk_ms: int = 100
    end_window_ms: int = 550
    enable_nonstream: bool = True
    show_utterances: bool = True
    boosting_table_id: str | None = None
    connect_timeout_s: float = 5.0
    result_timeout_s: float = 8.0
    post_finish_tail_grace_s: float = 0.25
    # How much already-sent PCM stays available for replay after a transport
    # reconnect. The discontinuity path commits past the gap, so only a short
    # contiguous tail can ever be replayed.
    reconnect_audio_ms: int = 400
    # Test seam: replaces ``websockets.connect`` without touching the network.
    connector: Any = None

    def __post_init__(self) -> None:
        if self.sample_rate != 16_000:
            raise ValueError("Volc ASR only supports 16000 Hz input")
        if self.chunk_ms <= 0 or self.sample_rate * self.chunk_ms % 1000:
            raise ValueError("Volc ASR chunk_ms must produce whole samples")
        if not 0 < self.connect_timeout_s <= 30:
            raise ValueError("Volc ASR connect timeout must be within (0, 30] seconds")
        if not 0 < self.result_timeout_s <= 60:
            raise ValueError("Volc ASR result timeout must be within (0, 60] seconds")
        if not 0.0 <= self.post_finish_tail_grace_s <= 2.0:
            raise ValueError("Volc ASR post-finish tail grace must be between 0 and 2 seconds")
        if not 0 <= self.reconnect_audio_ms <= 5_000:
            raise ValueError("Volc ASR reconnect audio must be within [0, 5000] ms")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> VolcASRConfig:
        e = env or dict(os.environ)
        endpoint = (
            e.get("VOLC_ASR_WS_URL", "").strip()
            or e.get("VOLC_ASR_WS_ENDPOINT", "").strip()
            or DEFAULT_VOLC_ASR_ENDPOINT
        )
        return cls(
            app_id=e.get("VOLC_ASR_APP_ID", "").strip(),
            access_token=e.get("VOLC_ASR_ACCESS_TOKEN", "").strip(),
            resource_id=e.get("VOLC_ASR_RESOURCE_ID", "").strip()
            or DEFAULT_VOLC_RESOURCE_ID,
            ws_url=endpoint,
            model_name=e.get("VOLC_ASR_MODEL", "bigmodel").strip() or "bigmodel",
            chunk_ms=int(e.get("VOLC_ASR_CHUNK_MS", "100")),
            end_window_ms=int(e.get("VOLC_ASR_END_WINDOW_MS", "550")),
            enable_nonstream=e.get("VOLC_ASR_ENABLE_NONSTREAM", "true").lower() == "true",
            show_utterances=e.get("VOLC_ASR_SHOW_UTTERANCES", "true").lower() == "true",
            boosting_table_id=e.get("VOLC_ASR_BOOSTING_TABLE_ID", "").strip() or None,
            connect_timeout_s=float(e.get("VOLC_ASR_CONNECT_TIMEOUT_S", "5")),
            result_timeout_s=float(e.get("VOLC_ASR_RESULT_TIMEOUT_S", "8")),
            post_finish_tail_grace_s=float(
                e.get("VOLC_ASR_POST_FINISH_TAIL_GRACE_S", "0.25")
            ),
            reconnect_audio_ms=int(e.get("VOLC_ASR_RECONNECT_AUDIO_MS", "400")),
        )

    def auth_headers(self) -> dict[str, str]:
        if not self.app_id or not self.access_token:
            raise ValueError(
                "Volc ASR credentials are missing; set VOLC_ASR_APP_ID and "
                "VOLC_ASR_ACCESS_TOKEN"
            )
        return {
            "X-Api-App-Key": self.app_id,
            "X-Api-Access-Key": self.access_token,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
        }


@dataclass
class _Evidence:
    start_sample: int | None = None
    end_sample: int | None = None
    send_count: int = 0
    sample_count: int = 0
    peak_abs: int = 0
    square_sum: float = 0.0

    def record(self, pcm: bytes, start: int, end: int) -> None:
        if self.start_sample is None:
            self.start_sample = start
        self.end_sample = end
        self.send_count += 1
        samples = len(pcm) // 2
        self.sample_count += samples
        peak = audioop.max(pcm, 2)
        rms = audioop.rms(pcm, 2)
        self.peak_abs = max(self.peak_abs, peak)
        self.square_sum += float(rms * rms * samples)

    def reset(self) -> None:
        self.start_sample = None
        self.end_sample = None
        self.send_count = 0
        self.sample_count = 0
        self.peak_abs = 0
        self.square_sum = 0.0

    @property
    def rms(self) -> float | None:
        if not self.sample_count:
            return None
        return math.sqrt(self.square_sum / self.sample_count)


class VolcASRSession:
    """One streaming session; each VAD segment is a new provider task."""

    def __init__(self, config: VolcASRConfig, *, metrics: Any = None) -> None:
        self.config = config
        self.metrics = metrics
        self.events: asyncio.Queue[FunASRServerEvent] = asyncio.Queue()
        self.task_id: str | None = None
        self._task_epoch = 0
        self._segment_epoch = 1
        self._task_sample_origin = 0
        self._last_sent_sample = 0
        self._last_committed_sample = 0
        self._evidence = _Evidence()
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._control: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._audio_buffer = bytearray()
        self._audio_sequence = 0
        self._task_open = False
        self._origin_known = False
        self._sentence_id = 0
        self._last_final_text = ""
        self._rotation_pending = False
        self._failed = False
        self._closed = False
        self._max_ring_bytes = config.sample_rate * 2 * config.reconnect_audio_ms // 1000
        self._ring: deque[tuple[bytes, int, int]] = deque()
        self._ring_bytes = 0

    @property
    def task_epoch(self) -> int:
        return self._task_epoch

    @property
    def segment_epoch(self) -> int:
        return self._segment_epoch

    @property
    def rotation_pending(self) -> bool:
        return self._rotation_pending

    @property
    def task_sample_origin(self) -> int:
        return self._task_sample_origin

    @property
    def task_audio_start_sample(self) -> int | None:
        return self._evidence.start_sample

    @property
    def task_audio_end_sample(self) -> int | None:
        return self._evidence.end_sample

    @property
    def task_audio_send_count(self) -> int:
        return self._evidence.send_count

    @property
    def task_pcm_sample_count(self) -> int:
        return self._evidence.sample_count

    @property
    def task_pcm_peak_abs(self) -> int | None:
        return self._evidence.peak_abs if self._evidence.sample_count else None

    @property
    def task_pcm_rms(self) -> float | None:
        return self._evidence.rms

    @property
    def last_sent_sample(self) -> int:
        return self._last_sent_sample

    def mark_committed_sample(self, sample: int) -> None:
        if sample < 0:
            raise ValueError("committed sample must be non-negative")
        self._last_committed_sample = max(self._last_committed_sample, sample)

    async def connect(self) -> None:
        """Open the transport and send the first task's configuration."""

        if self._closed:
            raise RuntimeError("Volc ASR session is closed")
        if self._ws is not None:
            return
        self.config.auth_headers()
        await self._open_ws()
        await self._send_config()

    async def _open_ws(self) -> ClientConnection:
        connector = self.config.connector or websockets.connect
        ws = cast(
            ClientConnection,
            await asyncio.wait_for(
                connector(
                    self.config.ws_url,
                    additional_headers=self.config.auth_headers(),
                    open_timeout=self.config.connect_timeout_s,
                ),
                timeout=self.config.connect_timeout_s,
            ),
        )
        self._ws = ws
        self._failed = False
        self._recv_task = asyncio.create_task(self._recv_loop(), name="volc-asr-recv-loop")
        return ws

    async def _send_config(self) -> None:
        ws = self._ws
        if ws is None:
            raise VolcASRError("Volc ASR WebSocket is not connected")
        # Control messages belong to the previous connection's receive loop.
        while True:
            try:
                self._control.get_nowait()
            except asyncio.QueueEmpty:
                break
        await ws.send(
            build_full_client_request(
                build_task_request(
                    model_name=self.config.model_name,
                    sample_rate=self.config.sample_rate,
                    end_window_ms=self.config.end_window_ms,
                    enable_nonstream=self.config.enable_nonstream,
                    show_utterances=self.config.show_utterances,
                    boosting_table_id=self.config.boosting_table_id,
                )
            )
        )
        self.task_id = str(uuid.uuid4())
        self._task_epoch += 1
        self._audio_sequence = 0
        self._audio_buffer.clear()
        self._sentence_id = 0
        self._last_final_text = ""
        self._evidence.reset()
        self._task_open = True
        self._origin_known = False
        self._rotation_pending = False
        if self.metrics is not None:
            self.metrics.inc_asr_request("opened", self.config.model_name)

    async def _begin_task(self, frame_start: int) -> None:
        """Open the next task at its real sample origin, replaying its tail."""

        await self._abort_task()
        await self._open_ws()
        replay_start = self._replay_watermark()
        origin = frame_start
        replay = b""
        if replay_start is not None and self._ring[-1][2] == frame_start:
            # Only a contiguous tail can be replayed: a hole between the last
            # sent sample and this frame would compress the provider's own
            # timeline, so the task then starts at the frame's real position.
            origin = replay_start
            replay = self._replay_pcm(replay_start)
        await self._send_config()
        self._task_sample_origin = origin
        self._origin_known = True
        if replay:
            self._queue_audio(replay, start=origin, replay=True)
            await self._flush_audio()

    def _replay_watermark(self) -> int | None:
        """First replayed absolute sample, or ``None`` when nothing is left.

        Clamped to the ring's oldest retained range so the origin always
        matches the first byte actually re-sent.
        """

        if not self._ring:
            return None
        earliest = self._ring[0][1]
        latest = self._ring[-1][2]
        floor = max(earliest, self._task_sample_origin, self._last_committed_sample)
        if floor >= latest:
            return None
        return floor

    def _replay_pcm(self, start_sample: int) -> bytes:
        chunks: list[bytes] = []
        for pcm, frame_start, frame_end in self._ring:
            if frame_end <= start_sample:
                continue
            offset_samples = max(0, start_sample - frame_start)
            chunks.append(pcm[offset_samples * 2 :])
        return b"".join(chunks)

    def _queue_audio(self, pcm: bytes, *, start: int, replay: bool = False) -> None:
        """Append PCM to the send buffer and the bounded replay ring."""

        if not self._origin_known:
            # The task starts where its first PCM really starts. A media
            # session joins the capture clock at an arbitrary offset, so zero
            # is only correct for a session that began at sample zero.
            self._task_sample_origin = start
            self._origin_known = True
        end = start + len(pcm) // 2
        self._audio_buffer.extend(pcm)
        if self._max_ring_bytes and not replay:
            # Replayed PCM keeps its original ring entry; re-appending it would
            # make the next replay send the same audio twice.
            self._ring.append((pcm, start, end))
            self._ring_bytes += len(pcm)
            while self._ring_bytes > self._max_ring_bytes and len(self._ring) > 1:
                old, _, _ = self._ring.popleft()
                self._ring_bytes -= len(old)
        self._evidence.record(pcm, start, end)

    async def _flush_audio(self) -> None:
        """Send every complete chunk, keeping only a partial tail buffered."""

        ws = self._ws
        if ws is None:
            raise VolcASRError("Volc ASR WebSocket is not connected")
        chunk_bytes = self.config.sample_rate * 2 * self.config.chunk_ms // 1000
        while len(self._audio_buffer) >= chunk_bytes:
            chunk = bytes(self._audio_buffer[:chunk_bytes])
            del self._audio_buffer[:chunk_bytes]
            self._audio_sequence += 1
            await ws.send(build_audio(chunk, sequence=self._audio_sequence))

    async def _finish_audio(self) -> None:
        """Close the task with the final packet.

        A buffered partial chunk is the final packet and carries the negative
        sequence. When the audio ended exactly on a chunk boundary the final
        packet has no payload and uses the "last package, no sequence" flag,
        which is the same shape the official Go SDK sends.
        """

        ws = self._ws
        if ws is None:
            raise VolcASRError("Volc ASR WebSocket is not connected")
        if not self._audio_buffer:
            await ws.send(build_empty_last_audio())
            return
        chunk = bytes(self._audio_buffer)
        self._audio_buffer.clear()
        self._audio_sequence += 1
        await ws.send(build_audio(chunk, sequence=self._audio_sequence, last=True))

    async def send_pcm(
        self,
        pcm: bytes,
        *,
        capture_start_sample: int | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("Volc ASR session is not connected")
        if self._failed:
            raise VolcASRError("Volc ASR session failed")
        if not pcm or len(pcm) % 2:
            raise ValueError("Volc ASR PCM must be non-empty 16-bit samples")
        start = self._last_sent_sample if capture_start_sample is None else capture_start_sample
        if start < self._last_sent_sample:
            raise ValueError("capture sample position moved backwards")
        end = start + len(pcm) // 2
        if self._rotation_pending or not self._task_open:
            await self._begin_task(start)
        self._queue_audio(pcm, start=start)
        await self._flush_audio()
        self._last_sent_sample = end

    async def _recv_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            async for message in ws:
                if isinstance(message, str):
                    logger.info("volc ASR ignored text frame")
                    continue
                try:
                    frame = parse_frame(message)
                except ValueError:
                    logger.warning("volc ASR dropped an undecodable frame")
                    continue
                if frame.message_type == SERVER_ERROR_RESPONSE:
                    self._failed = True
                    code = frame.error_code
                    await self._control.put(("failed", code))
                    await self.events.put(
                        FunASRServerEvent(
                            event="task-failed",
                            task_id=self.task_id or "",
                            error_code=str(code) if code is not None else None,
                            error_message="volc server error",
                        )
                    )
                    return
                if frame.message_type != FULL_SERVER_RESPONSE:
                    continue
                transcript = extract_transcript(frame.payload)
                if transcript is not None:
                    await self._emit_transcript(transcript, final=frame.is_last)
                if frame.is_last:
                    await self._control.put(("boundary", frame.sequence))
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._closed:
                return
            logger.warning("volc ASR receive loop ended", exc_info=True)
        if self._closed:
            return
        # The server closed without a last-flagged response: this task cannot
        # produce a boundary, so the segment must fail over instead of leaving
        # the caller blocked on ``_wait_for_boundary``.
        self._failed = True
        await self._control.put(("failed", "connection-closed"))
        await self.events.put(
            FunASRServerEvent(
                event="task-failed",
                task_id=self.task_id or "",
                error_message="volc connection closed",
            )
        )

    async def _emit_transcript(self, transcript: VolcTranscript, *, final: bool) -> None:
        sentence_end = final or transcript.definite
        if sentence_end and transcript.text == self._last_final_text:
            return
        if sentence_end:
            self._sentence_id += 1
            self._last_final_text = transcript.text
        await self.events.put(
            FunASRServerEvent(
                event="result-generated",
                task_id=self.task_id or "",
                sentence=FunASRSentence(
                    sentence_id=self._sentence_id,
                    text=transcript.text,
                    begin_ms=transcript.begin_ms,
                    end_ms=transcript.end_ms,
                    sentence_end=sentence_end,
                    heartbeat=False,
                    words=(),
                    word_timing_valid=False,
                ),
            )
        )

    async def rotate_task(self, *, require_consumed: bool = True) -> None:
        """End the current task with a negative packet and await its boundary."""

        if self._closed:
            return
        if self._ws is None or not self._task_open:
            self._emit_task_finished()
            self._rotation_pending = True
            self._segment_epoch += 1
            self._evidence.reset()
            return
        if not self._evidence.sample_count:
            # The task never received audio, so there is nothing for the
            # service to recognise. Close locally instead of waiting for a
            # response to a request that carries no speech.
            self._rotation_pending = True
            self._segment_epoch += 1
            self._task_open = False
            self._task_sample_origin = self._last_sent_sample
            self._evidence.reset()
            await self._abort_task()
            self._emit_task_finished()
            return
        try:
            if self._failed:
                raise VolcASRError("Volc ASR session failed before its task ended")
            await self._finish_audio()
            await self._wait_for_boundary()
        finally:
            self._rotation_pending = True
            self._segment_epoch += 1
            self._task_open = False
            self._task_sample_origin = self._last_sent_sample
            self._evidence.reset()
            await self._abort_task()
        self._emit_task_finished()

    async def _wait_for_boundary(self) -> None:
        deadline = monotonic() + self.config.result_timeout_s
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise VolcASRError("Volc ASR timed out waiting for task boundary")
            kind, payload = await asyncio.wait_for(self._control.get(), timeout=remaining)
            if kind == "failed":
                raise VolcASRError(f"Volc ASR task boundary failed: {payload}")
            if kind == "boundary":
                return

    def _emit_task_finished(self) -> None:
        self.events.put_nowait(
            FunASRServerEvent(
                event="task-finished",
                task_id=self.task_id or "",
            )
        )

    async def reconnect_with_replay(self) -> None:
        """Replace a dead connection and replay everything after the commit.

        The replacement task starts at the committed watermark clamped to the
        replay ring, and re-sends the PCM between that point and the
        disconnect. When the watermark has already reached the sent audio,
        the next frame's real capture position becomes the origin instead.
        """

        if self._closed:
            return
        await self._abort_task()
        self._failed = False
        self._rotation_pending = True
        self._task_open = False

    async def _abort_task(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv_task
            self._recv_task = None
        await self._close_ws()
        self._audio_buffer.clear()
        self._task_open = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ring.clear()
        self._ring_bytes = 0
        await self._abort_task()

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await ws.close()


__all__ = [
    "DEFAULT_VOLC_ASR_ENDPOINT",
    "DEFAULT_VOLC_RESOURCE_ID",
    "VolcASRConfig",
    "VolcASRError",
    "VolcASRSession",
]
