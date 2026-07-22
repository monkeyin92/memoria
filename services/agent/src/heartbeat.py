"""Process-level Agent heartbeat reported through the Control API."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx

logger = logging.getLogger(__name__)

HEARTBEAT_STATE_PATH = Path("/tmp/memoria-agent-heartbeat.json")
HEARTBEAT_MAX_AGE_S = 30.0


@dataclass(frozen=True, slots=True)
class AgentHeartbeatConfig:
    endpoint: str
    internal_token: str
    release_tag: str
    worker_health_url: str = "http://127.0.0.1:8081/"
    interval_s: float = 10.0
    timeout_s: float = 3.0
    state_path: Path = HEARTBEAT_STATE_PATH

    def __post_init__(self) -> None:
        if (
            not self.endpoint.strip()
            or not self.internal_token.strip()
            or not self.release_tag.strip()
            or not self.worker_health_url.strip()
        ):
            raise ValueError("agent heartbeat requires endpoint, token and release tag")
        if self.interval_s <= 0 or self.timeout_s <= 0:
            raise ValueError("agent heartbeat timing must be positive")


class AgentHeartbeat:
    def __init__(
        self,
        config: AgentHeartbeatConfig,
        *,
        registration_probe: Callable[[], bool],
        boot_id: UUID | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._registration_probe = registration_probe
        self._boot_id = boot_id or uuid4()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._worker_ready = False

    def mark_worker_ready(self, *_: object) -> None:
        self._worker_ready = True

    async def report(self, client: httpx.AsyncClient) -> None:
        livekit_ready = False
        if self._registration_probe():
            try:
                worker_health = await client.get(self._config.worker_health_url)
            except httpx.HTTPError:
                pass
            else:
                livekit_ready = worker_health.is_success
        response = await client.post(
            self._config.endpoint,
            headers={"X-Memoria-Internal-Token": self._config.internal_token},
            json={
                "release_tag": self._config.release_tag,
                "boot_id": str(self._boot_id),
                "worker_ready": self._worker_ready,
                "livekit_ready": livekit_ready,
                "last_loop_at": self._clock().astimezone(UTC).isoformat(),
            },
        )
        response.raise_for_status()
        try:
            accepted = response.json().get("status") == "recorded"
        except (AttributeError, ValueError):
            accepted = False
        if not accepted:
            raise httpx.HTTPStatusError(
                "agent heartbeat was not accepted",
                request=response.request,
                response=response,
            )
        if not self._worker_ready or not livekit_ready:
            return
        _write_heartbeat_state(
            self._config.state_path,
            release_tag=self._config.release_tag,
            accepted_at=self._clock(),
        )

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=self._config.timeout_s) as client:
            while True:
                try:
                    await self.report(client)
                except (httpx.HTTPError, OSError) as exc:
                    logger.warning("agent heartbeat failed: %s", type(exc).__name__)
                await asyncio.sleep(self._config.interval_s)


def _write_heartbeat_state(state_path: Path, *, release_tag: str, accepted_at: datetime) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = state_path.with_name(f".{state_path.name}.{uuid4().hex}.tmp")
    try:
        with temporary_path.open("x", encoding="utf-8") as state_file:
            json.dump(
                {
                    "release_tag": release_tag,
                    "accepted_at": accepted_at.astimezone(UTC).isoformat(),
                },
                state_file,
                separators=(",", ":"),
            )
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, state_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def is_agent_heartbeat_healthy(
    *,
    state_path: Path = HEARTBEAT_STATE_PATH,
    release_tag: str,
    worker_health_url: str = "http://127.0.0.1:8081/",
    now: datetime | None = None,
    max_age_s: float = HEARTBEAT_MAX_AGE_S,
    timeout_s: float = 3.0,
) -> bool:
    """Return whether this Agent has a recent accepted heartbeat and a healthy SDK."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        accepted_at = datetime.fromisoformat(state["accepted_at"])
        state_release = state["release_tag"]
    except (KeyError, OSError, TypeError, ValueError):
        return False
    if not isinstance(state_release, str) or state_release != release_tag or accepted_at.tzinfo is None:
        return False

    age_s = ((now or datetime.now(UTC)) - accepted_at).total_seconds()
    if age_s < 0 or age_s > max_age_s:
        return False

    try:
        with urllib.request.urlopen(worker_health_url, timeout=timeout_s) as response:
            status = response.status
            return isinstance(status, int) and 200 <= status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-health", action="store_true")
    parser.add_argument("--state-path", type=Path, default=HEARTBEAT_STATE_PATH)
    parser.add_argument("--release-tag", default=os.environ.get("MEMORIA_RELEASE_TAG", ""))
    parser.add_argument("--worker-health-url", default="http://127.0.0.1:8081/")
    args = parser.parse_args(argv)
    if not args.check_health:
        parser.error("--check-health is required")
    return int(
        not is_agent_heartbeat_healthy(
            state_path=args.state_path,
            release_tag=args.release_tag,
            worker_health_url=args.worker_health_url,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
