from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from services.agent.src.archive_sink import ArchiveSink, ArchiveSinkConfig
from services.agent.src.observability.metrics import MetricsRegistry


@pytest.mark.asyncio
async def test_failed_delivery_is_encrypted_and_replayed_idempotently(tmp_path: Path) -> None:
    attempts: list[dict[str, object]] = []
    available = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal available
        if not available:
            return httpx.Response(503)
        attempts.append(dict(request.read() and __import__("json").loads(request.content)))
        return httpx.Response(201, json={"duplicate": False})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    spool_path = tmp_path / "archive.spool"
    config = ArchiveSinkConfig(
        endpoint="https://control.test/v1/archive/session-events",
        internal_token="internal-test-token",
        spool_path=spool_path,
        spool_key=Fernet.generate_key().decode("ascii"),
        spool_max_bytes=64 * 1024,
    )
    sink = ArchiveSink(config, client=client)
    event = {
        "event_id": "event-001",
        "session_id": "session-001",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "这句话不能明文落盘。"},
    }

    assert await sink.publish(event) is False
    encrypted = spool_path.read_bytes()
    assert encrypted
    assert "这句话不能明文落盘".encode() not in encrypted

    available = True
    assert await sink.replay() == 1
    assert attempts == [event]
    assert spool_path.read_bytes() == b""
    await client.aclose()


@pytest.mark.asyncio
async def test_parent_not_recorded_response_remains_spooled_for_retry(tmp_path: Path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(425))
    )
    spool_path = tmp_path / "archive.spool"
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=Fernet.generate_key().decode("ascii"),
            spool_max_bytes=64 * 1024,
        ),
        client=client,
    )
    event = {
        "event_id": "assistant-before-parent",
        "session_id": "session-001",
        "event_type": "assistant.playout_stopped",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "assistant",
        "source": "generation_fence.actual_heard",
        "turn_id": 1,
        "generation_id": 1,
        "payload": {"text": "必须等待父话轮。", "actual_heard": True},
    }

    assert await sink.publish(event) is False
    encrypted = spool_path.read_bytes()
    assert encrypted
    assert await sink.replay() == 0
    assert spool_path.read_bytes() == encrypted
    await client.aclose()


@pytest.mark.asyncio
async def test_new_parent_event_unblocks_an_older_425_child(tmp_path: Path) -> None:
    parent_recorded = False
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal parent_recorded
        event = json.loads(request.content)
        event_id = str(event["event_id"])
        calls.append(event_id)
        if event_id == "parent-turn":
            parent_recorded = True
            return httpx.Response(201)
        if event_id == "assistant-child" and not parent_recorded:
            return httpx.Response(425)
        return httpx.Response(201)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    spool_path = tmp_path / "archive.spool"
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=Fernet.generate_key().decode("ascii"),
            spool_max_bytes=64 * 1024,
        ),
        client=client,
    )
    assistant = {
        "event_id": "assistant-child",
        "session_id": "session-001",
        "event_type": "assistant.playout_stopped",
        "occurred_at": "2026-07-19T08:00:01+00:00",
        "speaker_class": "assistant",
        "source": "generation_fence.actual_heard",
        "turn_id": 1,
        "generation_id": 1,
        "payload": {"text": "等待父话轮。", "actual_heard": True},
    }
    parent = {
        "event_id": "parent-turn",
        "session_id": "session-001",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "turn_id": 1,
        "generation_id": 1,
        "payload": {"text": "父话轮。", "persona_eligible": True},
    }

    assert await sink.publish(assistant) is False
    assert await sink.publish(parent) is True
    assert parent_recorded is True
    assert spool_path.read_bytes() == b""
    assert calls == [
        "assistant-child",
        "assistant-child",
        "parent-turn",
        "assistant-child",
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_deleted_session_is_discarded_without_blocking_later_spooled_events(
    tmp_path: Path,
) -> None:
    online = False

    def handler(request: httpx.Request) -> httpx.Response:
        event = json.loads(request.content)
        if not online:
            return httpx.Response(503)
        if event["session_id"] == "deleted-session":
            return httpx.Response(410)
        return httpx.Response(201, json={"duplicate": False})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    spool_path = tmp_path / "archive.spool"
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=Fernet.generate_key().decode("ascii"),
            spool_max_bytes=64 * 1024,
        ),
        client=client,
    )
    base = {
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "待重放证据"},
    }
    assert await sink.publish(
        {**base, "event_id": "deleted-event", "session_id": "deleted-session"}
    ) is False
    assert await sink.publish(
        {**base, "event_id": "active-event", "session_id": "active-session"}
    ) is False

    online = True
    assert await sink.replay() == 2
    assert spool_path.read_bytes() == b""
    await client.aclose()


