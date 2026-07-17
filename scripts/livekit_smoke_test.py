#!/usr/bin/env python3
"""Read-only authentication smoke for the configured LiveKit project."""

from __future__ import annotations

import asyncio
import os
import sys

from livekit import api


async def main() -> int:
    required = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        print("livekit_smoke_test FAIL: missing " + ", ".join(missing))
        return 1

    try:
        async with api.LiveKitAPI(
            os.environ["LIVEKIT_URL"],
            os.environ["LIVEKIT_API_KEY"],
            os.environ["LIVEKIT_API_SECRET"],
        ) as livekit:
            await livekit.room.list_rooms(api.ListRoomsRequest())
    except Exception as exc:
        print(f"livekit_smoke_test FAIL: {type(exc).__name__}")
        return 1

    print("livekit_smoke_test PASS: authenticated room-service access")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
