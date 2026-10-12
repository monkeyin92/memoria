"""Child for the P2-04 bridge process-exit harness.

This is not a product entrypoint. It runs the real ``scripts.run_media_bridge.run``
coroutine (the body of ``main``) and, only if that coroutine returns, prints
leftover asyncio task names. SIGKILL never reaches the print.

The child refuses production settings, mTLS, and any session/provider/runtime
factory so a bad parent environment cannot attach FunASR or a live agent.
"""

from __future__ import annotations

import asyncio
import json
import os

from scripts.run_media_bridge import run

_FACTORY_ENV = (
    "MEDIA_BRIDGE_SESSION_FACTORY",
    "MEDIA_BRIDGE_PROVIDER_FACTORY",
    "MEDIA_BRIDGE_RUNTIME_FACTORY",
)
_ENABLED = {"1", "true", "yes", "on"}


def _refuse_unless_offline() -> None:
    if os.environ.get("ENVIRONMENT") != "development":
        raise SystemExit("process-exit child refuses to start outside development")
    if os.environ.get("MEDIA_BRIDGE_MTLS", "").lower() in _ENABLED:
        raise SystemExit("process-exit child refuses mTLS")
    if os.environ.get("MEDIA_BRIDGE_GRPC_ENABLED", "").lower() not in _ENABLED:
        raise SystemExit("process-exit child requires MEDIA_BRIDGE_GRPC_ENABLED")
    for name in _FACTORY_ENV:
        if os.environ.get(name, "").strip():
            raise SystemExit(f"process-exit child refuses {name}")


async def _observed_run() -> None:
    await run()
    current = asyncio.current_task()
    leftover = sorted(
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    )
    print(
        "MEMORIA_PROCESS_RECLAIM " + json.dumps({"leftover_tasks": leftover}),
        flush=True,
    )


def main() -> None:
    _refuse_unless_offline()
    asyncio.run(_observed_run())


if __name__ == "__main__":
    main()
