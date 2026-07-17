"""Agent worker entrypoint (ch.24.4)."""

from __future__ import annotations

import sys

from services.agent.src.config import load_settings


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
    agents.cli.run_app(server)


if __name__ == "__main__":
    main()
