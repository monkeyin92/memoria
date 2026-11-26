"""The production HTTP client's timeout at a real TCP boundary."""

import asyncio
import json

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.response_planner_client import (
    ResponsePlannerClient,
    ResponsePlannerClientConfig,
)
from services.agent.tests.unit.test_response_planner_client import _plan_payload, _speaker


@pytest.mark.parametrize("delay, expected", [(0, "ok"), (0.6, "timeout")])
async def test_actual_network_timeout(delay, expected):
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            await asyncio.sleep(delay)
            body = json.dumps(_plan_payload()).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    client = ResponsePlannerClient(
        ResponsePlannerClientConfig(
            endpoint=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/plan",
            internal_token="synthetic-local-token",
            timeout_s=0.3,
        )
    )
    try:
        result = await client.fetch(
            session_id="session-1",
            query="杭州",
            fence=GenerationFence("session-1", 1, 2, 0),
            speaker_decision=_speaker(),
        )
        assert result.reason == expected
        assert result.available == (expected == "ok")
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
