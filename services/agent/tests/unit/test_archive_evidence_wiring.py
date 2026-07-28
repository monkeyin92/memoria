from __future__ import annotations

import asyncio
import hashlib
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


def _self_preview_voice_policy() -> ModePolicy:
    return ModePolicy(
        mode="self_preview",
        policy_version="s8-v1",
        companion_style_id=None,
        style_version=None,
        references=tuple(
            sorted(
                {
                    "voice_profile_id": "personal-voice-1",
                    "voice_profile_version": "1",
                    "voice_provider": "volcengine_doubao",
                    "voice_model": "seed-icl-2.0",
                    "voice_resource_id": "seed-icl-2.0",
                    "voice_provider_expires_at": "2026-08-01T00:00:00+00:00",
                    "voice_speaker_sha256": "a" * 64,
                    "fallback_voice_profile_id": "bright_peer",
                    "fallback_voice_provider": "volcengine_doubao",
                    "fallback_voice_model": "seed-tts-2.0",
                    "fallback_voice_resource_id": "seed-tts-2.0",
                }.items()
            )
        ),
        capabilities=(),
        companion_style=None,
    )


def _legacy_voice_policy(*, voice_allowed: bool) -> ModePolicy:
    return ModePolicy(
        mode="legacy",
        policy_version="s9-v1",
        companion_style_id=None,
        style_version=None,
        references=tuple(
            sorted(
                {
                    "voice_profile_id": "personal-voice-1" if voice_allowed else None,
                    "voice_profile_version": "1" if voice_allowed else None,
                    "voice_provider": "volcengine_doubao" if voice_allowed else None,
                    "voice_model": "seed-icl-2.0" if voice_allowed else None,
                    "voice_resource_id": "seed-icl-2.0" if voice_allowed else None,
                    "voice_provider_expires_at": (
                        "2026-08-01T00:00:00+00:00" if voice_allowed else None
                    ),
                    "voice_speaker_sha256": "a" * 64 if voice_allowed else None,
                    "fallback_voice_profile_id": "bright_peer",
                    "fallback_voice_provider": "volcengine_doubao",
                    "fallback_voice_model": "seed-tts-2.0",
                    "fallback_voice_resource_id": "seed-tts-2.0",
                    "legacy_voice_allowed": voice_allowed,
                }.items()
            )
        ),
        capabilities=(),
        companion_style=None,
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
    first_user_fence = runtime.fence.bump_turn()
    second_user_fence = first_user_fence.bump_turn()
    runtime.publish_transcript(
        speaker="user",
        text="是的。",
        final=True,
        fence=first_user_fence,
    )
    runtime.publish_transcript(
        speaker="user",
        text="另外一件事。",
        final=True,
        fence=second_user_fence,
    )
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
async def test_actual_heard_assistant_binds_bounded_response_provenance_to_exact_fence() -> None:
    runtime = DuplexRuntime.create(session_id="session-response-plan")
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        published.append(event)

    provenance = {
        "fence": {
            "session_id": runtime.fence.session_id,
            "turn_id": runtime.fence.turn_id,
            "generation_id": runtime.fence.generation_id,
            "tool_epoch": runtime.fence.tool_epoch,
        },
        "planner_policy_version": "digital-self-response-planner-v2",
        "interaction_mode": "companion",
        "mode_policy_version": "test-policy",
        "digital_self_version_id": None,
        "manifest_sha256": None,
        "relationship_profile_id": None,
        "relationship_profile_version": None,
        "speaker_class": "owner",
        "speaker_reason_code": "owner_match",
        "speaker_profile_id": "owner-profile",
        "speaker_model_version": "campplus-test",
        "speaker_template_version": 1,
        "source_refs": [
            {
                "kind": "memory_claim",
                "item_id": "claim-1",
                "source_event_ids": ["event-1"],
            }
        ],
        "epistemic_status": "fact",
        "epistemic_reason_codes": ["exact_owner_source"],
        "disclosures": [],
    }
    runtime.set_evidence_publisher(capture)

    assert runtime.bind_response_provenance(runtime.fence, provenance) is True
    assert (
        runtime.bind_response_provenance(
            runtime.fence.bump_generation(),
            provenance,
        )
        is False
    )
    assert (
        runtime.bind_response_provenance(
            runtime.fence,
            {**provenance, "instructions": "不得写入 archive"},
        )
        is False
    )

    runtime.publish_transcript(
        speaker="assistant",
        text="你曾经说过会先确认事实。",
        final=True,
        heard=True,
    )
    await asyncio.sleep(0)

    assert published[0]["payload"]["response_provenance"] == provenance
    assert published[0]["tool_epoch"] == runtime.fence.tool_epoch
    await runtime.close()


def test_generation_voice_snapshot_is_hashed_and_rejects_stale_fences() -> None:
    runtime = DuplexRuntime.create(session_id="session-voice-snapshot")
    _enable_owner_projection(runtime)
    fence = runtime.fence
    speaker_sha256 = hashlib.sha256(b"baseline-speaker").hexdigest()

    assert runtime.bind_generation_voice(
        fence,
        profile_id="warm_companion",
        resource_id="seed-tts-2.0",
        speaker_sha256=speaker_sha256,
        voice_kind="designed",
    )
    snapshot = runtime.generation_voice_for(fence)
    assert snapshot is not None
    assert snapshot.profile_id == "warm_companion"
    assert snapshot.resource_id == "seed-tts-2.0"
    assert snapshot.voice_kind == "designed"
    assert snapshot.speaker_sha256 == speaker_sha256
    assert not hasattr(snapshot, "speaker")

    stale = fence.bump_generation()
    assert not runtime.bind_generation_voice(
        stale,
        profile_id="stale",
        resource_id="seed-tts-2.0",
        speaker_sha256="b" * 64,
        voice_kind="designed",
    )
    assert runtime.generation_voice_for(stale) is None


def test_self_preview_generation_voice_requires_frozen_personal_digest_and_fallback() -> None:
    runtime = DuplexRuntime.create(session_id="session-self-preview-voice")
    runtime.set_mode_policy(_self_preview_voice_policy())
    fence = runtime.fence

    assert not runtime.bind_generation_voice(
        fence,
        profile_id="personal-voice-1",
        resource_id="seed-icl-2.0",
        speaker_sha256="b" * 64,
        voice_kind="personal",
    )
    assert runtime.bind_generation_voice(
        fence,
        profile_id="personal-voice-1",
        resource_id="seed-icl-2.0",
        speaker_sha256="a" * 64,
        voice_kind="personal",
    )
    assert not runtime.bind_generation_voice(
        fence,
        profile_id="warm_companion",
        resource_id="seed-tts-2.0",
        speaker_sha256="c" * 64,
        voice_kind="designed",
    )
    assert not runtime.bind_generation_voice(
        fence,
        profile_id=None,
        resource_id="seed-tts-2.0",
        speaker_sha256="c" * 64,
        voice_kind="designed",
    )
    assert runtime.bind_generation_voice(
        fence,
        profile_id="bright_peer",
        resource_id="seed-tts-2.0",
        speaker_sha256="c" * 64,
        voice_kind="designed",
    )


def test_legacy_generation_voice_accepts_only_authorized_personal_or_frozen_fallback() -> None:
    runtime = DuplexRuntime.create(session_id="session-legacy-voice")
    runtime.set_mode_policy(_legacy_voice_policy(voice_allowed=True))
    fence = runtime.fence

    assert runtime.bind_generation_voice(
        fence,
        profile_id="personal-voice-1",
        resource_id="seed-icl-2.0",
        speaker_sha256="a" * 64,
        voice_kind="personal",
    )
    assert runtime.bind_generation_voice(
        fence,
        profile_id="bright_peer",
        resource_id="seed-tts-2.0",
        speaker_sha256="b" * 64,
        voice_kind="designed",
    )
    assert not runtime.bind_generation_voice(
        fence,
        profile_id="other-designed",
        resource_id="seed-tts-2.0",
        speaker_sha256="b" * 64,
        voice_kind="designed",
    )

    runtime.set_mode_policy(_legacy_voice_policy(voice_allowed=False))
    assert not runtime.bind_generation_voice(
        fence,
        profile_id="personal-voice-1",
        resource_id="seed-icl-2.0",
        speaker_sha256="a" * 64,
        voice_kind="personal",
    )


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