@pytest.mark.asyncio
async def test_close_replays_a_deleted_sessions_spooled_event(tmp_path: Path) -> None:
    deleted = False

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(410 if deleted else 503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    spool_path = tmp_path / "archive.spool"
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=Fernet.generate_key().decode("ascii"),
            spool_max_bytes=64 * 1024,
        ),
        client=client,
    )
    assert await sink.publish(
        {
            "event_id": "deleted-event",
            "session_id": "deleted-session",
            "event_type": "speech.utterance_finalized",
            "occurred_at": "2026-07-19T08:00:00+00:00",
            "speaker_class": "owner",
            "source": "funasr.authoritative_final",
            "payload": {"text": "删除时不得留在 spool"},
        }
    ) is False

    deleted = True
    await sink.close()

    assert spool_path.read_bytes() == b""
    await client.aclose()


@pytest.mark.asyncio
async def test_shared_spool_does_not_lose_append_during_another_process_replay(
    tmp_path: Path,
) -> None:
    """Two LiveKit job processes may share the bind-mounted production spool."""
    replay_started = asyncio.Event()
    finish_replay = asyncio.Event()
    sink_one_online = False

    async def first_handler(_: httpx.Request) -> httpx.Response:
        if not sink_one_online:
            return httpx.Response(503)
        replay_started.set()
        await finish_replay.wait()
        return httpx.Response(201, json={"duplicate": False})

    async def second_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    spool_path = tmp_path / "archive.spool"
    spool_key = Fernet.generate_key().decode("ascii")
    config = ArchiveSinkConfig(
        endpoint="https://control.test/v1/archive/session-events",
        internal_token="internal-test-token",
        spool_path=spool_path,
        spool_key=spool_key,
        spool_max_bytes=64 * 1024,
    )
    client_one = httpx.AsyncClient(transport=httpx.MockTransport(first_handler))
    client_two = httpx.AsyncClient(transport=httpx.MockTransport(second_handler))
    sink_one = ArchiveSink(config, client=client_one)
    sink_two = ArchiveSink(config, client=client_two)
    base = {
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "并发证据"},
    }

    assert await sink_one.publish(
        {**base, "event_id": "event-one", "session_id": "session-one"}
    ) is False
    sink_one_online = True
    replay_task = asyncio.create_task(sink_one.replay())
    await replay_started.wait()
    publish_task = asyncio.create_task(
        sink_two.publish({**base, "event_id": "event-two", "session_id": "session-two"})
    )
    await asyncio.sleep(0)
    finish_replay.set()

    assert await replay_task == 1
    assert await publish_task is False
    encrypted_lines = [line for line in spool_path.read_bytes().splitlines() if line]
    decrypted = [
        json.loads(Fernet(spool_key.encode("ascii")).decrypt(line)) for line in encrypted_lines
    ]
    assert [envelope["body"]["event_id"] for envelope in decrypted] == ["event-two"]
    assert [envelope["target"] for envelope in decrypted] == ["event"]

    await client_one.aclose()
    await client_two.aclose()


@pytest.mark.asyncio
async def test_cancelled_delivery_is_durable_before_cancellation_propagates(
    tmp_path: Path,
) -> None:
    delivery_started = asyncio.Event()
    never_respond = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        delivery_started.set()
        await never_respond.wait()
        return httpx.Response(201)  # pragma: no cover - cancellation is the scenario

    spool_path = tmp_path / "archive.spool"
    spool_key = Fernet.generate_key().decode("ascii")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=spool_key,
            spool_max_bytes=64 * 1024,
        ),
        client=client,
    )
    event = {
        "event_id": "cancelled-event",
        "session_id": "closing-session",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "即使关闭会话也不能丢"},
    }

    publish_task = asyncio.create_task(sink.publish(event))
    await delivery_started.wait()
    publish_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await publish_task

    encrypted_lines = [line for line in spool_path.read_bytes().splitlines() if line]
    assert [
        json.loads(Fernet(spool_key.encode("ascii")).decrypt(line)) for line in encrypted_lines
    ] == [{"target": "event", "body": event}]
    await client.aclose()
