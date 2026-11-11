"""Transport primitives for the Media Edge gRPC bridge.

``MediaBridgeTLS``, ``_PriorityOutgoing`` and ``_Connection`` are composed by
``MediaBridgeGrpcServer``; the server imports them from here.  The module exists
so mTLS material loading, the bounded priority outgoing queue and the
per-connection record can be read on their own, without the request, session and
emission handlers around them.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc

from services.agent.src.voice_core.generated.memoria.media.v1 import (
    media_pb2 as _media_pb2,
)
from services.agent.src.voice_core.media_bridge_server import MediaBridgeSession

media_pb2: Any = _media_pb2


@dataclass(frozen=True, slots=True)
class MediaBridgeTLS:
    """Server certificate material for internal mTLS."""

    private_key_pem: bytes
    certificate_chain_pem: bytes
    client_ca_pem: bytes | None = None

    @classmethod
    def from_files(
        cls,
        *,
        private_key_file: str,
        certificate_chain_file: str,
        client_ca_file: str | None = None,
    ) -> MediaBridgeTLS:
        """Load mTLS material without ever logging the key contents."""

        def read_required(path_value: str, label: str) -> bytes:
            path = Path(path_value).expanduser()
            if not path_value.strip() or not path.is_file():
                raise ValueError(f"media bridge {label} file does not exist")
            data = path.read_bytes()
            if not data:
                raise ValueError(f"media bridge {label} file is empty")
            return data

        client_ca = None
        if client_ca_file is not None:
            client_ca = read_required(client_ca_file, "client CA")
        return cls(
            private_key_pem=read_required(private_key_file, "private key"),
            certificate_chain_pem=read_required(certificate_chain_file, "certificate"),
            client_ca_pem=client_ca,
        )

    def credentials(self) -> grpc.ServerCredentials:
        if not self.private_key_pem or not self.certificate_chain_pem:
            raise ValueError("bridge TLS key and certificate are required")
        return grpc.ssl_server_credentials(
            ((self.private_key_pem, self.certificate_chain_pem),),
            root_certificates=self.client_ca_pem,
            require_client_auth=self.client_ca_pem is not None,
        )


class _PriorityOutgoing:
    """Bounded Critical/Reliable/Coalescing queue with strict priority drain."""

    _RELIABLE_BUDGET = 16

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._critical: deque[media_pb2.CoreToMedia | None] = deque()
        self._reliable: deque[media_pb2.CoreToMedia | None] = deque()
        self._coalescing: deque[media_pb2.CoreToMedia | None] = deque()
        self._ready = asyncio.Event()
        self._space = asyncio.Event()
        self._space.set()
        self._reliable_since_bulk = 0
        self._next_event_sequence = 0

    def prepare_for_send(
        self,
        message: media_pb2.CoreToMedia | None,
    ) -> media_pb2.CoreToMedia | None:
        """Stamp ordered event identifiers at the actual transport boundary.

        Producers enqueue across priority lanes.  Assigning the shared event
        sequence before that arbitration lets a later critical event overtake
        an earlier reliable event while retaining a larger sequence, causing
        Media Edge to reject the delayed event as stale.
        """

        if message is None:
            return None
        kind = message.WhichOneof("event")
        if kind not in {
            "generation",
            "transcript",
            "state",
            "client",
            "realtime_effect",
            "floor_effect",
        }:
            return message
        event = getattr(message, kind)
        sequence = self._next_event_sequence
        self._next_event_sequence += 1
        event.sequence = sequence
        if kind == "realtime_effect":
            event.effect_id = (
                f"{event.identity.session_id}:{event.identity.stream_epoch}:effect:{sequence}"
            )
        elif kind == "floor_effect":
            event.effect_id = (
                f"{event.identity.session_id}:{event.identity.stream_epoch}:"
                f"floor:{event.floor_epoch}:{sequence}"
            )
        elif kind == "client":
            try:
                payload = json.loads(bytes(event.json_payload))
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                payload["sequence"] = sequence
                payload["event_id"] = (
                    f"{event.identity.session_id}:{event.identity.stream_epoch}:{sequence}"
                )
                event.json_payload = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
        return message

    @staticmethod
    def _lane(message: media_pb2.CoreToMedia | None) -> str:
        if message is None:
            return "critical"
        kind = message.WhichOneof("event")
        if kind in {"generation", "realtime_effect", "floor_effect", "state", "error"}:
            return "critical"
        if kind == "transcript" and not bool(message.transcript.final):
            return "coalescing"
        if kind == "client" and str(message.client.type).startswith("turn.provisional."):
            return "coalescing"
        return "reliable"

    @staticmethod
    def _coalescing_key(message: media_pb2.CoreToMedia | None) -> tuple[object, ...] | None:
        if message is None:
            return None
        kind = message.WhichOneof("event")
        if kind == "transcript":
            return (kind, int(message.transcript.turn_id))
        if kind == "client":
            return (kind, str(message.client.type), int(message.client.turn_id))
        return None

    def put_nowait(self, message: media_pb2.CoreToMedia | None) -> None:
        lane = self._lane(message)
        if lane == "coalescing":
            key = self._coalescing_key(message)
            if key is not None:
                self._coalescing = deque(
                    queued for queued in self._coalescing if self._coalescing_key(queued) != key
                )
        if self.qsize() >= self.maxsize:
            raise asyncio.QueueFull
        target = {
            "critical": self._critical,
            "reliable": self._reliable,
            "coalescing": self._coalescing,
        }[lane]
        target.append(message)
        self._ready.set()
        if self.qsize() >= self.maxsize:
            self._space.clear()

    async def put(self, message: media_pb2.CoreToMedia | None) -> None:
        while True:
            try:
                self.put_nowait(message)
                return
            except asyncio.QueueFull:
                self._space.clear()
                if self.qsize() < self.maxsize:
                    continue
                await self._space.wait()

    def get_nowait(self) -> media_pb2.CoreToMedia | None:
        if self._critical:
            message = self._critical.popleft()
        elif self._coalescing and (
            not self._reliable or self._reliable_since_bulk >= self._RELIABLE_BUDGET
        ):
            message = self._coalescing.popleft()
            self._reliable_since_bulk = 0
        elif self._reliable:
            message = self._reliable.popleft()
            self._reliable_since_bulk += 1
        elif self._coalescing:
            message = self._coalescing.popleft()
            self._reliable_since_bulk = 0
        else:
            raise asyncio.QueueEmpty
        message = self.prepare_for_send(message)
        self._space.set()
        if self.empty():
            self._ready.clear()
        return message

    async def get(self) -> media_pb2.CoreToMedia | None:
        while True:
            try:
                return self.get_nowait()
            except asyncio.QueueEmpty:
                self._ready.clear()
                if not self.empty():
                    continue
                await self._ready.wait()

    def evict_coalescing(self) -> media_pb2.CoreToMedia | None:
        if not self._coalescing:
            return None
        message = self._coalescing.popleft()
        self._space.set()
        if self.empty():
            self._ready.clear()
        return message

    def clear(self) -> None:
        self._critical.clear()
        self._reliable.clear()
        self._coalescing.clear()
        self._reliable_since_bulk = 0
        self._ready.clear()
        self._space.set()

    def qsize(self) -> int:
        return len(self._critical) + len(self._reliable) + len(self._coalescing)

    def empty(self) -> bool:
        return self.qsize() == 0


@dataclass(slots=True)
class _Connection:
    session: MediaBridgeSession
    outgoing: _PriorityOutgoing
    closed: bool = False
    close_notified: bool = False
    client_sequence: int = 0
