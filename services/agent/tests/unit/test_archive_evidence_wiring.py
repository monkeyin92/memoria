from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from services.agent.src.archive_sink import ArchiveSink, ArchiveSinkConfig
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


def _owner_decision() -> SpeakerDecision:
    return SpeakerDecision(
        classification="owner",
        score=0.95,
        quality_score=0.9,
        reason_code="owner_match",
        model_version="campplus-test",
        template_version=1,
        profile_id="owner-profile",
        permissions=permissions_for_speaker("owner"),
    )


def _provenance(*, owner_projection_eligible: bool = False) -> dict[str, object]:
    return {
        "interaction_mode": "companion" if owner_projection_eligible else "unavailable",
        "mode_policy_version": "test-policy" if owner_projection_eligible else "unavailable",
        "simulated_output": False,
        "history_eligible": owner_projection_eligible,
        "owner_projection_eligible": owner_projection_eligible,
    }


def _enable_owner_projection(runtime: DuplexRuntime) -> None:
    runtime.set_mode_policy(
        ModePolicy.companion_for_test(
            policy_version="test-policy",
            private_context=True,
            owner_evidence=True,
            tools=True,
            voice_profile=True,
            shadow_low_sensitivity_persona=True,
        )
    )


@pytest.mark.asyncio
async def test_only_final_user_and_actual_heard_assistant_text_become_evidence() -> None:
    runtime = DuplexRuntime.create(session_id="session-001")
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_evidence_publisher(capture)
    runtime.publish_transcript(speaker="user", text="还没说完", final=False)
    runtime.publish_transcript(speaker="assistant", text="未播放完整回答", final=True)
    runtime.publish_transcript(speaker="user", text="我在杭州读过书。", final=True)
    runtime.publish_transcript(
        speaker="assistant",
        text="原来你在杭州读过书。",
        final=True,
        heard=True,
    )
    await asyncio.sleep(0)

    assert [(event["event_type"], event["speaker_class"]) for event in published] == [
        ("speech.utterance_finalized", "uncertain"),
        ("assistant.playout_stopped", "assistant"),
    ]
    assert published[0]["payload"] == {
        "text": "我在杭州读过书。",
        "persona_eligible": False,
        "prompt_kind": "spontaneous",
        **_provenance(),
    }
    assert published[1]["payload"] == {
        "text": "原来你在杭州读过书。",
        "actual_heard": True,
        **_provenance(),
    }
    assert published[0]["session_id"] == "session-001"
    assert published[0]["event_id"] != published[1]["event_id"]
    await runtime.close()


