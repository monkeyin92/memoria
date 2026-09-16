"""Unit tests for resolve_target_images script."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from scripts import resolve_target_images as rti
from scripts.resolve_target_images import (
    verify_resolved_services,
)

_CANDIDATE = "20260916-livekit-181-v1"
_STACK = "20260901-0945-wake-word-whitelist"
_CANDIDATE_IMAGE = f"memoria-agent:{_CANDIDATE}"
_STACK_IMAGE = f"memoria-agent:{_STACK}"


def _services(agent: str, bridge: str) -> dict[str, dict[str, str]]:
    return {
        "agent": {"image": agent},
        "voice-core-media-bridge": {"image": bridge},
    }


def _release_dir(tmp_path: Path, *, commit_placeholder: bool = False) -> Path:
    release_dir = tmp_path / "release"
    release_dir.mkdir(exist_ok=True)
    (release_dir / rti.BASE_COMPOSE_FILE).write_text(
        "services: {}\n", encoding="utf-8"
    )
    if commit_placeholder:
        (release_dir / rti.BASE_COMPOSE_FILE).write_text(
            "services: {}\n", encoding="utf-8"
        )
    return release_dir


def _fake_compose(monkeypatch: pytest.MonkeyPatch, services: dict[str, object]) -> list[dict]:
    """Replace docker with a shim that records the command and renders services."""

    calls: list[dict] = []

    def fake_run(
        release_dir: str,
        overrides: list[str],
        *,
        stack_tag: str,
        release_commit: str | None = None,
        docker_cmd: list[str] | None = None,
    ) -> tuple[int, str, str]:
        calls.append(
            {
                "release_dir": release_dir,
                "overrides": list(overrides),
                "stack_tag": stack_tag,
                "release_commit": release_commit,
                "docker_cmd": list(docker_cmd or ()),
            }
        )
        return 0, json.dumps({"services": services}), ""

    monkeypatch.setattr(rti, "run_compose_config", fake_run)
    return calls


# ---------------------------------------------------------------------------
# verify_resolved_services
# ---------------------------------------------------------------------------


def test_verify_resolved_services_accepts_candidate_tag_distinct_from_stack_tag() -> None:
    ok, errors = verify_resolved_services(
        _services(_CANDIDATE_IMAGE, _CANDIDATE_IMAGE),
        candidate_tag=_CANDIDATE,
        stack_tag=_STACK,
    )
    assert ok is True
    assert errors == []


def test_verify_resolved_services_rejects_a_missing_candidate_identity() -> None:
    """Two services on the same live image must never pass on consistency alone."""

    ok, errors = verify_resolved_services(
        _services(_STACK_IMAGE, _STACK_IMAGE),
        stack_tag=_STACK,
    )
    assert ok is False
    assert any("candidate identity is required" in error for error in errors)


def test_verify_resolved_services_names_the_live_stack_image_as_the_cause() -> None:
    ok, errors = verify_resolved_services(
        _services(_STACK_IMAGE, _STACK_IMAGE),
        candidate_tag=_CANDIDATE,
        stack_tag=_STACK,
    )
    assert ok is False
    assert all("still resolves to the effective stack image" in error for error in errors)
    assert all(_CANDIDATE in error for error in errors)


def test_verify_resolved_services_fails_when_service_missing() -> None:
    services = {"agent": {"image": _CANDIDATE_IMAGE}}
    ok, errors = verify_resolved_services(
        services,
        candidate_tag=_CANDIDATE,
        stack_tag=_STACK,
    )
    assert ok is False
    assert any("voice-core-media-bridge: NOT RESOLVED" in error for error in errors)


def test_verify_resolved_services_fails_on_tag_mismatch() -> None:
    ok, errors = verify_resolved_services(
        _services("memoria-agent:20260915-old-tag", "memoria-agent:20260915-old-tag"),
        candidate_tag=_CANDIDATE,
        stack_tag=_STACK,
    )
    assert ok is False
    assert any("does not match expected tag" in error for error in errors)


def test_verify_resolved_services_rejects_divergent_target_services() -> None:
    ok, errors = verify_resolved_services(
        _services(_CANDIDATE_IMAGE, "memoria-agent:20260915-old-tag"),
        candidate_tag=_CANDIDATE,
        stack_tag=_STACK,
    )
    assert ok is False
    assert any("inconsistent images" in error for error in errors)


def test_verify_resolved_services_rejects_expected_images_from_two_artifacts() -> None:
    ok, errors = verify_resolved_services(
        _services(_CANDIDATE_IMAGE, _CANDIDATE_IMAGE),
        expected_images={
            "agent": _CANDIDATE_IMAGE,
            "voice-core-media-bridge": "memoria-agent:other",
        },
    )
    assert ok is False
    assert any("one candidate artifact" in error for error in errors)


def test_verify_resolved_services_expected_image_exact_match() -> None:
    services = _services("registry.example.com/agent:v1", "registry.example.com/agent:v1")
    ok, errors = verify_resolved_services(
        services,
        expected_images={
            "agent": "registry.example.com/agent:v1",
            "voice-core-media-bridge": "registry.example.com/agent:v1",
        },
    )
    assert ok is True
    assert errors == []


# ---------------------------------------------------------------------------
# main(): invocation contract
# ---------------------------------------------------------------------------


def test_main_requires_a_candidate_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _fake_compose(monkeypatch, _services(_STACK_IMAGE, _STACK_IMAGE))
    code = rti.main(
        [
            "resolve_target_images.py",
            "--release-dir",
            str(_release_dir(tmp_path)),
            "--stack-tag",
            _STACK,
            "--docker-cmd",
            sys.executable,
        ]
    )
    assert code == 2
    assert calls == []


def test_main_requires_an_explicit_stack_tag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _fake_compose(monkeypatch, _services(_CANDIDATE_IMAGE, _CANDIDATE_IMAGE))
    monkeypatch.delenv("MEMORIA_STACK_TAG", raising=False)
    monkeypatch.delenv("MEMORIA_RELEASE_TAG", raising=False)
    code = rti.main(
        [
            "resolve_target_images.py",
            "--release-dir",
            str(_release_dir(tmp_path)),
            "--expected-tag",
            _CANDIDATE,
            "--docker-cmd",
            sys.executable,
        ]
    )
    assert code == 2
    assert calls == []


def test_main_rejects_a_missing_override_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _fake_compose(monkeypatch, _services(_CANDIDATE_IMAGE, _CANDIDATE_IMAGE))
    code = rti.main(
        [
            "resolve_target_images.py",
            "--release-dir",
            str(_release_dir(tmp_path)),
            "--stack-tag",
            _STACK,
            "--expected-tag",
            _CANDIDATE,
            "--override",
            str(tmp_path / "missing.override.yml"),
            "--docker-cmd",
            sys.executable,
        ]
    )
    assert code == 2
    assert calls == []


def test_main_rejects_an_invalid_candidate_tag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _fake_compose(monkeypatch, _services(_CANDIDATE_IMAGE, _CANDIDATE_IMAGE))
    code = rti.main(
        [
            "resolve_target_images.py",
            "--release-dir",
            str(_release_dir(tmp_path)),
            "--stack-tag",
            _STACK,
            "--expected-tag",
            "bad tag; rm -rf /",
            "--docker-cmd",
            sys.executable,
        ]
    )
    assert code == 2
    assert calls == []


def test_main_fails_on_the_real_live_override_chain_without_the_candidate_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The live-only override chain must be rejected, with the stack image named."""

    release_dir = _release_dir(tmp_path)
    live_override = tmp_path / "pre-cutover-live.override.yml"
    live_override.write_text("services: {}\n", encoding="utf-8")
    calls = _fake_compose(monkeypatch, _services(_STACK_IMAGE, _STACK_IMAGE))

    code = rti.main(
        [
            "resolve_target_images.py",
            "--release-dir",
            str(release_dir),
            "--stack-tag",
            _STACK,
            "--release-commit",
            "d96d4c29b7f13719d052b94647b2c59223ea70a1",
            "--expected-tag",
            _CANDIDATE,
            "--override",
            str(live_override),
            "--docker-cmd",
            "docker",
        ]
    )

    assert code == 1
    assert calls[0]["overrides"] == [str(live_override)]
    assert calls[0]["stack_tag"] == _STACK
    assert calls[0]["release_commit"] == "d96d4c29b7f13719d052b94647b2c59223ea70a1"


