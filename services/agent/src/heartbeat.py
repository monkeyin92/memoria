"""Voice-core media bridge heartbeat reported through the Control API.

The bridge process posts ``{release_tag, boot_id, worker_ready, last_loop_at,
chat_llm_providers}`` to ``/internal/readiness/agent-heartbeat``.
``worker_ready`` is true only while the bridge's gRPC server is serving.
Each accepted ready heartbeat refreshes a local state file,
which the container healthcheck reads through
``python -m services.agent.src.heartbeat --check-health``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx

from services.common.chat_llm_providers import ChatLlmFamily, chat_llm_plan
from services.common.failure_log import failure_log
from services.common.voice_engines import VoiceEngines

logger = logging.getLogger(__name__)

HEARTBEAT_PATH = "/internal/readiness/agent-heartbeat"
HEARTBEAT_STATE_PATH = Path("/tmp/memoria-agent-heartbeat.json")
HEARTBEAT_MAX_AGE_S = 30.0


@dataclass(frozen=True, slots=True)
class AgentHeartbeatConfig:
    endpoint: str
    internal_token: str
    release_tag: str
    chat_llm_providers: tuple[ChatLlmFamily, ...]
    # What this process built, so Control can name a split where the two env
    # files selected different engines instead of vouching for a smoke that
    # tested the other one.
    engines: str = ""
    interval_s: float = 10.0
    timeout_s: float = 3.0
    state_path: Path = HEARTBEAT_STATE_PATH

    def __post_init__(self) -> None:
        if (
            not self.endpoint.strip()
            or not self.internal_token.strip()
            or not self.release_tag.strip()
        ):
            raise ValueError("agent heartbeat requires endpoint, token and release tag")
        if not self.chat_llm_providers:
            raise ValueError("agent heartbeat requires the chat model families it builds")
        if self.interval_s <= 0 or self.timeout_s <= 0:
            raise ValueError("agent heartbeat timing must be positive")


class AgentHeartbeat:
    def __init__(
        self,
        config: AgentHeartbeatConfig,
        *,
        boot_id: UUID | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._boot_id = boot_id or uuid4()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._worker_ready = False
        self._control_accepts_engines: bool | None = None

    def mark_worker_ready(self) -> None:
        self._worker_ready = True

    def mark_worker_stopped(self) -> None:
        self._worker_ready = False

    def _payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "release_tag": self._config.release_tag,
            "boot_id": str(self._boot_id),
            "worker_ready": self._worker_ready,
            "last_loop_at": self._clock().astimezone(UTC).isoformat(),
            "chat_llm_providers": list(self._config.chat_llm_providers),
        }
        if self._config.engines and self._control_accepts_engines is not False:
            payload["engines"] = self._config.engines
        return payload

    async def _post(self, client: httpx.AsyncClient) -> httpx.Response:
        response = await client.post(
            self._config.endpoint,
            headers={"X-Memoria-Internal-Token": self._config.internal_token},
            json=self._payload(),
        )
        if response.status_code == 422 and self._config.engines and self._control_accepts_engines is not False:
            self._control_accepts_engines = False
            response = await client.post(
                self._config.endpoint,
                headers={"X-Memoria-Internal-Token": self._config.internal_token},
                json=self._payload(),
            )
        elif response.is_success and self._config.engines:
            self._control_accepts_engines = True
        return response

    async def report(self, client: httpx.AsyncClient) -> None:
        worker_ready = self._worker_ready
        response = await self._post(client)
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
        if not worker_ready:
            return
        _write_heartbeat_state(
            self._config.state_path,
            release_tag=self._config.release_tag,
            accepted_at=self._clock(),
        )

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=self._config.timeout_s) as client:
            await self.run_with_client(client)

    async def run_with_client(self, client: httpx.AsyncClient) -> None:
        while True:
            try:
                await self.report(client)
            except httpx.HTTPStatusError as exc:
                # Control API answers 409 when its own MEMORIA_RELEASE_TAG
                # differs from the one reported here. Without the status and the
                # reported tag, a permanent stack tag split is indistinguishable
                # from Control API being unreachable.
                logger.warning(
                    "agent heartbeat rejected %s release_tag=%s",
                    failure_log(exc),
                    self._config.release_tag,
                )
            except (httpx.HTTPError, OSError) as exc:
                logger.warning("agent heartbeat failed: %s", type(exc).__name__)
            await asyncio.sleep(self._config.interval_s)


def heartbeat_endpoint(archive_session_events_url: str) -> str:
    """Address the Control API readiness route on the archive endpoint's host."""

    archive_url = urlsplit(archive_session_events_url)
    return urlunsplit((archive_url.scheme, archive_url.netloc, HEARTBEAT_PATH, "", ""))


def build_agent_heartbeat(settings: Any) -> AgentHeartbeat | None:
    """Production bridges report readiness; other environments stay silent."""

    if getattr(settings, "environment", "development") != "production":
        return None
    return AgentHeartbeat(
        AgentHeartbeatConfig(
            endpoint=heartbeat_endpoint(settings.archive_session_events_url),
            internal_token=settings.internal_token("agent_heartbeat"),
            release_tag=os.environ.get("MEMORIA_RELEASE_TAG", "development"),
            chat_llm_providers=chat_llm_plan(
                llm_provider=settings.llm_provider,
                llm_failover_enabled=settings.llm_failover_enabled,
            ).families,
            engines=VoiceEngines.from_settings(settings).smoke_line(),
        )
    )


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


def _tcp_port_open(address: str, *, timeout_s: float) -> bool:
    host, separator, port = address.rpartition(":")
    if not separator or not host or not port.isdigit():
        return False
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout_s):
            return True
    except OSError:
        return False


def is_agent_heartbeat_healthy(
    *,
    state_path: Path = HEARTBEAT_STATE_PATH,
    release_tag: str,
    probe_addr: str | None = None,
    now: datetime | None = None,
    max_age_s: float = HEARTBEAT_MAX_AGE_S,
    timeout_s: float = 3.0,
) -> bool:
    """Return whether the bridge has a recent accepted ready heartbeat.

    ``probe_addr`` (``host:port``) additionally requires the gRPC listener to
    accept a TCP connection.
    """
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
    return probe_addr is None or _tcp_port_open(probe_addr, timeout_s=timeout_s)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-health", action="store_true")
    parser.add_argument("--state-path", type=Path, default=HEARTBEAT_STATE_PATH)
    parser.add_argument("--release-tag", default=os.environ.get("MEMORIA_RELEASE_TAG", ""))
    parser.add_argument("--probe-addr", default=None)
    args = parser.parse_args(argv)
    if not args.check_health:
        parser.error("--check-health is required")
    return int(
        not is_agent_heartbeat_healthy(
            state_path=args.state_path,
            release_tag=args.release_tag,
            probe_addr=args.probe_addr,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
