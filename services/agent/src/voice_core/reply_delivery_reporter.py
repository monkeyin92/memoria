"""Non-blocking cross-process delivery for text-free reply telemetry."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReplyDeliveryReporterConfig:
    endpoint: str
    token: str
    spool_path: Path
    spool_key: str
    spool_max_bytes: int = 8 * 1024 * 1024
    timeout_s: float = 2.0
    max_pending: int = 512

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("reply delivery endpoint must be an HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in {
            "control-api",
            "agent",
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("plaintext reply delivery endpoint must be local control API")
        if len(self.token.strip()) < 32:
            raise ValueError("reply delivery token must contain at least 32 characters")
        if self.spool_max_bytes < 4096:
            raise ValueError("reply delivery spool cap must be at least 4096 bytes")
        try:
            Fernet(self.spool_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("reply delivery spool key must be a valid Fernet key") from exc
        if self.timeout_s <= 0:
            raise ValueError("reply delivery timeout must be positive")
        if self.max_pending <= 0:
            raise ValueError("reply delivery pending cap must be positive")


class ReplyDeliveryReporter:
    """Queue bounded telemetry so a slow Control API never blocks audio."""

    def __init__(
        self,
        config: ReplyDeliveryReporterConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._fernet = Fernet(config.spool_key.encode("ascii"))
        self._closed = False
        self.published_count = 0
        self.failed_count = 0
        self.dropped_count = 0

    async def replay(self) -> int:
        """Replay encrypted network-failure rows before accepting new events."""

        delivered = 0
        encrypted_lines = self._read_lines()
        remaining: list[bytes] = []
        for index, token in enumerate(encrypted_lines):
            try:
                payload = self._fernet.decrypt(token)
                decoded = json.loads(payload)
            except (InvalidToken, json.JSONDecodeError, TypeError) as exc:
                raise RuntimeError("reply delivery spool is corrupt or key-mismatched") from exc
            if not isinstance(decoded, dict):
                raise RuntimeError("reply delivery spool payload must be an object")
            if await self._deliver(decoded):
                delivered += 1
                continue
            remaining.extend(encrypted_lines[index:])
            break
        if remaining != encrypted_lines:
            self._replace_lines(remaining)
        return delivered

    def submit(self, payload: dict[str, Any]) -> bool:
        """Admit one projection without awaiting network I/O."""

        if self._closed:
            self.dropped_count += 1
            logger.error(
                "reply delivery projection dropped after reporter close event_id=%s",
                payload.get("event_id"),
            )
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.dropped_count += 1
            logger.error(
                "reply delivery projection dropped without running loop event_id=%s",
                payload.get("event_id"),
            )
            return False
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.config.max_pending)
        try:
            self._queue.put_nowait(dict(payload))
        except asyncio.QueueFull:
            self.dropped_count += 1
            logger.error(
                "reply delivery projection queue full event_id=%s pending=%s",
                payload.get("event_id"),
                self.config.max_pending,
            )
            return False
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(),
                name="reply-delivery-projection",
            )
        return True

    async def _run(self) -> None:
        queue = self._queue
        if queue is None:  # pragma: no cover - submit creates the queue first
            return
        self._ensure_client()
        while True:
            payload = await queue.get()
            try:
                if self._read_lines():
                    await self.replay()
                if self._read_lines():
                    self._append(payload)
                else:
                    if not await self._deliver(payload):
                        self._append(payload)
            except (httpx.HTTPError, OSError, RuntimeError) as exc:
                self.failed_count += 1
                logger.error(
                    "reply delivery projection failed error=%s event_id=%s",
                    type(exc).__name__,
                    payload.get("event_id"),
                )
                try:
                    self._append(payload)
                except Exception:
                    logger.exception(
                        "reply delivery projection could not persist failed event_id=%s",
                        payload.get("event_id"),
                    )
            finally:
                queue.task_done()

    async def _deliver(self, payload: dict[str, Any]) -> bool:
        client = self._ensure_client()
        try:
            response = await client.post(
                self.config.endpoint,
                headers={
                    "X-Media-Reply-Delivery-Token": self.config.token,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            logger.error(
                "reply delivery projection network failure event_id=%s",
                payload.get("event_id"),
            )
            return False
        if response.status_code in {200, 201, 202}:
            self.published_count += 1
            return True
        if response.status_code >= 500 or response.status_code == 429:
            logger.error(
                "reply delivery projection retryable status=%s event_id=%s",
                response.status_code,
                payload.get("event_id"),
            )
            return False
        self.failed_count += 1
        logger.error(
            "reply delivery projection permanently rejected status=%s event_id=%s",
            response.status_code,
            payload.get("event_id"),
        )
        return True

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout_s)
        return self._client

    async def flush(self) -> None:
        if self._queue is not None:
            await self._queue.join()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.flush()
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _read_lines(self) -> list[bytes]:
        if not self.config.spool_path.exists():
            return []
        return [line for line in self.config.spool_path.read_bytes().splitlines() if line]

    def _append(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        token = self._fernet.encrypt(encoded) + b"\n"
        path = self.config.spool_path
        path.parent.mkdir(parents=True, exist_ok=True)
        current_size = path.stat().st_size if path.exists() else 0
        if current_size + len(token) > self.config.spool_max_bytes:
            self.dropped_count += 1
            logger.error(
                "reply delivery spool full event_id=%s cap=%s",
                payload.get("event_id"),
                self.config.spool_max_bytes,
            )
            return
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            os.write(descriptor, token)
            os.fsync(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _replace_lines(self, lines: list[bytes]) -> None:
        path = self.config.spool_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            data = b"".join(line + b"\n" for line in lines)
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)


__all__ = ["ReplyDeliveryReporter", "ReplyDeliveryReporterConfig"]
