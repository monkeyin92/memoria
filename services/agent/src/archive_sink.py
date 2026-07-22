"""Non-blocking archive delivery with a bounded encrypted disk spool."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import io
import json
import logging
import os
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.fernet import Fernet, InvalidToken


class ArchiveSpoolFullError(RuntimeError):
    """The configured safety cap was reached; no unbounded disk growth is allowed."""


class ArchiveSpoolKeyError(RuntimeError):
    """The spool cannot be decrypted with the configured key."""


ArchiveTarget = Literal["event", "raw_audio"]
MAX_RAW_VOICE_PCM_BYTES = 1_920_000
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ArchiveSinkConfig:
    endpoint: str
    internal_token: str
    spool_path: Path
    spool_key: str
    spool_max_bytes: int = 8 * 1024 * 1024
    timeout_s: float = 2.0

    def __post_init__(self) -> None:
        if not self.endpoint.startswith(("https://", "http://")):
            raise ValueError("archive endpoint must be HTTP(S)")
        if not self.internal_token:
            raise ValueError("archive internal token must not be blank")
        if not self.endpoint.endswith("/session-events"):
            raise ValueError("archive endpoint must end with /session-events")
        if self.spool_max_bytes < 4096:
            raise ValueError("archive spool cap must be at least 4096 bytes")
        if self.timeout_s <= 0:
            raise ValueError("archive timeout must be positive")
        try:
            Fernet(self.spool_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("archive spool key must be a valid Fernet key") from exc

    @property
    def raw_audio_endpoint(self) -> str:
        return self.endpoint.removesuffix("/session-events") + "/session-raw-audio"

    @property
    def consent_endpoint(self) -> str:
        return self.endpoint.removesuffix("/session-events") + "/session-raw-voice-consent"


class ArchiveSink:
    """Deliver evidence off the realtime path; replay failures remain encrypted on disk."""

    def __init__(
        self,
        config: ArchiveSinkConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._fernet = Fernet(config.spool_key.encode("ascii"))
        self._client = client or httpx.AsyncClient(timeout=config.timeout_s)
        self._owns_client = client is None
        self._lock = asyncio.Lock()

    async def publish(
        self,
        event: dict[str, Any],
        *,
        target: ArchiveTarget = "event",
    ) -> bool:
        """Return True when delivered now, False when safely queued for replay."""
        payload = self._encode({"target": target, "body": event})
        try:
            async with self._lock:
                async with self._exclusive_spool_lock():
                    if self._read_lines():
                        await self._replay_locked()
                        queued_lines = self._read_lines()
                        if queued_lines:
                            if target == "event" and await self._deliver(payload):
                                # A newly arrived parent turn may unblock an older
                                # assistant event that was waiting with HTTP 425.
                                await self._replay_locked()
                                return True
                            self._append(payload)
                            return False
                    if await self._deliver(payload):
                        return True
                    self._append(payload)
                    return False
        except asyncio.CancelledError:
            # Session shutdown may cancel a delivery while HTTP or another
            # process owns the spool lock. Persist before cancellation escapes;
            # a remote success racing this append is safe because event_id is
            # idempotent at the archive boundary.
            await asyncio.shield(self._append_after_cancellation(payload))
            raise

    async def publish_owner_turn(
        self,
        event: dict[str, Any],
        *,
        pcm: bytes,
        sample_rate: int,
    ) -> bool:
        """Archive owner WAV only with live consent; transcript always survives."""
        try:
            consent = await self._active_raw_voice_consent(
                str(event.get("session_id") or "")
            )
        except asyncio.CancelledError:
            await asyncio.shield(self.publish(event))
            raise
        if (
            consent is None
            or event.get("speaker_class") != "owner"
            or sample_rate != 16_000
            or not pcm
            or len(pcm) % 2
            or len(pcm) > MAX_RAW_VOICE_PCM_BYTES
        ):
            return await self.publish(event)
        shared_event = {
            **event,
            "consent_grant_id": consent["consent_grant_id"],
        }
        transcript_delivered = await self.publish(shared_event)
        wav = self._wav(pcm, sample_rate=sample_rate)
        raw_event = {
            **shared_event,
            "audio_base64": base64.b64encode(wav).decode("ascii"),
            "media_type": "audio/wav",
            "retention_policy": consent["retention_policy"],
        }
        try:
            raw_delivered = await self.publish(raw_event, target="raw_audio")
        except ArchiveSpoolFullError:
            logger.error(
                "raw voice dropped because shared archive spool is full event_id=%s",
                event.get("event_id"),
            )
            return transcript_delivered
        return transcript_delivered and raw_delivered

    async def _active_raw_voice_consent(self, session_id: str) -> dict[str, str] | None:
        if not session_id:
            return None
        try:
            response = await self._client.get(
                self._config.consent_endpoint,
                headers={"X-Memoria-Internal-Token": self._config.internal_token},
                params={"session_id": session_id},
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("allowed") is not True:
            return None
        required = ("consent_grant_id", "retention_policy")
        if any(not isinstance(payload.get(key), str) or not payload[key] for key in required):
            return None
        return {key: str(payload[key]) for key in required}

    @staticmethod
    def _wav(pcm: bytes, *, sample_rate: int) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            writer.writeframes(pcm)
        return output.getvalue()

    async def _append_after_cancellation(self, payload: bytes) -> None:
        async with self._lock:
            async with self._exclusive_spool_lock():
                self._append(payload)

    async def replay(self) -> int:
        async with self._lock:
            async with self._exclusive_spool_lock():
                return await self._replay_locked()

    @asynccontextmanager
    async def _exclusive_spool_lock(self) -> AsyncIterator[None]:
        """Serialize read/replay/replace across LiveKit job processes."""
        lock_path = self._config.spool_path.with_name(
            f".{self._config.spool_path.name}.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    async def _replay_locked(self) -> int:
        encrypted_lines = self._read_lines()
        delivered = 0
        remaining: list[bytes] = []
        for index, token in enumerate(encrypted_lines):
            try:
                payload = self._fernet.decrypt(token)
            except InvalidToken as exc:
                raise ArchiveSpoolKeyError("archive spool key mismatch or corrupt data") from exc
            target, _ = self._decode_envelope(payload)
            if await self._deliver(payload):
                delivered += 1
                continue
            remaining.append(token)
            if target == "event":
                remaining.extend(encrypted_lines[index + 1 :])
                break
        self._replace_lines(remaining)
        return delivered

    async def _deliver(self, payload: bytes) -> bool:
        target, body = self._decode_envelope(payload)
        endpoint = (
            self._config.raw_audio_endpoint if target == "raw_audio" else self._config.endpoint
        )
        try:
            response = await self._client.post(
                endpoint,
                headers={
                    "X-Memoria-Internal-Token": self._config.internal_token,
                    "Content-Type": "application/json",
                },
                content=self._encode(body),
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return False
        if response.status_code in {200, 201, 202}:
            return True
        if response.status_code == 425:
            # Derived evidence can race its canonical parent across retries.
            # Keep it encrypted until the parent turn is durably visible.
            return False
        if target == "raw_audio" and response.status_code in {403, 409, 410, 413, 422}:
            logger.error(
                "raw voice spool row discarded after permanent response status=%s event_id=%s",
                response.status_code,
                body.get("event_id"),
            )
            return True
        if response.status_code == 410:
            # A deleted-session tombstone is permanent. Keeping this row would
            # violate deletion and head-of-line block every later event.
            return True
        if response.status_code >= 500:
            return False
        response.raise_for_status()
        return False  # pragma: no cover - raise_for_status covers other responses

    @staticmethod
    def _decode_envelope(payload: bytes) -> tuple[ArchiveTarget, dict[str, Any]]:
        decoded = json.loads(payload)
        if (
            isinstance(decoded, dict)
            and decoded.get("target") in {"event", "raw_audio"}
            and isinstance(decoded.get("body"), dict)
        ):
            return decoded["target"], decoded["body"]
        if isinstance(decoded, dict):
            # Replay pre-envelope spool rows created by older releases.
            return "event", decoded
        raise ValueError("archive spool payload must be a JSON object")

    @staticmethod
    def _encode(event: dict[str, Any]) -> bytes:
        return json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _read_lines(self) -> list[bytes]:
        path = self._config.spool_path
        if not path.exists():
            return []
        return [line for line in path.read_bytes().splitlines() if line]

    def _append(self, payload: bytes) -> None:
        path = self._config.spool_path
        path.parent.mkdir(parents=True, exist_ok=True)
        token = self._fernet.encrypt(payload) + b"\n"
        current_size = path.stat().st_size if path.exists() else 0
        target, _ = self._decode_envelope(payload)
        if (
            target == "event"
            and current_size + len(token) > self._config.spool_max_bytes
        ):
            kept: list[bytes] = []
            evicted_raw_audio = 0
            for encrypted_line in self._read_lines():
                if current_size + len(token) <= self._config.spool_max_bytes:
                    kept.append(encrypted_line)
                    continue
                try:
                    queued_payload = self._fernet.decrypt(encrypted_line)
                except InvalidToken as exc:
                    raise ArchiveSpoolKeyError(
                        "archive spool key mismatch or corrupt data"
                    ) from exc
                queued_target, _ = self._decode_envelope(queued_payload)
                if queued_target == "raw_audio":
                    current_size -= len(encrypted_line) + 1
                    evicted_raw_audio += 1
                else:
                    kept.append(encrypted_line)
            if evicted_raw_audio:
                self._replace_lines(kept)
                logger.warning(
                    "raw voice evicted to preserve transcript count=%s",
                    evicted_raw_audio,
                )
        if current_size + len(token) > self._config.spool_max_bytes:
            raise ArchiveSpoolFullError(
                f"archive spool reached {self._config.spool_max_bytes} byte cap"
            )
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, token)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _replace_lines(self, lines: list[bytes]) -> None:
        path = self._config.spool_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        data = b"".join(line + b"\n" for line in lines)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)

    async def close(self) -> None:
        try:
            await self.replay()
        finally:
            if self._owns_client:
                await self._client.aclose()
