from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError
from services.device_media_gateway.protocol import (
    FrameType,
    decode_audio_frame,
)

ROOT = Path(__file__).parents[1]
CONTRACT = json.loads(
    (ROOT / "packages" / "contracts" / "device-media-v2.json").read_text(
        encoding="utf-8"
    )
)
VALIDATOR = Draft202012Validator(CONTRACT)


def _validate(value: object) -> None:
    VALIDATOR.validate(value)


def _v2_hello() -> dict[str, object]:
    return {
        "type": "device.hello",
        "version": 2,
        "device_id": "dev_board_01",
        "firmware_version": "0.2.0",
        "board_profile": "memoria-atk-dnesp32s3-v1",
        "stream_epoch": 18,
        "audio": {
            "uplink_codec": "opus",
            "uplink_sample_rate": 16_000,
            "downlink_sample_rates": [16_000, 24_000],
            "channels": 1,
            "frame_ms": 20,
        },
        "capabilities": {
            "display": True,
            "microphone": True,
            "speaker": True,
            "simultaneous_capture_playback": True,
            "aec_mode": "fd_low_cost",
            "aec_reference": "software_post_gain_pre_i2s",
            "aec_reference_verified": False,
            "local_vad": True,
            "local_stop_keyword": False,
            "physical_stop_button": True,
            "playback_watermark": "exact",
            "local_duck": False,
            "barge_in_level": 0,
        },
    }


def test_shared_contract_keeps_legacy_v1_fixture_and_direct_v2_shape_valid() -> None:
    Draft202012Validator.check_schema(CONTRACT)
    _validate(
        {
            "type": "device.hello",
            "version": 1,
            "device_id": "dev_board_01",
            "firmware_version": "0.1.0",
            "board_profile": "memoria-atk-dnesp32s3-v1",
            "stream_epoch": 17,
            "audio": {
                "uplink_codec": "opus",
                "uplink_sample_rate": 16_000,
                "downlink_sample_rate": 24_000,
                "channels": 1,
                "frame_ms": 20,
            },
            "capabilities": {
                "display": True,
                "microphone": True,
                "speaker": True,
                "device_aec": False,
                "physical_button": True,
            },
        }
    )
    _validate(_v2_hello())


def test_v2_requires_16khz_downlink_and_explicit_acoustic_facts() -> None:
    hello = _v2_hello()
    audio = dict(hello["audio"])  # type: ignore[arg-type]
    audio["downlink_sample_rates"] = [24_000]
    hello["audio"] = audio
    with pytest.raises(ValidationError):
        _validate(hello)

    hello = _v2_hello()
    capabilities = dict(hello["capabilities"])  # type: ignore[arg-type]
    capabilities.pop("aec_reference_verified")
    hello["capabilities"] = capabilities
    with pytest.raises(ValidationError):
        _validate(hello)


def test_full_duplex_mode_requires_server_acoustic_attestation() -> None:
    accepted: dict[str, object] = {
        "type": "session.accepted",
        "version": 2,
        "session_id": "session_01",
        "stream_epoch": 18,
        "interaction_authority": "python_authoritative",
        "audio_mode": "full_duplex_verified",
        "downlink_sample_rate": 16_000,
        "runtime_profile_version": 27,
        "device_settings": {
            "settings_version": 4,
            "volume_limit": 72,
            "screen_brightness": 80,
            "night_mode": False,
            "do_not_disturb": False,
            "learning_mode": "off",
            "audio_mode": "full_duplex_verified",
            "wake_mode": "button_or_keyword",
            "allowed_barge_in": ["button", "voice"],
        },
        "current_fence": None,
        "current_generation_active": False,
        "acoustic_attestation": None,
    }
    with pytest.raises(ValidationError):
        _validate(accepted)
    accepted["acoustic_attestation"] = {
        "verified": True,
        "board_profile": "memoria-atk-dnesp32s3-v1",
        "profile_version": 3,
    }
    _validate(accepted)


