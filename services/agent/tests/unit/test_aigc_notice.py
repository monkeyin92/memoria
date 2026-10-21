"""Configurable AIGC notice hooks and the per-round internal log."""

from __future__ import annotations

import asyncio
import json
import math
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.aigc_generation_log import (
    AIGC_ROUND_EVENT,
    AigcRoundLogger,
    AigcTtsDescriptor,
)
from services.agent.src.voice_core import media_session_aigc_notice
from services.agent.src.voice_core.aigc_notice import (
    AIGC_NOTICE_SAMPLE_RATE_HZ,
    DEFAULT_TONE_PATTERN,
    DEFAULT_TONE_VOLUME,
    AigcNoticeConfig,
    opening_marker_ready_at,
    validate_tone_pattern,
)
from services.agent.src.voice_core.media_session_aigc_notice import (
    MediaSessionAigcNoticeMixin,
    close_reason_for,
)
from services.agent.src.voice_core.media_session_standby import (
    MediaSessionStandbyMixin,
)


class _NoticeHost(MediaSessionAigcNoticeMixin):
    def __init__(self, config: AigcNoticeConfig) -> None:
        self.aigc_notice_config = config
        self.played: list[bytes] = []

    async def _play_aigc_opening_marker(self, context: Any, pcm: bytes) -> None:
        del context
        self.played.append(pcm)


class _StandbyHost(MediaSessionStandbyMixin):
    def __init__(self, config: AigcNoticeConfig) -> None:
        self.aigc_notice_config = config


class _RoundHost(MediaSessionAigcNoticeMixin):
    def __init__(self, logger: AigcRoundLogger | None) -> None:
        self.aigc_round_logger = logger


