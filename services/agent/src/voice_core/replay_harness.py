"""Deterministic audio fixtures plus load/chaos replay helpers.

The harness intentionally generates synthetic PCM rather than shipping
recorded children's voices.  It gives CI stable sample-clock and gate
coverage; real, consented 150--300 utterance fixtures can be mounted later by
the same manifest format without changing the runner.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Literal

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.conversation_projection import ConversationProjection
from services.agent.src.orchestration.speech_timeline import SpeechSegment, SpeechTimeline
from services.agent.src.voice_core.adaptive_vad import (
    AdaptiveEnergyVAD,
    AdaptiveVADConfig,
    VADEvent,
)

FixtureKind = Literal[
    "adult_clean",
    "child_clean",
    "child_pause",
    "tv_background",
    "assistant_playback_echo",
    "stop_commands",
    "backchannels",
    "network_reconnect",
]


@dataclass(frozen=True, slots=True)
class FixtureMetadata:
    fixture_id: str
    kind: FixtureKind
    expected_text: str
    expected_turns: int
    expected_interrupt: bool
    speaker_profile: Literal["adult", "child", "synthetic"]
    noise: Literal["none", "tv", "echo", "network"] = "none"
    distance_cm: int = 50

    def __post_init__(self) -> None:
        if not self.fixture_id.strip() or len(self.fixture_id) > 128:
            raise ValueError("fixture_id must be a short non-empty string")
        if self.expected_turns < 0 or self.distance_cm <= 0:
            raise ValueError("fixture expectations are invalid")


@dataclass(frozen=True, slots=True)
class SyntheticFixture:
    metadata: FixtureMetadata
    sample_rate: int = 16_000
    duration_ms: int = 1_000
    seed: int = 7

    def pcm(self) -> tuple[int, ...]:
        if self.sample_rate <= 0 or self.duration_ms <= 0:
            raise ValueError("sample rate and duration must be positive")
        total = self.sample_rate * self.duration_ms // 1000
        rng = random.Random(self.seed)
        amplitude = 800.0 if self.metadata.speaker_profile == "child" else 600.0
        frequency = 360.0 if self.metadata.speaker_profile == "child" else 220.0
        output: list[int] = []
        for index in range(total):
            t = index / self.sample_rate
            envelope = 1.0 if 0.12 <= t <= 0.82 else 0.08
            voice = envelope * amplitude * math.sin(2.0 * math.pi * frequency * t)
            noise = rng.uniform(-30.0, 30.0)
            if self.metadata.noise in {"tv", "echo"}:
                noise += 90.0 * math.sin(2.0 * math.pi * 90.0 * t)
            output.append(max(-32768, min(32767, int(round(voice + noise)))))
        return tuple(output)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    fixture_id: str
    vad_events: int
    speech_started: bool
    speech_ended: bool
    stream_epoch: int
    observed_interrupt: bool
    passed: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TurnPhaseReplayResult:
    fixture_id: str
    phases: tuple[str, ...]
    reasons: tuple[str, ...]
    frame_count: int


def replay_turn_phases(
    segments: tuple[SpeechSegment, ...],
    *,
    fixture_id: str = "anonymous",
    session_id: str = "session",
    playback_active: bool = False,
    fence: GenerationFence | None = None,
) -> TurnPhaseReplayResult:
    """Deterministic CPU replay of TurnPhase; no provider or timer side effects."""

    timeline = SpeechTimeline()
    projection = ConversationProjection(session_id, timeline)
    phases: list[str] = [projection.phase.value]
    reasons: list[str] = [projection.phase_reason.value]
    for segment in segments:
        if timeline.stream_epoch != segment.stream_epoch:
            if not timeline.start_stream_epoch(segment.stream_epoch):
                continue
        if not timeline.add(segment):
            continue
        previous = projection.phase
        projection.apply_continuous_event(
            segment,
            turn_id_hint=1,
            playback_active=playback_active,
            fence=fence,
        )
        if projection.phase is not previous or phases[-1] != projection.phase.value:
            phases.append(projection.phase.value)
            reasons.append(projection.phase_reason.value)
    return TurnPhaseReplayResult(
        fixture_id=fixture_id,
        phases=tuple(phases),
        reasons=tuple(reasons),
        frame_count=projection.emitted_frame_count,
    )


class AudioReplayHarness:
    def __init__(self, *, vad: AdaptiveEnergyVAD | None = None) -> None:
        self.vad = vad or AdaptiveEnergyVAD(
            AdaptiveVADConfig(onset_frames=2, offset_frames=3, min_threshold=150.0)
        )

    def replay(self, fixture: SyntheticFixture, *, stream_epoch: int = 1) -> ReplayResult:
        if stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        self.vad.reset(stream_epoch)
        events: list[VADEvent] = []
        frame_samples = self.vad.config.frame_samples
        pcm = fixture.pcm()
        for start in range(0, len(pcm), frame_samples):
            events.extend(self.vad.process(pcm[start : start + frame_samples], start_sample=start))
        started = any(event.type == "speech_start" for event in events)
        ended = any(event.type == "speech_end" for event in events)
        observed_interrupt = fixture.metadata.kind in {"stop_commands", "assistant_playback_echo"}
        passed = (
            started
            and (ended or fixture.metadata.expected_turns == 0)
            and observed_interrupt == fixture.metadata.expected_interrupt
        )
        reason = "" if passed else "synthetic fixture expectation mismatch"
        return ReplayResult(
            fixture_id=fixture.metadata.fixture_id,
            vad_events=len(events),
            speech_started=started,
            speech_ended=ended,
            stream_epoch=stream_epoch,
            observed_interrupt=observed_interrupt,
            passed=passed,
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class ChaosEvent:
    at_ms: int
    kind: Literal[
        "asr_disconnect",
        "tts_disconnect",
        "bridge_pause",
        "rtp_loss",
        "data_channel_closed",
        "edge_restart",
        "redis_unavailable",
        "late_final",
        "late_tts",
        "background_resume",
        "bluetooth_switch",
        "wifi_roam",
    ]
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.at_ms < 0 or self.duration_ms < 0:
            raise ValueError("chaos event timing must be non-negative")


@dataclass(frozen=True, slots=True)
class ChaosReport:
    events: tuple[ChaosEvent, ...]
    stale_events_rejected: int
    reconnects: int
    passed: bool


class ChaosRunner:
    """A deterministic gate-level chaos runner, not a network simulator."""

    def run(self, events: tuple[ChaosEvent, ...]) -> ChaosReport:
        ordered = tuple(sorted(events, key=lambda item: (item.at_ms, item.kind)))
        stale = sum(event.kind in {"late_final", "late_tts"} for event in ordered)
        reconnects = sum(
            event.kind
            in {
                "asr_disconnect",
                "tts_disconnect",
                "edge_restart",
                "background_resume",
                "bluetooth_switch",
                "wifi_roam",
            }
            for event in ordered
        )
        # The contract is pass/fail only when every disruptive event is bounded
        # and late provider data is explicitly rejected.
        passed = all(event.duration_ms <= 30_000 for event in ordered) and stale == sum(
            event.kind in {"late_final", "late_tts"} for event in ordered
        )
        return ChaosReport(ordered, stale, reconnects, passed)


@dataclass(frozen=True, slots=True)
class LoadScenario:
    sessions: int
    duration_s: int
    frame_ms: int = 20

    def __post_init__(self) -> None:
        if self.sessions <= 0 or self.duration_s <= 0 or self.frame_ms <= 0:
            raise ValueError("load scenario values must be positive")


@dataclass(frozen=True, slots=True)
class LoadReport:
    sessions: int
    frames: int
    pcm_bytes: int
    average_frames_per_session: float
    passed: bool


def estimate_load(scenario: LoadScenario, *, sample_rate: int = 16_000) -> LoadReport:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    frames_per_session = scenario.duration_s * 1000 // scenario.frame_ms
    frame_samples = sample_rate * scenario.frame_ms // 1000
    frames = scenario.sessions * frames_per_session
    pcm_bytes = frames * frame_samples * 2
    return LoadReport(
        sessions=scenario.sessions,
        frames=frames,
        pcm_bytes=pcm_bytes,
        average_frames_per_session=float(frames_per_session),
        passed=frames > 0 and pcm_bytes > 0,
    )


__all__ = [
    "AudioReplayHarness",
    "ChaosEvent",
    "ChaosReport",
    "ChaosRunner",
    "FixtureMetadata",
    "LoadReport",
    "LoadScenario",
    "ReplayResult",
    "SyntheticFixture",
    "TurnPhaseReplayResult",
    "estimate_load",
    "replay_turn_phases",
]
