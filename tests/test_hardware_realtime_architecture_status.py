from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
STATUS = (ROOT / "architecture-status.yaml").read_text(encoding="utf-8")
ADR = (
    ROOT / "docs" / "adr" / "0035-esp32-first-class-realtime-terminal.md"
).read_text(encoding="utf-8")


def test_hardware_runtime_status_separates_delivery_evidence_layers() -> None:
    assert "schema_version: 2" in STATUS
    for field in ("code:", "wired:", "enabled:", "verified:"):
        assert field in STATUS
    assert "hardware_media_interaction_authority: python_authoritative" in STATUS
    assert "hardware_media_target_runtime: go_media_edge_direct_voice_core" in STATUS
    assert "hardware_media_rollback_runtime: python_device_gateway_livekit_compat" in STATUS


def test_unverified_hardware_cannot_be_advertised_as_full_duplex() -> None:
    assert "advertised_duplex_level: none" in STATUS
    assert "hardware_aec: pending" in STATUS
    assert "hardware_double_talk_matrix: pending_real_hardware" in STATUS
    assert "full_duplex_verified" in ADR
    assert "产品不得宣传全双工" in ADR


def test_miniprogram_is_frozen_as_a_non_media_control_plane() -> None:
    assert "miniprogram_role: control_plane_only" in STATUS
    for gate in (
        "realtime_microphone_allowed: false",
        "realtime_tts_playback_allowed: false",
        "realtime_media_wss_allowed: false",
        "livekit_room_allowed: false",
    ):
        assert gate in STATUS
    assert "不申请 `scope.record`" in ADR