def test_active_reconnect_requires_a_complete_current_fence() -> None:
    accepted = {
        "type": "session.accepted",
        "version": 2,
        "session_id": "session_01",
        "stream_epoch": 19,
        "interaction_authority": "python_authoritative",
        "audio_mode": "interrupt_assist",
        "downlink_sample_rate": 16_000,
        "runtime_profile_version": 28,
        "device_settings": {
            "settings_version": 5,
            "volume_limit": 72,
            "screen_brightness": 80,
            "night_mode": False,
            "do_not_disturb": False,
            "learning_mode": "off",
            "audio_mode": "interrupt_assist",
            "wake_mode": "button_or_keyword",
            "allowed_barge_in": ["button"],
        },
        "current_fence": None,
        "current_generation_active": True,
        "acoustic_attestation": None,
    }
    with pytest.raises(ValidationError):
        _validate(accepted)
    accepted["current_fence"] = {
        "turn_id": 7,
        "generation_id": 9,
        "tool_epoch": 1,
        "session_epoch": 1,
    }
    _validate(accepted)


def test_hard_stop_and_playback_receipts_require_complete_generation_fence() -> None:
    stop: dict[str, object] = {
        "type": "button.stop",
        "version": 2,
        "stream_epoch": 18,
        "control_sequence": 9,
        "device_monotonic_ms": 42_000,
        "expected_fence": {
            "turn_id": 4,
            "generation_id": 7,
            "tool_epoch": 2,
            "session_epoch": 1,
        },
        "local_flush_sample_end": 12_480,
    }
    _validate(stop)
    broken = dict(stop)
    broken["expected_fence"] = {"generation_id": 7}
    with pytest.raises(ValidationError):
        _validate(broken)

    receipt = {
        "type": "playback.ended",
        "version": 2,
        "stream_epoch": 18,
        "control_sequence": 10,
        "device_monotonic_ms": 42_100,
        "fence": {
            "turn_id": 4,
            "generation_id": 7,
            "tool_epoch": 2,
            "session_epoch": 1,
        },
        "received_sequence": 25,
        "rendered_sample_end": 12_480,
        "approximate": False,
    }
    _validate(receipt)


def test_session_close_has_a_strict_reason_shape() -> None:
    close = {
        "type": "session.close",
        "version": 2,
        "stream_epoch": 18,
        "control_sequence": 11,
        "device_monotonic_ms": 42_200,
        "value": {"reason": "device_close"},
    }
    _validate(close)
    close["value"] = {}
    with pytest.raises(ValidationError):
        _validate(close)


def test_generation_complete_is_an_ordered_audio_lane_barrier() -> None:
    assert "generation.completed" in CONTRACT["priority_lanes"]["p2"]
    assert "generation.completed" not in CONTRACT["priority_lanes"]["p1"]
    assert "same generation" in CONTRACT["ordered_barriers"]["generation.completed"]


def test_binary_golden_frames_match_the_existing_network_order_header() -> None:
    expected_types = {
        "uplink-v1": FrameType.UPLINK_AUDIO,
        "downlink-24k-v1": FrameType.DOWNLINK_AUDIO,
        "downlink-16k-negotiated": FrameType.DOWNLINK_AUDIO,
    }
    for fixture in CONTRACT["binary_audio"]["golden_frames"]:
        frame = decode_audio_frame(
            bytes.fromhex(fixture["frame_hex"]),
            expected_type=expected_types[fixture["name"]],
        )
        assert frame.stream_epoch == fixture["stream_epoch"]
        assert frame.sequence == fixture["sequence"]
        assert frame.sample_start == fixture["sample_start"]
        assert frame.frame_samples == fixture["frame_samples"]
        assert frame.generation_id == fixture["generation_id"]
        assert frame.payload.hex() == fixture["payload_hex"]


def test_direct_path_generation_contract_forbids_n_plus_one_mapping() -> None:
    semantics = CONTRACT["binary_audio"]["generation_semantics"]
    assert semantics["none"] == 0
    assert semantics["first"] == 1
    assert semantics["mapping_allowed_on_direct_path"] is False
    assert "playback.flush" in CONTRACT["priority_lanes"]["p0"]


