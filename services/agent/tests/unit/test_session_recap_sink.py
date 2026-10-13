"""Session-end recap sink: draft, discard path, and HTTP ingest shape."""

from __future__ import annotations

import json

import httpx
import pytest
from services.agent.src.orchestration.context_manager import ContextManager
from services.agent.src.session_recap_sink import (
    HttpSessionRecapConfig,
    SessionRecapSink,
    snapshot_context_turns,
)
from services.guardian.session_recap import SessionTurn


@pytest.mark.asyncio
async def test_publish_posts_summary_and_facts_without_transcript() -> None:
    posted: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "skipped": False,
                "summary_record_id": "dlgsum:1",
                "archive_record_ids": ["a"],
            },
        )

    sink = SessionRecapSink(
        HttpSessionRecapConfig(
            endpoint="http://control.test/v1/internal/child-memory/session-recap",
            internal_token="token-that-is-long-enough",
        ),
        _client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await sink.publish_session_end(
        subject_id="child-1",
        resource_owner_id="parent-1",
        session_id="sess-1",
        turns=[
            SessionTurn(role="user", text="我想听恐龙"),
            SessionTurn(role="assistant", text="好呀"),
            SessionTurn(role="user", text="还有小狗"),
            SessionTurn(role="assistant", text="可爱"),
        ],
    )
    await sink.aclose()
    assert result is not None
    assert result["summary_record_id"] == "dlgsum:1"
    assert posted[0]["subject_id"] == "child-1"
    assert isinstance(posted[0]["summary_text"], str) and posted[0]["summary_text"]
    assert "transcript" not in posted[0]
    assert "text" not in posted[0]
    assert any(item["text"] == "喜欢恐龙" for item in posted[0]["archive_facts"])  # type: ignore[index]


@pytest.mark.asyncio
async def test_publish_skips_short_session_without_posting() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    sink = SessionRecapSink(
        HttpSessionRecapConfig(
            endpoint="http://control.test/v1/internal/child-memory/session-recap",
            internal_token="token-that-is-long-enough",
        ),
        _client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await sink.publish_session_end(
        subject_id="child-1",
        resource_owner_id="parent-1",
        session_id="sess-1",
        turns=[
            SessionTurn(role="user", text="你好"),
            SessionTurn(role="assistant", text="嗨"),
        ],
    )
    await sink.aclose()
    assert result == {
        "skipped": True,
        "reason": "fewer_than_two_turns",
        "summary_record_id": None,
        "archive_record_ids": [],
    }
    assert calls == 0


def test_snapshot_then_clear_discards_transcript() -> None:
    manager = ContextManager(system_prompt="sys")
    manager.add_user("第一句", speaker_scope="owner")
    manager.commit_assistant_heard("好", speaker_scope="owner")
    manager.add_user("第二句恐龙", speaker_scope="owner")
    snapped = snapshot_context_turns(manager.turns)
    manager.clear_turns()
    assert manager.turns == []
    assert [turn.text for turn in snapped if turn.role == "user"] == ["第一句", "第二句恐龙"]