@pytest.mark.asyncio
async def test_actual_heard_assistant_prompt_kind_is_consumed_by_next_user_turn() -> None:
    runtime = DuplexRuntime.create(session_id="session-prompt-kind")
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_evidence_publisher(capture)
    runtime.publish_transcript(
        speaker="assistant",
        text="你是不是更喜欢安静？",
        final=True,
        heard=True,
    )
    runtime.publish_transcript(speaker="user", text="是的。", final=True)
    runtime.publish_transcript(speaker="user", text="另外一件事。", final=True)
    await asyncio.sleep(0)

    user_payloads = [
        event["payload"]
        for event in published
        if event["event_type"] == "speech.utterance_finalized"
    ]
    assert [payload["prompt_kind"] for payload in user_payloads] == [
        "leading",
        "spontaneous",
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_verified_owner_turn_snapshots_pcm_for_the_shared_archive_sink() -> None:
    runtime = DuplexRuntime.create(session_id="session-owner-audio")
    _enable_owner_projection(runtime)
    ordinary: list[dict[str, object]] = []
    owner_turns: list[tuple[dict[str, object], bytes, int]] = []

    async def classify(_: bytes, __: int) -> SpeakerDecision:
        return _owner_decision()

    async def capture(event: dict[str, object]) -> None:
        ordinary.append(event)

    async def capture_owner(
        event: dict[str, object],
        pcm: bytes,
        sample_rate: int,
    ) -> None:
        owner_turns.append((event, pcm, sample_rate))

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.set_evidence_publisher(capture)
    runtime.set_owner_turn_publisher(capture_owner)
    pcm = b"\x01\x00" * 1600
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(pcm)
    runtime.on_user_voice_stopped()
    await runtime.await_speaker_classification()
    fence = await runtime.on_turn_committed("这是主人说的话。")
    runtime.publish_transcript(
        speaker="user",
        text="这是主人说的话。",
        final=True,
        fence=fence,
    )
    await asyncio.sleep(0)

    assert [event["event_type"] for event in ordinary] == ["speaker.classified"]
    assert len(owner_turns) == 1
    event, captured_pcm, sample_rate = owner_turns[0]
    assert event["speaker_class"] == "owner"
    assert captured_pcm == pcm
    assert sample_rate == 16_000
    await runtime.close()


@pytest.mark.asyncio
async def test_archived_transcripts_are_redacted_before_fingerprinting_and_delivery() -> None:
    runtime = DuplexRuntime.create(session_id="session-redaction")
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_evidence_publisher(capture)
    runtime.publish_transcript(
        speaker="user",
        text="我的手机号是13800138000，邮箱是owner@example.com。",
        final=True,
    )
    runtime.publish_transcript(
        speaker="assistant",
        text="我记下了13800138000。",
        final=True,
        heard=True,
    )
    await asyncio.sleep(0)

    assert [event["payload"] for event in published] == [
        {
            "text": "我的手机号是[手机号]，邮箱是[邮箱]。",
            "persona_eligible": False,
            "prompt_kind": "spontaneous",
            **_provenance(),
        },
        {"text": "我记下了[手机号]。", "actual_heard": True, **_provenance()},
    ]
    assert "13800138000" not in json.dumps(published, ensure_ascii=False)
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_drains_durable_evidence_instead_of_canceling_it() -> None:
    runtime = DuplexRuntime.create(session_id="session-close-drain")
    started = asyncio.Event()
    release = asyncio.Event()
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        started.set()
        await release.wait()
        published.append(event)

    runtime.set_evidence_publisher(capture)
    runtime.publish_transcript(speaker="user", text="关机前也要保存。", final=True)
    await started.wait()

    close_task = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)

    assert close_task.done() is False
    release.set()
    await close_task
    assert [event["payload"] for event in published] == [
        {
            "text": "关机前也要保存。",
            "persona_eligible": False,
            "prompt_kind": "spontaneous",
            **_provenance(),
        }
    ]


@pytest.mark.asyncio
async def test_runtime_close_timeout_spools_inflight_evidence(tmp_path: Path) -> None:
    delivery_started = asyncio.Event()
    never_respond = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        delivery_started.set()
        await never_respond.wait()
        return httpx.Response(201)  # pragma: no cover - shutdown cancels delivery

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
    runtime = DuplexRuntime.create(session_id="session-close-timeout")
    runtime._evidence_drain_timeout_s = 0.01

    async def publish(event: dict[str, object]) -> None:
        await sink.publish(event)

    runtime.set_evidence_publisher(publish)
    runtime.publish_transcript(speaker="user", text="超时也必须落盘。", final=True)
    await delivery_started.wait()
    await runtime.close()

    encrypted_lines = [line for line in spool_path.read_bytes().splitlines() if line]
    persisted = [
        json.loads(Fernet(spool_key.encode("ascii")).decrypt(line)) for line in encrypted_lines
    ]
    assert [envelope["target"] for envelope in persisted] == ["event"]
    assert [envelope["body"]["payload"] for envelope in persisted] == [
        {
            "text": "超时也必须落盘。",
            "persona_eligible": False,
            "prompt_kind": "spontaneous",
            **_provenance(),
        }
    ]
    await client.aclose()