def test_downlink_clock_resets_to_zero_per_generation_start_or_flush() -> None:
    clock = CONTRACT["binary_audio"]["generation_semantics"]["downlink_clock"]
    assert clock["reset_on"] == ["generation.started", "playback.flush"]
    assert clock["reset_to"] == 0
    # Queue drops must be surfaced with the discontinuity flag and the device
    # accepts a flagged gap only when the deltas are consistent multiples of
    # frame_samples; backward/forged/unflagged gaps are rejected.
    assert "discontinuity" in clock["gap_policy"]
    assert "frame_samples" in clock["gap_policy"]
    assert "rejects backward, forged or unflagged gaps" in clock["gap_policy"]


def test_discontinuity_flag_is_downlink_only_bit_zero_at_header_offset_two() -> None:
    flags = CONTRACT["binary_audio"]["flags"]
    assert flags["discontinuity"] == 1
    assert flags["uplink_allowed"] is False
    # The flag occupies header bytes 2-3 (network order), bit 0, exactly like
    # the gateway header layout: !BBHIIQIIH with flags 0x0001.
    flagged = struct.pack(
        "!BBHIIQIIH",
        1,   # version
        2,   # downlink
        1,   # flags: discontinuity
        18,  # stream_epoch
        3,   # sequence
        960,  # sample_start
        320,  # frame_samples
        1,   # generation_id
        2,   # payload_size
    ) + bytes.fromhex("0405")
    assert len(flagged) == CONTRACT["binary_audio"]["header_size"] + 2
    assert flagged[0] == 1
    assert flagged[1] == 2
    assert int.from_bytes(flagged[2:4], "big") == 1
    assert int.from_bytes(flagged[2:4], "big") & ~1 == 0
    # Same frame with any other bit would be a forged marker.
    forged = bytearray(flagged)
    forged[2:4] = (2).to_bytes(2, "big")
    assert int.from_bytes(forged[2:4], "big") != CONTRACT["binary_audio"]["flags"]["discontinuity"]
    fields = {field["name"]: field for field in CONTRACT["binary_audio"]["fields"]}
    assert "discontinuity" in fields["flags"]["description"]


def test_runtime_profile_applied_receipt_is_strict_and_epoch_bound() -> None:
    receipt = {
        "type": "runtime_profile.applied",
        "version": 2,
        "stream_epoch": 18,
        "control_sequence": 5,
        "device_monotonic_ms": 1234,
        "profile_version": 27,
        "settings_version": 4,
    }
    _validate(receipt)
    receipt["stream_epoch"] = 0
    with pytest.raises(ValidationError):
        _validate(receipt)


def test_priority_lanes_are_server_to_device_and_mute_is_uplink_only() -> None:
    lanes = CONTRACT["priority_lanes"]
    # device.mute_changed is a device-to-server event; it must not sit in the
    # server-to-device write lanes, least of all the P0 preempt lane.
    for priority in ("p0", "p1", "p2", "p3"):
        assert "device.mute_changed" not in lanes[priority]
    assert "playback.flush" in lanes["p0"]
    assert "generation.cancelled" in lanes["p0"]
    assert "session.close" in lanes["p0"]

    # The uplink expression carries the mute event at top urgency and must
    # not contain server-only controls.
    uplink = CONTRACT["uplink_priority_lanes"]
    assert "device.mute_changed" in uplink["p0"]
    for server_only in (
        "session.accepted",
        "generation.started",
        "generation.pause",
        "generation.resume",
        "generation.cancelled",
        "generation.completed",
        "playback.flush",
        "playback.duck",
        "runtime_profile.invalidated",
        "screen.subtitle",
        "screen.expression",
        "assistant.audio.frame",
    ):
        for priority in ("p0", "p1", "p2", "p3"):
            assert server_only not in uplink[priority], server_only