@pytest.mark.asyncio
async def test_owner_turn_attaches_consent_without_raw_audio_upload(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "allowed": True,
                    "consent_grant_id": "raw-grant-001",
                    "policy_version": "raw-voice-archive-v1",
                    "retention_policy": "account_lifetime",
                },
            )
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        return httpx.Response(201, json={"duplicate": False})

    spool_path = tmp_path / "archive.spool"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=Fernet.generate_key().decode("ascii"),
            spool_max_bytes=512 * 1024,
        ),
        client=client,
    )
    event = {
        "event_id": "owner-event",
        "session_id": "session-owner",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "这段声音经过了单独授权。"},
    }

    assert await sink.publish_owner_turn(event, pcm=b"\x00\x00" * 1600, sample_rate=16_000)
    assert requests == [
        (
            "/v1/archive/session-events",
            {**event, "consent_grant_id": "raw-grant-001"},
        )
    ]
    assert not spool_path.exists() or spool_path.read_bytes() == b""
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_raw_consent_keeps_the_transcript_without_audio(
    tmp_path: Path,
) -> None:
    consent_allowed = False
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=(
                    {
                        "allowed": True,
                        "consent_grant_id": "live-grant",
                        "policy_version": "raw-voice-archive-v1",
                        "retention_policy": "account_lifetime",
                    }
                    if consent_allowed
                    else {"allowed": False}
                ),
            )
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        return httpx.Response(201)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=tmp_path / "archive.spool",
            spool_key=Fernet.generate_key().decode("ascii"),
        ),
        client=client,
    )
    event = {
        "event_id": "owner-event",
        "session_id": "session-owner",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "转写要保留，原始声音默认不保存。"},
    }

    assert await sink.publish_owner_turn(event, pcm=b"\x00\x00" * 1600, sample_rate=16_000)
    assert requests[-1] == (
        "/v1/archive/session-events",
        event,
    )

    consent_allowed = True
    assert await sink.publish_owner_turn(
        {**event, "event_id": "consented-owner-event"},
        pcm=b"\x00\x00" * 1600,
        sample_rate=16_000,
    )
    assert requests[-1] == (
        "/v1/archive/session-events",
        {
            **event,
            "event_id": "consented-owner-event",
            "consent_grant_id": "live-grant",
        },
    )
    assert all(not path.endswith("/session-raw-audio") for path, _ in requests)
    await client.aclose()



@pytest.mark.asyncio
async def test_minor_corpus_consent_never_uploads_raw_audio(tmp_path: Path) -> None:
    """Scheme A: child corpus consent must not persist conversation audio."""

    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "allowed": True,
                    "subject_category": "minor",
                    "archive_purpose": "corpus_recording",
                    "consent_grant_id": "corpus-consent-001",
                    "retention_policy": "corpus_time_bounded",
                },
            )
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=tmp_path / "archive.spool",
            spool_key=Fernet.generate_key().decode("ascii"),
        ),
        client=client,
    )
    event = {
        "event_id": "minor-corpus-event",
        "session_id": "minor-session",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-08-09T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "授权语料的转写仍按普通未成年人边界处理。"},
    }

    assert await sink.publish_owner_turn(
        event,
        pcm=b"\x00\x00" * 1600,
        sample_rate=16_000,
    )
    assert requests == [("/v1/archive/session-events", event)]
    await client.aclose()

def _seed_encrypted_spool(path: Path, key: str, envelopes: list[dict[str, object]]) -> None:
    fernet = Fernet(key.encode("ascii"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            fernet.encrypt(
                json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            + b"\n"
            for envelope in envelopes
        )
    )


@pytest.mark.asyncio
async def test_leftover_raw_audio_spool_rows_are_discarded_without_blocking_transcripts(
    tmp_path: Path,
) -> None:
    online = False
    delivered_events: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not online:
            return httpx.Response(503)
        delivered_events.append(str(body["event_id"]))
        return httpx.Response(201)

    spool_path = tmp_path / "archive.spool"
    spool_key = Fernet.generate_key().decode("ascii")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=spool_key,
            spool_max_bytes=256 * 1024,
        ),
        client=client,
    )
    base = {
        "session_id": "session-owner",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "保留转写。"},
    }
    _seed_encrypted_spool(
        spool_path,
        spool_key,
        [
            {
                "target": "raw_audio",
                "body": {
                    **base,
                    "event_id": "stale-raw",
                    "consent_grant_id": "stale-grant",
                    "audio_base64": base64.b64encode(b"RIFF-stale").decode("ascii"),
                    "media_type": "audio/wav",
                    "retention_policy": "account_lifetime",
                },
            },
            {"target": "event", "body": {**base, "event_id": "later-text"}},
        ],
    )
    online = True
    assert await sink.replay() == 2
    assert delivered_events == ["later-text"]
    assert spool_path.read_bytes() == b""
    await client.aclose()



