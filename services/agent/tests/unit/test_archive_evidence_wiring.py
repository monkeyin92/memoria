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
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.common.companions import designed_voice_speaker_sha256
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
        "interaction_mode": "companion",
        "mode_policy_version": "test-policy",
        "simulated_output": False,
        "history_eligible": owner_projection_eligible,
        "owner_projection_eligible": owner_projection_eligible,
    }


def _enable_owner_projection(runtime: DuplexRuntime) -> None:
    bind_owner_policy(
        runtime,
        private_context=True,
        owner_evidence=True,
        tools=True,
        voice_profile=True,
        shadow_low_sensitivity_persona=True,
        include_raw_audio=True,
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
    bind_owner_policy(runtime)
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_evidence_publisher(capture)
    runtime.publish_transcript(speaker="user", text="还没说完", final=False)
    runtime.publish_transcript(speaker="assistant", text="未播放完整回答", final=True)
    runtime.publish_transcript(
        speaker="user", text="我在杭州读过书。", final=True, fence=runtime.fence
    )
    runtime.publish_transcript(
        speaker="assistant",
        text="原来你在杭州读过书。",
        final=True,
        heard=True,
        fence=runtime.fence,
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
    bind_owner_policy(runtime)
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_evidence_publisher(capture)
    runtime.publish_transcript(
        speaker="assistant",
        text="你是不是更喜欢安静？",
        final=True,
        heard=True,
        fence=runtime.fence,
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
    bind_owner_policy(runtime)
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
        "evolution_artifacts": [],
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
        fence=runtime.fence,
    )
    await asyncio.sleep(0)

    assert published[0]["payload"]["response_provenance"] == provenance
    assert published[0]["tool_epoch"] == runtime.fence.tool_epoch
    await runtime.close()


def test_generation_voice_snapshot_is_hashed_and_rejects_stale_fences() -> None:
    runtime = DuplexRuntime.create(session_id="session-voice-snapshot")
    _enable_owner_projection(runtime)
    fence = runtime.fence
    speaker_sha256 = designed_voice_speaker_sha256("warm_companion")
    assert speaker_sha256 is not None

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
    runtime = DuplexRuntime.create(
        session_id="session-self-preview-voice", device_id="dev_01J_test"
    )
    from dataclasses import replace

    from services.agent.tests.unit.runtime_profile_test_helpers import personal_voice_profile

    runtime.set_mode_policy(
        replace(
            _self_preview_voice_policy(),
            runtime_profile=personal_voice_profile(
                "session-self-preview-voice", mode="self_preview"
            ),
        )
    )
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
        speaker_sha256=designed_voice_speaker_sha256("bright_peer") or "",
        voice_kind="designed",
    )


def test_legacy_generation_voice_accepts_only_authorized_personal_or_frozen_fallback() -> None:
    runtime = DuplexRuntime.create(session_id="session-legacy-voice", device_id="dev_01J_test")
    from dataclasses import replace

    from services.agent.tests.unit.runtime_profile_test_helpers import personal_voice_profile

    runtime.set_mode_policy(
        replace(
            _legacy_voice_policy(voice_allowed=True),
            runtime_profile=personal_voice_profile(
                "session-legacy-voice", mode="legacy_access"
            ),
        )
    )
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
        speaker_sha256=designed_voice_speaker_sha256("bright_peer") or "",
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


def test_unknown_safe_generation_voice_accepts_only_anonymous_public_baseline() -> None:
    runtime = DuplexRuntime.create(session_id="session-unknown-safe-voice")
    runtime.set_mode_policy(ModePolicy.degraded_unknown_safe())
    fence = runtime.fence
    approved_hash = designed_voice_speaker_sha256("warm_companion")
    assert approved_hash is not None

    assert runtime.bind_generation_voice(
        fence,
        profile_id=None,
        resource_id="seed-tts-2.0",
        speaker_sha256=approved_hash,
        voice_kind="designed",
    )
    snapshot = runtime.generation_voice_for(fence)
    assert snapshot is not None
    assert snapshot.profile_id is None
    assert snapshot.voice_kind == "designed"

    assert not runtime.bind_generation_voice(
        fence,
        profile_id="warm_companion",
        resource_id="seed-tts-2.0",
        speaker_sha256=approved_hash,
        voice_kind="designed",
    )
    assert not runtime.bind_generation_voice(
        fence,
        profile_id=None,
        resource_id="seed-tts-2.0",
        speaker_sha256="f" * 64,
        voice_kind="designed",
    )
    assert not runtime.bind_generation_voice(
        fence,
        profile_id="personal-voice-1",
        resource_id="seed-icl-2.0",
        speaker_sha256=approved_hash,
        voice_kind="personal",
    )

    overprivileged = ModePolicy(
        mode="unknown_safe",
        policy_version="unsafe-test-policy",
        companion_style_id=None,
        style_version=None,
        references=(),
        capabilities=(("conversation", True), ("history", True)),
        companion_style=None,
    )
    privileged_runtime = DuplexRuntime.create(session_id="unknown-safe-overprivileged")
    privileged_runtime.set_mode_policy(overprivileged)
    assert not privileged_runtime.bind_generation_voice(
        privileged_runtime.fence,
        profile_id=None,
        resource_id="seed-tts-2.0",
        speaker_sha256=approved_hash,
        voice_kind="designed",
    )

    runtime.set_mode_policy(ModePolicy.unavailable("authority_missing"))
    assert not runtime.bind_generation_voice(
        fence,
        profile_id=None,
        resource_id="seed-tts-2.0",
        speaker_sha256=approved_hash,
        voice_kind="designed",
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
    bind_owner_policy(runtime)
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_evidence_publisher(capture)
    runtime.publish_transcript(
        speaker="user",
        text="我的手机号是13800138000，邮箱是owner@example.com。",
        final=True,
        fence=runtime.fence,
    )
    runtime.publish_transcript(
        speaker="assistant",
        text="我记下了13800138000。",
        final=True,
        heard=True,
        fence=runtime.fence,
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
    bind_owner_policy(runtime)
    started = asyncio.Event()
    release = asyncio.Event()
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        started.set()
        await release.wait()
        published.append(event)

    runtime.set_evidence_publisher(capture)
    runtime.publish_transcript(
        speaker="user", text="关机前也要保存。", final=True, fence=runtime.fence
    )
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
    bind_owner_policy(runtime)

    async def publish(event: dict[str, object]) -> None:
        await sink.publish(event)

    runtime.set_evidence_publisher(publish)
    runtime.publish_transcript(
        speaker="user", text="超时也必须落盘。", final=True, fence=runtime.fence
    )
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


@pytest.mark.asyncio
async def test_transcript_without_caller_fence_degrades_to_ui_only() -> None:
    """Audit: archive evidence requires the caller's original fence; a
    fence-less transcript is explicitly degraded to UI-only (never archived)."""

    runtime = DuplexRuntime.create(session_id="session-no-fence")
    bind_owner_policy(runtime)
    ui: list[dict[str, object]] = []
    archived: list[dict[str, object]] = []

    async def capture_ui(event: dict[str, object]) -> None:
        ui.append(event)

    async def capture_archive(event: dict[str, object]) -> None:
        archived.append(event)

    runtime.set_event_publisher(capture_ui)
    runtime.set_evidence_publisher(capture_archive)
    assert (
        runtime.publish_transcript(speaker="user", text="没有原始fence。", final=True)
        is True
    )
    await asyncio.sleep(0)

    assert archived == []
    deltas = [event for event in ui if event.get("type") == "transcript_delta"]
    assert len(deltas) == 1
    # The delta carries an epoch-0 lifecycle envelope, never the current fence.
    assert deltas[0]["session_epoch"] == 0
    assert deltas[0]["active_subject_id"] is None
    assert deltas[0]["runtime_profile_id"] is None
    await runtime.close()


@pytest.mark.asyncio
async def test_stale_epoch_transcript_never_reaches_archive() -> None:
    """A transcript frozen under an old session epoch is not archived: the
    persistence decision fails closed for a pre-switch fence."""

    runtime = DuplexRuntime.create(session_id="session-stale-epoch")
    bind_owner_policy(runtime)
    archived: list[dict[str, object]] = []

    async def capture_archive(event: dict[str, object]) -> None:
        archived.append(event)

    runtime.set_evidence_publisher(capture_archive)
    from dataclasses import replace

    stale_fence = replace(runtime.fence, session_epoch=0)
    assert runtime.fence.session_epoch == 1
    runtime.publish_transcript(
        speaker="user",
        text="旧主体的话轮。",
        final=True,
        fence=stale_fence,
    )
    await asyncio.sleep(0)

    assert archived == []
    await runtime.close()


@pytest.mark.asyncio
async def test_archived_evidence_carries_verifiable_memory_write_fence() -> None:
    """Evidence carries device + subject revision + exact capability receipt so
    the archive can re-verify the write against the signed RuntimeProfile."""

    runtime = DuplexRuntime.create(session_id="session-write-fence")
    bind_owner_policy(runtime)
    archived: list[dict[str, object]] = []

    async def capture_archive(event: dict[str, object]) -> None:
        archived.append(event)

    runtime.set_evidence_publisher(capture_archive)
    runtime.publish_transcript(
        speaker="user",
        text="这条要归档。",
        final=True,
        fence=runtime.fence,
    )
    await asyncio.sleep(0)

    assert len(archived) == 1
    event = archived[0]
    assert event["session_id"] == "session-write-fence"
    assert event["session_epoch"] == runtime.fence.session_epoch
    assert event["turn_id"] == runtime.fence.turn_id
    assert event["generation_id"] == runtime.fence.generation_id
    assert event["tool_epoch"] == runtime.fence.tool_epoch
    assert event["device_id"] == "dev_01J_test"
    assert event["subject_revision"] == 1
    assert event["active_subject_id"] == "person_owner"
    assert event["policy_receipt_id"] is not None
    assert event["event_sequence"] >= 1
    await runtime.close()
