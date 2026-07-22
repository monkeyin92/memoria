"""Agent worker entrypoint (ch.24.4)."""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlsplit, urlunsplit

from services.agent.src.config import load_settings


def _livekit_server_is_registered(server: object) -> bool:
    worker_id = getattr(server, "_id", None)
    return (
        isinstance(worker_id, str)
        and worker_id not in {"", "unregistered"}
        and getattr(server, "_closed", True) is False
        and getattr(server, "_connecting", True) is False
        and getattr(server, "_connection_failed", True) is False
    )


def main() -> None:
    settings = load_settings(require_keys=True)
    # Offline health: allow import without starting LiveKit CLI.
    if settings.offline_mock and len(sys.argv) == 1:
        print("agent offline mode: not starting LiveKit worker")
        return

    from livekit import agents

    from services.agent.src.agent import entrypoint, prewarm

    server = agents.AgentServer(setup_fnc=prewarm)
    server.rtc_session(
        entrypoint,
        agent_name=settings.livekit_agent_name,
    )
    if settings.environment == "production":
        from services.agent.src.heartbeat import AgentHeartbeat, AgentHeartbeatConfig

        archive_url = urlsplit(settings.archive_session_events_url)
        heartbeat = AgentHeartbeat(
            AgentHeartbeatConfig(
                endpoint=urlunsplit(
                    (
                        archive_url.scheme,
                        archive_url.netloc,
                        "/internal/readiness/agent-heartbeat",
                        "",
                        "",
                    )
                ),
                internal_token=settings.internal_token("agent_heartbeat"),
                release_tag=os.environ.get("MEMORIA_RELEASE_TAG", "development"),
            ),
            registration_probe=lambda: _livekit_server_is_registered(server),
        )
        heartbeat_task: asyncio.Task[None] | None = None

        def worker_started(*_: object) -> None:
            nonlocal heartbeat_task
            heartbeat.mark_worker_ready()
            heartbeat_task = asyncio.create_task(heartbeat.run(), name="agent-heartbeat")

        server.on("worker_started", worker_started)
    agents.cli.run_app(server)


if __name__ == "__main__":
    main()