@pytest.mark.asyncio
async def test_raw_spool_overflow_never_removes_the_already_spooled_transcript(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "allowed": True,
                    "consent_grant_id": "raw-grant",
                    "retention_policy": "account_lifetime",
                },
            )
        return httpx.Response(503)

    spool_path = tmp_path / "archive.spool"
    spool_key = Fernet.generate_key().decode("ascii")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=spool_key,
            spool_max_bytes=4096,
        ),
        client=client,
    )
    event = {
        "event_id": "spool-pressure",
        "session_id": "session-owner",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "空间不足时优先保留转写。"},
    }

    assert await sink.publish_owner_turn(
        event,
        pcm=b"\x00\x00" * 4000,
        sample_rate=16_000,
    ) is False
    lines = [line for line in spool_path.read_bytes().splitlines() if line]
    assert len(lines) == 1
    envelope = json.loads(Fernet(spool_key.encode("ascii")).decrypt(lines[0]))
    assert envelope == {
        "target": "event",
        "body": {**event, "consent_grant_id": "raw-grant"},
    }
    await client.aclose()
@pytest.mark.asyncio
async def test_existing_raw_audio_is_evicted_before_a_new_transcript_is_lost(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "allowed": True,
                    "consent_grant_id": "raw-grant",
                    "retention_policy": "account_lifetime",
                },
            )
        return httpx.Response(503)

    spool_path = tmp_path / "archive.spool"
    spool_key = Fernet.generate_key().decode("ascii")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=spool_key,
            spool_max_bytes=4096,
        ),
        client=client,
    )
    base = {
        "session_id": "session-owner",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "权威转写优先于可选原始音频。"},
    }
    _seed_encrypted_spool(
        spool_path,
        spool_key,
        [
            {
                "target": "raw_audio",
                "body": {
                    **base,
                    "event_id": "old-raw",
                    "consent_grant_id": "raw-grant",
                    "audio_base64": "A" * 2600,
                    "media_type": "audio/wav",
                    "retention_policy": "account_lifetime",
                },
            }
        ],
    )

    new_event = {**base, "event_id": "new-transcript"}
    assert await sink.publish_owner_turn(
        new_event,
        pcm=b"\x00\x00" * 4000,
        sample_rate=16_000,
    ) is False

    encrypted_lines = [line for line in spool_path.read_bytes().splitlines() if line]
    envelopes = [
        json.loads(Fernet(spool_key.encode("ascii")).decrypt(line))
        for line in encrypted_lines
    ]
    assert envelopes == [
        {
            "target": "event",
            "body": {**new_event, "consent_grant_id": "raw-grant"},
        }
    ]
    await client.aclose()



@pytest.mark.asyncio
async def test_cancelled_consent_query_shields_the_transcript_delivery(tmp_path: Path) -> None:
    query_started = asyncio.Event()
    never_finish = asyncio.Event()
    posted: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            query_started.set()
            await never_finish.wait()
            return httpx.Response(200, json={"allowed": False})
        posted.append(json.loads(request.content))
        return httpx.Response(201)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=tmp_path / "archive.spool",
            spool_key=Fernet.generate_key().decode("ascii"),
        ),
        client=client,
    )
    event = {
        "event_id": "cancel-query",
        "session_id": "session-owner",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "取消授权查询也不能丢转写。"},
    }
    task = asyncio.create_task(
        sink.publish_owner_turn(event, pcm=b"\x00\x00" * 1600, sample_rate=16_000)
    )
    await query_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert posted == [event]
    await client.aclose()


@pytest.mark.asyncio
async def test_archive_event_id_is_sent_once(tmp_path: Path) -> None:
    posts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(201)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=tmp_path / "archive.spool",
            spool_key=Fernet.generate_key().decode("ascii"),
            spool_max_bytes=64 * 1024,
        ),
        client=client,
    )
    event = {
        "event_id": "event-dedupe",
        "session_id": "session-001",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "同一条只写一次。"},
    }

    assert await sink.publish(event) is True
    assert await sink.publish(event) is True
    assert posts == [event]
    await client.aclose()


