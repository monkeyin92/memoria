"""Provider-neutral streaming ASR session contract.

``provider_adapter`` owns the sample clock, task fencing and event mapping. It
must not depend on the concrete DashScope FunASR session, because the primary
provider can be selected by configuration. This Protocol is the surface the
adapter actually uses.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from services.agent.src.providers.funasr_protocol import FunASRServerEvent


@runtime_checkable
class StreamingASRSession(Protocol):
    """One streaming ASR session with provider task rotation."""

    config: Any
    events: asyncio.Queue[FunASRServerEvent]
    task_id: str | None

    @property
    def task_epoch(self) -> int: ...

    @property
    def segment_epoch(self) -> int: ...

    @property
    def rotation_pending(self) -> bool: ...

    @property
    def task_sample_origin(self) -> int: ...

    @property
    def task_audio_start_sample(self) -> int | None: ...

    @property
    def task_audio_end_sample(self) -> int | None: ...

    @property
    def task_audio_send_count(self) -> int: ...

    @property
    def task_pcm_sample_count(self) -> int: ...

    @property
    def task_pcm_peak_abs(self) -> int | None: ...

    @property
    def task_pcm_rms(self) -> float | None: ...

    @property
    def last_sent_sample(self) -> int: ...

    async def connect(self) -> None: ...

    async def send_pcm(
        self,
        pcm: bytes,
        *,
        capture_start_sample: int | None = None,
    ) -> None: ...

    async def rotate_task(self, *, require_consumed: bool = True) -> None: ...

    def mark_committed_sample(self, sample: int) -> None: ...

    async def reconnect_with_replay(self) -> None: ...

    async def aclose(self) -> None: ...


__all__ = ["StreamingASRSession"]