def _context(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "aigc_round": None,
        "aigc_opening_marker_pending": False,
        "aigc_opening_marker_ready_at": None,
        "aigc_opening_marker_started_at": None,
        "aigc_closing_marker_state": "idle",
        "closed": False,
        "standby_requested": False,
        "identity": SimpleNamespace(session_id="session-aigc", client_type="device"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_defaults_are_the_compliance_short_long_short_short_tone() -> None:
    config = AigcNoticeConfig()

    assert config.enabled is True
    assert config.mode == "tone"
    assert config.tone_pattern == DEFAULT_TONE_PATTERN == "SL SS"
    assert config.tone_volume == pytest.approx(DEFAULT_TONE_VOLUME)
    assert config.lead_interval_ms == 400
    assert config.position_enabled("opening") is True
    assert config.position_enabled("closing") is True


def test_tone_pattern_accepts_only_short_and_long_groups() -> None:
    assert validate_tone_pattern(" sl  ss ") == "SL SS"
    for invalid in ("", "SX", "S-L", "S 1"):
        with pytest.raises(ValueError):
            validate_tone_pattern(invalid)


def test_config_rejects_invalid_volume_rate_and_durations() -> None:
    with pytest.raises(ValueError, match="volume"):
        AigcNoticeConfig(tone_volume=1.5)
    with pytest.raises(ValueError, match="24000 Hz"):
        AigcNoticeConfig(speech_sample_rate_hz=16_000)
    with pytest.raises(ValueError, match="non-negative"):
        AigcNoticeConfig(lead_interval_ms=-1)
    with pytest.raises(ValueError, match="tone durations"):
        AigcNoticeConfig(tone_short_ms=0)


def test_tone_pcm_is_generated_in_memory_without_writing_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = AigcNoticeConfig()

    pcm = config.marker_pcm("opening")

    assert pcm is not None
    expected_ms = 120 + 90 + 300 + 260 + 120 + 90 + 120
    assert len(pcm) == AIGC_NOTICE_SAMPLE_RATE_HZ * expected_ms // 1_000 * 2
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    assert max(samples) > 0
    assert min(samples) < 0
    assert max(abs(value) for value in samples) <= math.ceil(32767 * 0.20)
    assert list(tmp_path.iterdir()) == []


def test_disabled_and_position_flags_play_nothing() -> None:
    assert AigcNoticeConfig(enabled=False).marker_pcm("opening") is None
    assert AigcNoticeConfig(mode="none").marker_pcm("closing") is None
    assert AigcNoticeConfig(opening_enabled=False).marker_pcm("opening") is None
    assert AigcNoticeConfig(closing_enabled=False).marker_pcm("closing") is None


def test_speech_mode_without_a_resource_or_with_a_missing_file_plays_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert (
        AigcNoticeConfig(mode="speech", speech_resource="").marker_pcm("opening") is None
    )

    config = AigcNoticeConfig(
        mode="speech",
        speech_resource="missing_intro",
        speech_root=str(tmp_path),
    )
    with caplog.at_level("WARNING"):
        assert config.marker_pcm("opening") is None
    assert any("not found" in record.getMessage() for record in caplog.records)


def test_speech_mode_plays_an_external_24k_wav_at_native_speed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    wav = tmp_path / "approved_intro.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(AIGC_NOTICE_SAMPLE_RATE_HZ)
        handle.writeframes(b"\x01\x00" * 480)
    config = AigcNoticeConfig(
        mode="speech",
        speech_resource="approved_intro",
        speech_root=str(tmp_path),
    )

    assert config.marker_pcm("opening") == b"\x01\x00" * 480

    slow = tmp_path / "slow.wav"
    with wave.open(str(slow), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x01\x00" * 480)
    with caplog.at_level("WARNING"):
        assert (
            AigcNoticeConfig(
                mode="speech",
                speech_resource="slow",
                speech_root=str(tmp_path),
            ).marker_pcm("opening")
            is None
        )
    assert any("unreadable" in record.getMessage() for record in caplog.records)


def test_from_env_defaults_and_boolean_parsing() -> None:
    assert AigcNoticeConfig.from_env({}).mode == "tone"
    assert AigcNoticeConfig.from_env({}).enabled is True
    assert AigcNoticeConfig.from_env({"AIGC_NOTICE_ENABLED": "off"}).enabled is False
    config = AigcNoticeConfig.from_env(
        {
            "AIGC_NOTICE_MODE": "speech",
            "AIGC_NOTICE_SPEECH_RESOURCE": "approved",
            "AIGC_NOTICE_LEAD_INTERVAL_MS": "650",
        }
    )
    assert config.mode == "speech"
    assert config.speech_resource == "approved"
    assert config.lead_interval_ms == 650


def test_opening_ready_at_follows_the_whole_clip_then_the_configured_gap() -> None:
    pcm = b"\x00\x00" * (AIGC_NOTICE_SAMPLE_RATE_HZ * 1100 // 1_000)

    assert opening_marker_ready_at(
        started_at=10.0,
        pcm=pcm,
        sample_rate_hz=AIGC_NOTICE_SAMPLE_RATE_HZ,
        lead_interval_ms=400,
    ) == pytest.approx(11.5)
    with pytest.raises(ValueError):
        opening_marker_ready_at(
            started_at=0.0,
            pcm=pcm,
            sample_rate_hz=0,
            lead_interval_ms=0,
        )


@pytest.mark.asyncio
async def test_opening_marker_plays_configured_pcm_and_clears_pending() -> None:
    config = AigcNoticeConfig()
    host = _NoticeHost(config)
    context = _context(aigc_opening_marker_pending=True)

    await host._speak_aigc_opening_marker(context)  # type: ignore[arg-type]

    assert host.played == [config.tone_pcm()]
    assert context.aigc_opening_marker_pending is False


@pytest.mark.asyncio
async def test_opening_marker_is_silent_without_a_resource_and_clears_pending() -> None:
    host = _NoticeHost(AigcNoticeConfig(mode="speech", speech_resource=""))
    context = _context(aigc_opening_marker_pending=True)

    await host._speak_aigc_opening_marker(context)  # type: ignore[arg-type]

    assert host.played == []
    assert context.aigc_opening_marker_pending is False


@pytest.mark.asyncio
async def test_opening_lead_in_waits_for_the_remaining_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(media_session_aigc_notice.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(media_session_aigc_notice.time, "monotonic", lambda: 10.0)
    host = _NoticeHost(AigcNoticeConfig())
    context = _context(aigc_opening_marker_ready_at=11.5)

    await host._wait_aigc_opening_lead_in(context)  # type: ignore[arg-type]

    assert sleeps == [pytest.approx(1.5)]


def test_closing_marker_pending_only_when_enabled_and_idle() -> None:
    host = _StandbyHost(AigcNoticeConfig())

    assert host._aigc_closing_marker_pending(_context()) is True  # type: ignore[arg-type]
    assert (
        host._aigc_closing_marker_pending(_context(aigc_closing_marker_state="done"))
        is False
    )
    assert (
        _StandbyHost(AigcNoticeConfig(enabled=False))._aigc_closing_marker_pending(
            _context()
        )
        is False
    )


def test_round_log_writes_time_vendor_and_content_id_without_text(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rounds.jsonl"
    moments = iter(
        [
            datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 10, 20, 10, 3, 0, tzinfo=UTC),
        ]
    )
    logger = AigcRoundLogger(
        AigcTtsDescriptor(
            provider="stepfun",
            engine="stepaudio-3-tts",
            voice="yuanqishaonv",
            fallback_provider="volcengine_doubao",
            fallback_engine="seed-tts-2.0",
            fallback_voice="zh_female_kailangjiejie_uranus_bigtts",
        ),
        path=str(path),
        content_id_factory=lambda: "aigc-round-test",
        clock=lambda: next(moments),
    )
    record = logger.start()
    payload = logger.finish(record, close_reason="child_stop")

    assert payload["event"] == AIGC_ROUND_EVENT
    assert payload["content_id"] == "aigc-round-test"
    assert payload["provider"] == "stepfun"
    assert payload["engine"] == "stepaudio-3-tts"
    assert payload["fallback_voice"] == "zh_female_kailangjiejie_uranus_bigtts"
    assert payload["close_reason"] == "child_stop"
    assert not {
        "text",
        "user_text",
        "assistant_text",
        "reply",
        "transcript",
        "utterance",
        "prompt",
        "content",
    } & set(payload)
    written = json.loads(path.read_text(encoding="utf-8").strip())
    assert written == payload


def test_round_logger_is_optional_and_idempotent(tmp_path: Path) -> None:
    assert (
        AigcRoundLogger.from_settings(
            SimpleNamespace(
                aigc_generation_log_enabled=False,
                tts_provider="doubao",
            )
        )
        is None
    )

    logger = AigcRoundLogger(
        AigcTtsDescriptor(provider="volcengine_doubao", engine="seed-tts-2.0"),
        path=str(tmp_path / "rounds.jsonl"),
        content_id_factory=lambda: "one",
    )
    record = logger.start()
    logger.finish(record, close_reason="session_closed")
    with pytest.raises(ValueError, match="already finished"):
        logger.finish(record, close_reason="session_closed")


def test_round_bookkeeping_starts_once_and_finishes_once(tmp_path: Path) -> None:
    logger = AigcRoundLogger(
        AigcTtsDescriptor(provider="stepfun", engine="stepaudio-3-tts"),
        path=str(tmp_path / "rounds.jsonl"),
        content_id_factory=lambda: "round-1",
    )
    host = _RoundHost(logger)
    context = _context()

    first = host._start_aigc_round(context)  # type: ignore[arg-type]
    second = host._start_aigc_round(context)  # type: ignore[arg-type]

    assert first is second
    assert first is not None
    assert first.content_id == "round-1"
    host._finish_aigc_round(context, close_reason="owner_silence_timeout")  # type: ignore[arg-type]
    assert context.aigc_round is None
    host._finish_aigc_round(context, close_reason="owner_silence_timeout")  # type: ignore[arg-type]
    payloads = [
        json.loads(line)
        for line in (tmp_path / "rounds.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(payloads) == 1
    assert payloads[0]["close_reason"] == "owner_silence_timeout"


def test_close_reason_is_bounded_and_named() -> None:
    assert close_reason_for("  child_stop  ") == "child_stop"
    assert close_reason_for("   ") == "conversation_end"
    assert len(close_reason_for("x" * 500)) == 128


@pytest.mark.asyncio
async def test_device_session_opens_and_finishes_one_round_record(tmp_path: Path) -> None:
    from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
    from services.agent.tests.unit.media_session_support import (
        _AckCapturingProvider,
        _CapturingGenerationBridge,
        _device_identity,
    )

    path = tmp_path / "rounds.jsonl"
    logger = AigcRoundLogger(
        AigcTtsDescriptor(provider="stepfun", engine="stepaudio-3-tts"),
        path=str(path),
        content_id_factory=lambda: "round-from-session",
    )
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        aigc_notice_config=AigcNoticeConfig(enabled=False),
        aigc_round_logger=logger,
    )
    registry.install()
    identity = _device_identity("aigc-round-record")
    bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        assert context.aigc_round is not None
        assert context.aigc_round.content_id == "round-from-session"
    finally:
        await registry.finalize_session(identity.session_id)

    payloads = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(payloads) == 1
    assert payloads[0]["content_id"] == "round-from-session"
    assert payloads[0]["provider"] == "stepfun"
    assert payloads[0]["close_reason"]


def test_aigc_marker_is_a_pcm_fast_acknowledgement_not_a_tts_reply() -> None:
    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.delegation_coordinator import (
        DelegationCoordinator,
    )
    from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2

    fence = GenerationFence("session-aigc", 1, 1, 0)
    intent = DelegationCoordinator.aigc_marker(
        fence=fence,
        context_version=0,
        expires_at_ms=2_000,
        pcm_s16le=b"\x01\x00",
        now_ms=1_000,
    )

    assert intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT
    assert intent.pcm_s16le == b"\x01\x00"
    assert intent.tts_source == ""
    assert intent.priority > 0

    with pytest.raises(ValueError, match="16-bit audio"):
        DelegationCoordinator.aigc_marker(
            fence=fence,
            context_version=0,
            expires_at_ms=2_000,
            pcm_s16le=b"\x01",
            now_ms=1_000,
        )


def test_non_device_session_does_not_open_an_aigc_round(tmp_path: Path) -> None:
    logger = AigcRoundLogger(
        AigcTtsDescriptor(provider="stepfun", engine="stepaudio-3-tts"),
        path=str(tmp_path / "rounds.jsonl"),
    )
    host = _RoundHost(logger)
    context = _context(
        identity=SimpleNamespace(session_id="h5-aigc", client_type="h5")
    )

    assert host._start_aigc_round(context) is None  # type: ignore[arg-type]
    assert context.aigc_round is None


@pytest.mark.asyncio
async def test_opening_marker_keeps_device_wake_ack_and_returns_to_listening() -> None:
    from services.agent.src.orchestration.state_machine import InteractionPhase
    from services.agent.src.prompts import is_allowlisted_device_phrase
    from services.agent.src.voice_core.media_protocol import (
        PlaybackEventType,
        PlaybackProgress,
    )
    from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
    from services.agent.tests.unit.media_session_support import (
        _AckCapturingProvider,
        _CapturingGenerationBridge,
        _device_identity,
        _wait_until,
    )

    config = AigcNoticeConfig(
        tone_short_ms=20,
        tone_long_ms=30,
        tone_gap_ms=0,
        tone_group_gap_ms=0,
        lead_interval_ms=150,
    )
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        aigc_notice_config=config,
        aigc_round_logger=None,
        speaking_stall_timeout_s=0.3,
    )
    registry.install()
    identity = _device_identity("aigc-wake-ack")
    session = bridge.bridge.open(identity)
    stalls_before = registry.metrics.get("voice_speaking_stall_total")
    ack_entry: dict[str, object] = {}
    original_ack = registry._speak_device_wake_ack

    async def capture_ack(context: Any) -> None:
        ack_entry["phase"] = context.runtime.interaction_phase
        await original_ack(context)

    registry._speak_device_wake_ack = capture_ack  # type: ignore[method-assign]
    try:
        context = await registry.open_session(identity)

        assert ack_entry["phase"] is InteractionPhase.LISTENING
        await _wait_until(lambda: len(provider.texts) >= 2)
        assert is_allowlisted_device_phrase(provider.texts[-1])
        ack_fence = context.device_wake_ack_fence
        assert ack_fence is not None
        ack_frame = next(
            frame
            for frame in reversed(bridge.frames)
            if frame.generation_id == ack_fence.generation_id
            and frame.turn_id == ack_fence.turn_id
        )
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=ack_fence.generation_id,
                received_sequence=ack_frame.sequence,
                rendered_sample_end=ack_frame.source_start_sample + ack_frame.frame_samples,
                client_monotonic_ms=1,
                turn_id=ack_fence.turn_id,
                tool_epoch=ack_fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
        await _wait_until(
            lambda: context.runtime.interaction_phase is InteractionPhase.LISTENING
        )
        await asyncio.sleep(0.35)
        assert not context.closed
        assert not context.standby_requested
        assert registry.metrics.get("voice_speaking_stall_total") == stalls_before
        assert context.runtime.interaction_phase is InteractionPhase.LISTENING
        assert context.aigc_opening_marker_turn_id is None
    finally:
        await registry.finalize_session(identity.session_id)


def test_device_wake_ack_gate_closes_after_user_turn_advances() -> None:
    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry

    def context_at(turn_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            runtime=SimpleNamespace(
                fence=GenerationFence(
                    session_id="session-gate",
                    turn_id=turn_id,
                    generation_id=turn_id,
                    tool_epoch=0,
                )
            ),
            aigc_opening_marker_turn_id=1,
        )

    assert MediaVoiceCoreRegistry._device_wake_ack_turn_available(context_at(1)) is True
    assert MediaVoiceCoreRegistry._device_wake_ack_turn_available(context_at(2)) is False


@pytest.mark.asyncio
async def test_opening_marker_returns_to_listening_without_a_wake_ack() -> None:
    from services.agent.src.orchestration.state_machine import InteractionPhase
    from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
    from services.agent.tests.unit.media_session_support import (
        _AckCapturingProvider,
        _CapturingGenerationBridge,
        _device_identity,
        _wait_until,
    )

    config = AigcNoticeConfig(
        tone_short_ms=20,
        tone_long_ms=30,
        tone_gap_ms=0,
        tone_group_gap_ms=0,
        lead_interval_ms=150,
    )
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        aigc_notice_config=config,
        aigc_round_logger=None,
        speaking_stall_timeout_s=0.3,
    )
    registry.install()
    identity = _device_identity("aigc-marker-only")
    bridge.bridge.open(identity)
    stalls_before = registry.metrics.get("voice_speaking_stall_total")

    async def no_ack(context: Any) -> None:
        context.device_wake_ack_pending = False

    registry._speak_device_wake_ack = no_ack  # type: ignore[method-assign]
    try:
        context = await registry.open_session(identity)

        # The marker itself opened turn 1 and reached the downlink; no wake
        # acknowledgement followed, so only marker completion can restore the
        # floor.
        assert any(frame.turn_id == 1 for frame in bridge.frames)
        await _wait_until(
            lambda: context.runtime.interaction_phase is InteractionPhase.LISTENING
        )
        assert context.aigc_opening_marker_turn_id == 1
        await asyncio.sleep(0.35)
        assert not context.closed
        assert not context.standby_requested
        assert registry.metrics.get("voice_speaking_stall_total") == stalls_before
        assert context.runtime.interaction_phase is InteractionPhase.LISTENING
    finally:
        await registry.finalize_session(identity.session_id)