@pytest.mark.asyncio
async def test_existing_archive_conflict_is_a_completed_idempotent_write(tmp_path: Path) -> None:
    spool_path = tmp_path / "archive.spool"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"detail": "event_id 'event-conflict' already has different content"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=Fernet.generate_key().decode("ascii"),
            spool_max_bytes=64 * 1024,
        ),
        client=client,
    )
    event = {
        "event_id": "event-conflict",
        "session_id": "session-001",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "服务端已有不可变版本。"},
    }

    assert await sink.publish(event) is True
    assert not spool_path.exists() or spool_path.read_bytes() == b""
    assert sink.retained_conflicts == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_event_id_conflict_during_replay_clears_the_spool(tmp_path: Path) -> None:
    online = False

    def handler(_: httpx.Request) -> httpx.Response:
        if not online:
            return httpx.Response(503)
        return httpx.Response(
            409,
            json={"detail": "event_id 'event-retried' already has different content"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    spool_path = tmp_path / "archive.spool"
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=Fernet.generate_key().decode("ascii"),
            spool_max_bytes=64 * 1024,
        ),
        client=client,
    )
    event = {
        "event_id": "event-retried",
        "session_id": "session-001",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "重放时服务端已存在。"},
    }

    assert await sink.publish(event) is False
    assert spool_path.read_bytes()
    online = True
    assert await sink.replay() == 1
    assert spool_path.read_bytes() == b""
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail",
    (
        {"code": "ambiguous_parent_turn"},
        {"code": "parent_turn_not_canonical"},
        {"code": "turn_event_conflict"},
        {"code": "archive_subject_fence_stale"},
    ),
)
async def test_non_idempotent_conflict_stays_in_spool_and_alerts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    detail: dict[str, str],
) -> None:
    spool_path = tmp_path / "archive.spool"
    metrics = MetricsRegistry()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": detail})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=Fernet.generate_key().decode("ascii"),
            spool_max_bytes=64 * 1024,
        ),
        client=client,
        metrics=metrics,
    )
    event = {
        "event_id": "event-conflict-open",
        "session_id": "session-001",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "服务端拒绝了这条留证。"},
    }

    with caplog.at_level(logging.ERROR, logger="services.agent.src.archive_sink"):
        assert await sink.publish(event) is False
        assert spool_path.read_bytes()
        assert await sink.replay() == 0

    assert spool_path.read_bytes()
    assert sink.retained_conflicts == 2
    assert metrics.get(
        "archive_conflict_retained_total", {"reason": detail["code"]}
    ) == 2
    assert "archive conflict retained in spool" in caplog.text
    assert detail["code"] in caplog.text
    await client.aclose()


@pytest.mark.asyncio
async def test_too_early_is_not_a_terminal_write_and_retries(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(201) if calls > 1 else httpx.Response(425)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    spool_path = tmp_path / "archive.spool"
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=Fernet.generate_key().decode("ascii"),
            spool_max_bytes=64 * 1024,
        ),
        client=client,
    )
    event = {
        "event_id": "event-too-early",
        "session_id": "session-001",
        "event_type": "assistant.playout_stopped",
        "occurred_at": "2026-07-19T08:00:01+00:00",
        "speaker_class": "assistant",
        "source": "generation_fence.actual_heard",
        "turn_id": 1,
        "generation_id": 1,
        "payload": {"text": "等待父话轮。", "actual_heard": True},
    }

    assert await sink.publish(event) is False
    assert await sink.replay() == 1
    assert calls == 2
    assert spool_path.read_bytes() == b""
    await client.aclose()


@pytest.mark.asyncio
async def test_spooled_duplicate_event_ids_are_delivered_once(tmp_path: Path) -> None:
    posts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(201)

    spool_path = tmp_path / "archive.spool"
    spool_key = Fernet.generate_key().decode("ascii")
    event = {
        "event_id": "event-spool-duplicate",
        "session_id": "session-001",
        "event_type": "speech.utterance_finalized",
        "occurred_at": "2026-07-19T08:00:00+00:00",
        "speaker_class": "owner",
        "source": "funasr.authoritative_final",
        "payload": {"text": "重连重复写。"},
    }
    _seed_encrypted_spool(
        spool_path,
        spool_key,
        [
            {"target": "event", "body": event},
            {"target": "event", "body": event},
        ],
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="https://control.test/v1/archive/session-events",
            internal_token="internal-test-token",
            spool_path=spool_path,
            spool_key=spool_key,
            spool_max_bytes=64 * 1024,
        ),
        client=client,
    )

    assert await sink.replay() == 1
    assert posts == 1
    assert spool_path.read_bytes() == b""
    await client.aclose()
