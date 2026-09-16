"""Unit tests for resolve_target_images script."""

from __future__ import annotations

import json

import pytest
from scripts import resolve_target_images as rti
from scripts.resolve_target_images import (
    verify_resolved_services,
)


def test_verify_resolved_services_success_with_matching_tag() -> None:
    services = {
        "agent": {"image": "memoria-agent:20260916-livekit-181-v1"},
        "voice-core-media-bridge": {"image": "memoria-agent:20260916-livekit-181-v1"},
    }
    ok, errors = verify_resolved_services(services, expected_tag="20260916-livekit-181-v1")
    assert ok is True
    assert errors == []


def test_verify_resolved_services_fails_when_service_missing() -> None:
    services = {
        "agent": {"image": "memoria-agent:20260916-livekit-181-v1"},
    }
    ok, errors = verify_resolved_services(services, expected_tag="20260916-livekit-181-v1")
    assert ok is False
    assert any("voice-core-media-bridge: NOT RESOLVED" in e for e in errors)


def test_verify_resolved_services_fails_on_tag_mismatch() -> None:
    services = {
        "agent": {"image": "memoria-agent:20260915-old-tag"},
        "voice-core-media-bridge": {"image": "memoria-agent:20260915-old-tag"},
    }
    ok, errors = verify_resolved_services(services, expected_tag="20260916-livekit-181-v1")
    assert ok is False
    assert any("does not match expected tag" in e for e in errors)


def test_verify_resolved_services_fails_on_cross_service_inconsistency() -> None:
    services = {
        "agent": {"image": "memoria-agent:20260916-livekit-181-v1"},
        "voice-core-media-bridge": {"image": "memoria-agent:20260915-old-tag"},
    }
    ok, errors = verify_resolved_services(services)
    assert ok is False
    assert any("inconsistent images" in e for e in errors)


def test_verify_resolved_services_expected_image_exact_match() -> None:
    services = {
        "agent": {"image": "registry.example.com/agent:v1"},
        "voice-core-media-bridge": {"image": "registry.example.com/agent:v1"},
    }
    ok, errors = verify_resolved_services(
        services,
        expected_images={"agent": "registry.example.com/agent:v1", "voice-core-media-bridge": "registry.example.com/agent:v1"},
    )
    assert ok is True
    assert errors == []


def test_main_cli_fails_when_compose_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rti,
        "run_compose_config",
        lambda *args, **kwargs: (1, "", "docker daemon unavailable"),
    )
    code = rti.main(["resolve_target_images.py", "/tmp/nonexistent", "override.yml"])
    assert code == 1


def test_main_cli_succeeds_when_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_stdout = json.dumps({
        "services": {
            "agent": {"image": "memoria-agent:20260916-livekit-181-v1"},
            "voice-core-media-bridge": {"image": "memoria-agent:20260916-livekit-181-v1"},
        }
    })
    monkeypatch.setattr(
        rti,
        "run_compose_config",
        lambda *args, **kwargs: (0, mock_stdout, ""),
    )
    code = rti.main([
        "resolve_target_images.py",
        "/tmp/release",
        "--expected-tag",
        "20260916-livekit-181-v1",
        "override.yml",
    ])
    assert code == 0
