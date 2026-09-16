"""P1-01: assert the installed LiveKit 1.8.x SDK really consumes this repo's config.

``TypedDict`` accepts unknown keys silently, so "construction did not raise" is
not evidence.  These tests resolve the same values through the real SDK object
the session is started with, and fail on any key that would be dropped on the
floor.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from services.agent.src import session_entrypoint as entrypoint_mod

pytest.importorskip("livekit.agents")

from livekit.agents import AgentSession  # noqa: E402
from livekit.agents.voice.turn import (  # noqa: E402
    EndpointingOptions,
    InterruptionOptions,
    PreemptiveGenerationOptions,
    TurnHandlingOptions,
)

# ``stream_speak_while_think`` is a Memoria product flag, not a LiveKit key; it
# is read by the Agent from the returned dict rather than handed to the SDK.
_MEMORIA_ONLY_TURN_KEYS = frozenset({"stream_speak_while_think"})


def _typed_dict_fields(typed_dict: Any) -> frozenset[str]:
    return frozenset(typed_dict.__annotations__)


def _fake_components() -> dict[str, Any]:
    return {"vad": None, "stt": None, "llm": None, "tts": None}


@pytest.mark.parametrize("device_vad", [False, True])
@pytest.mark.parametrize("profile", ["cn_self_hosted", "livekit_cloud"])
def test_every_turn_handling_key_is_a_real_livekit_field(profile: str, device_vad: bool) -> None:
    config = entrypoint_mod.build_turn_handling_config(profile, device_vad=device_vad)

    unknown = set(config) - _typed_dict_fields(TurnHandlingOptions) - _MEMORIA_ONLY_TURN_KEYS
    assert unknown == set(), f"turn_handling keys would be silently dropped: {sorted(unknown)}"

    for key, typed_dict in (
        ("endpointing", EndpointingOptions),
        ("interruption", InterruptionOptions),
        ("preemptive_generation", PreemptiveGenerationOptions),
    ):
        dropped = set(config[key]) - _typed_dict_fields(typed_dict)
        assert dropped == set(), f"{key} keys would be silently dropped: {sorted(dropped)}"


@pytest.mark.parametrize("device_vad", [False, True])
def test_agent_session_resolves_our_turn_options_instead_of_defaults(device_vad: bool) -> None:
    """Start a real ``AgentSession`` and read back the resolved turn handling."""

    config = entrypoint_mod.build_turn_handling_config("cn_self_hosted", device_vad=device_vad)
    kwargs = entrypoint_mod.build_session_kwargs(
        **_fake_components(),
        profile="cn_self_hosted",
        offline=True,
        device_vad=device_vad,
    )
    assert "turn_handling" in kwargs, "candidate SDK must accept our TurnHandlingOptions"

    session = AgentSession(**kwargs)
    resolved = session._opts.turn_handling

    if isinstance(config["turn_detection"], str):
        assert resolved["turn_detection"] == config["turn_detection"]
    else:
        # ``{"version": ...}`` is materialized into a TurnDetector instance.
        assert resolved["turn_detection"].model == (
            f"turn-detector-{config['turn_detection']['version']}"
        )
    assert dict(resolved["endpointing"]) == config["endpointing"]
    assert resolved["interruption"]["enabled"] == config["interruption"]["enabled"]
    assert resolved["interruption"]["mode"] == config["interruption"]["mode"]
    assert resolved["interruption"]["min_duration"] == config["interruption"]["min_duration"]
    assert (
        resolved["interruption"]["false_interruption_timeout"]
        == config["interruption"]["false_interruption_timeout"]
    )
    assert (
        resolved["preemptive_generation"]["enabled"]
        == config["preemptive_generation"]["enabled"]
    )
    assert (
        resolved["preemptive_generation"]["preemptive_tts"]
        == config["preemptive_generation"]["preemptive_tts"]
    )


def test_candidate_sdk_keeps_an_off_by_default_user_turn_limit() -> None:
    """1.8.x adds ``user_turn_limit``; the upgrade must not start enforcing it."""

    session = AgentSession(
        **entrypoint_mod.build_session_kwargs(
            **_fake_components(),
            profile="cn_self_hosted",
            offline=True,
            device_vad=True,
        )
    )
    limits = session._opts.turn_handling["user_turn_limit"]

    assert limits["max_words"] is None
    assert limits["max_duration"] is None


def test_half_duplex_session_disables_interruption_end_to_end() -> None:
    """The miniprogram half-duplex path must disable barge-in in the real SDK."""

    session = AgentSession(
        **entrypoint_mod.build_session_kwargs(
            **_fake_components(),
            profile="cn_self_hosted",
            offline=True,
            interruptions_enabled=False,
        )
    )

    assert session._opts.turn_handling["interruption"]["enabled"] is False
    assert session._opts.turn_handling["preemptive_generation"]["enabled"] is False


def test_miniprogram_aec_policy_survives_the_candidate_sdk() -> None:
    """``aec_warmup_duration=None`` must still mean "no warmup" on 1.8.x."""

    plain: dict[str, Any] = {}
    aec: dict[str, Any] = {}
    assert not entrypoint_mod.apply_miniprogram_session_audio_policy(plain, "web")
    assert entrypoint_mod.apply_miniprogram_session_audio_policy(
        aec,
        entrypoint_mod.MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA,
    )

    assert AgentSession(**plain)._aec_warmup_remaining == 3.0
    assert AgentSession(**aec)._aec_warmup_remaining == 0.0


def test_room_io_options_still_match_the_candidate_signatures() -> None:
    """RoomIO is semi-internal; assert every field we pass still exists."""

    from livekit.agents import room_io

    audio_input = room_io.AudioInputOptions(
        sample_rate=24000,
        num_channels=1,
        frame_size_ms=50,
        auto_gain_control=True,
        pre_connect_audio=True,
    )
    audio_output = entrypoint_mod.build_cascade_audio_output_options()
    text_output = room_io.TextOutputOptions(sync_transcription=True, json_format=True)
    room_options = room_io.RoomOptions(
        text_input=room_io.TextInputOptions(text_input_cb=lambda *_: None),
        audio_input=audio_input,
        audio_output=audio_output,
        text_output=text_output,
    )

    # #7064 relaxed the AGC default only when noise cancellation is configured.
    # This repo never configures NC, so the explicit AGC request must survive.
    assert audio_input.auto_gain_control is True
    assert audio_input.noise_cancellation is None
    assert audio_output.sample_rate == 24000
    assert audio_output.num_channels == 1
    assert room_options.close_on_disconnect is True


def test_stt_and_tts_adapters_still_expose_the_contract_we_implement() -> None:
    """Our custom providers subclass these; a rename would break at runtime."""

    from livekit.agents import stt, tts

    for name in ("SpeechEvent", "SpeechData", "STTCapabilities", "RecognizeStream"):
        assert hasattr(stt, name), f"livekit.agents.stt.{name} disappeared"
    for name in (
        "SynthesizedAudio",
        "TTSCapabilities",
        "ChunkedStream",
        "SynthesizeStream",
    ):
        assert hasattr(tts, name), f"livekit.agents.tts.{name} disappeared"
    assert hasattr(tts, "AudioEmitter")


def test_optional_designed_voice_contract_is_unchanged() -> None:
    """Guard the helpers our providers import from livekit.agents.types."""

    from livekit.agents import types

    assert hasattr(types, "TimedString")
    assert hasattr(types, "DEFAULT_API_CONNECT_OPTIONS")
    assert dataclasses.is_dataclass(types.DEFAULT_API_CONNECT_OPTIONS)