def test_main_succeeds_on_the_real_live_plus_candidate_override_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_dir = _release_dir(tmp_path)
    live_override = tmp_path / "pre-cutover-live.override.yml"
    candidate_override = tmp_path / "agent-component.override.yml"
    for override in (live_override, candidate_override):
        override.write_text("services: {}\n", encoding="utf-8")
    calls = _fake_compose(monkeypatch, _services(_CANDIDATE_IMAGE, _CANDIDATE_IMAGE))

    code = rti.main(
        [
            "resolve_target_images.py",
            "--release-dir",
            str(release_dir),
            "--stack-tag",
            _STACK,
            "--expected-tag",
            _CANDIDATE,
            "--expected-image",
            _CANDIDATE_IMAGE,
            "--override",
            str(live_override),
            "--override",
            str(candidate_override),
            "--docker-cmd",
            "docker",
        ]
    )

    assert code == 0
    assert calls[0]["overrides"] == [str(live_override), str(candidate_override)]


def test_main_cli_fails_when_compose_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        rti,
        "run_compose_config",
        lambda *args, **kwargs: (1, "", "docker daemon unavailable"),
    )
    code = rti.main(
        [
            "resolve_target_images.py",
            "--release-dir",
            str(_release_dir(tmp_path)),
            "--stack-tag",
            _STACK,
            "--expected-tag",
            _CANDIDATE,
        ]
    )
    assert code == 1


def test_main_cli_succeeds_when_verified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _fake_compose(monkeypatch, _services(_CANDIDATE_IMAGE, _CANDIDATE_IMAGE))
    override = tmp_path / "override.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    code = rti.main(
        [
            "resolve_target_images.py",
            str(_release_dir(tmp_path)),
            "--stack-tag",
            _STACK,
            "--expected-tag",
            _CANDIDATE,
            str(override),
        ]
    )
    assert code == 0
    assert calls[0]["overrides"] == [str(override)]
