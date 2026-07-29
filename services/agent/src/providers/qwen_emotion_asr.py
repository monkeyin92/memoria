"""Non-blocking Qwen3-ASR Realtime sidecar for ephemeral acoustic emotion labels."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import logging
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

from services.agent.src.orchestration.emotion import EMOTION_LABELS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QwenEmotionConfig:
    api_key: str
    ws_url: str
    model: str = "qwen3-asr-flash-realtime"
    sample_rate: int = 16000
    queue_chunks: int = 32
    connect_timeout_s: float = 5.0
    reconnect_delay_s: float = 1.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> QwenEmotionConfig:
        import os

        values = env or dict(os.environ)
        model = values.get("QWEN_EMOTION_MODEL", "qwen3-asr-flash-realtime")
        explicit = values.get("QWEN_EMOTION_WS_URL", "").strip()
        base = explicit or values.get("DASHSCOPE_WS_URL", "").strip()
        if base and not explicit:
            base = base.replace("/api-ws/v1/inference", "/api-ws/v1/realtime")
        ws_url = _with_model(base, model) if base else ""
        return cls(
            api_key=values.get("DASHSCOPE_API_KEY", ""),
            ws_url=ws_url,
            model=model,
            sample_rate=int(values.get("QWEN_EMOTION_SAMPLE_RATE", "16000")),
            queue_chunks=int(values.get("QWEN_EMOTION_QUEUE_CHUNKS", "32")),
            connect_timeout_s=float(values.get("QWEN_EMOTION_CONNECT_TIMEOUT_S", "5")),
            reconnect_delay_s=float(values.get("QWEN_EMOTION_RECONNECT_DELAY_S", "1")),
        )


@dataclass(frozen=True)
class QwenEmotionResult:
    label: str
    text: str
    turn_id: int | None
    provider_confidence: None = None


def _with_model(url: str, model: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["model"] = model
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def parse_emotion_event(
    event: dict[str, Any],
    *,
    turn_id: int | None = None,
) -> QwenEmotionResult | None:
    if event.get("type") != "conversation.item.input_audio_transcription.completed":
        return None
    label = str(event.get("emotion") or "neutral")
    if label not in EMOTION_LABELS:
        label = "neutral"
    text = str(event.get("transcript") or event.get("text") or "")
    return QwenEmotionResult(label=label, text=text, turn_id=turn_id)


class QwenEmotionSidecar:
    """Owns an independent bounded queue; feed_pcm never awaits network I/O."""

    def __init__(
        self,
        config: QwenEmotionConfig,
        *,
        on_observation: Callable[[QwenEmotionResult], Any],
    ) -> None:
        self.config = config
        self._on_observation = on_observation
        self._queue: asyncio.Queue[tuple[int | None, bytes]] = asyncio.Queue(
            maxsize=config.queue_chunks
        )
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._active_turn_id: int | None = None
        self._sent_samples = 0
        self._sent_turn_intervals: deque[tuple[int, int, int | None]] = deque()
        self._provider_item_turns: dict[str, int | None] = {}
        self.dropped_chunks = 0

    @property
    def configured(self) -> bool:
        return bool(self.config.api_key and self.config.ws_url)

    def feed_pcm(self, pcm: bytes) -> None:
        if not pcm or self._closing:
            return
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self.dropped_chunks += 1
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait((self._active_turn_id, pcm))

    def start_turn(self, turn_id: int) -> None:
        self._active_turn_id = turn_id

    def start(self) -> bool:
        if not self.configured or self._task is not None:
            return False
        self._task = asyncio.create_task(self._run(), name="qwen-emotion-sidecar")
        return True

    async def _run(self) -> None:
        while not self._closing:
            try:
                await self._run_connection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Qwen emotion sidecar disconnected: %s", type(exc).__name__)
            if not self._closing:
                await asyncio.sleep(self.config.reconnect_delay_s)

    async def _run_connection(self) -> None:
        self._sent_samples = 0
        self._sent_turn_intervals.clear()
        self._provider_item_turns.clear()
        async with websockets.connect(
            self.config.ws_url,
            additional_headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "OpenAI-Beta": "realtime=v1",
            },
            open_timeout=self.config.connect_timeout_s,
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "event_id": str(uuid.uuid4()),
                        "type": "session.update",
                        "session": {
                            "modalities": ["text"],
                            "input_audio_format": "pcm",
                            "sample_rate": self.config.sample_rate,
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.0,
                                "silence_duration_ms": 400,
                            },
                        },
                    },
                    ensure_ascii=False,
                )
            )
            sender = asyncio.create_task(self._send_loop(ws), name="qwen-emotion-send")
            receiver = asyncio.create_task(self._receive_loop(ws), name="qwen-emotion-recv")
            done, pending = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                error = task.exception()
                if error is not None:
                    raise error

    async def _send_loop(self, ws: Any) -> None:
        while not self._closing:
            turn_id, pcm = await self._queue.get()
            self._record_sent_audio(turn_id, pcm)
            await ws.send(
                json.dumps(
                    {
                        "event_id": str(uuid.uuid4()),
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm).decode("ascii"),
                    }
                )
            )

    def _record_sent_audio(self, turn_id: int | None, pcm: bytes) -> None:
        start = self._sent_samples
        self._sent_samples += len(pcm) // 2
        if self._sent_turn_intervals and self._sent_turn_intervals[-1][2] == turn_id:
            previous_start, _, _ = self._sent_turn_intervals.pop()
            self._sent_turn_intervals.append((previous_start, self._sent_samples, turn_id))
        else:
            self._sent_turn_intervals.append((start, self._sent_samples, turn_id))

    def _turn_for_audio_start(self, audio_start_ms: object) -> int | None:
        if not isinstance(audio_start_ms, int) or audio_start_ms < 0:
            return None
        start_sample = audio_start_ms * self.config.sample_rate // 1000
        while (
            len(self._sent_turn_intervals) > 1 and self._sent_turn_intervals[0][1] <= start_sample
        ):
            self._sent_turn_intervals.popleft()
        return next(
            (
                turn_id
                for begin, end, turn_id in self._sent_turn_intervals
                if begin <= start_sample < end
            ),
            None,
        )

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "input_audio_buffer.speech_started":
                item_id = event.get("item_id")
                if isinstance(item_id, str) and item_id:
                    self._provider_item_turns[item_id] = self._turn_for_audio_start(
                        event.get("audio_start_ms")
                    )
                continue
            if event.get("type") != "conversation.item.input_audio_transcription.completed":
                continue
            item_id = event.get("item_id")
            turn_id = (
                self._provider_item_turns.pop(item_id, None) if isinstance(item_id, str) else None
            )
            result = parse_emotion_event(event, turn_id=turn_id)
            if result is None:
                continue
            callback_result = self._on_observation(result)
            if inspect.isawaitable(callback_result):
                await callback_result

    async def aclose(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
