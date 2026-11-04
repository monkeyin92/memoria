from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release_ops.sh"

from scripts.capture_runtime_inventory import (  # noqa: E402
    FLAG_ALLOWLIST,
    INSPECT_FORMATS,
    UNKNOWN,
    build_inventory,
    capture_from_backend,
    compare_inventories,
    inspect_format,
    parse_prev_release,
    render_markdown,
    validate_inventory,
)


def test_inspect_formats_match_release_ops_case_arms() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index("inspect_container() {")
    end = script.index("\n}\n", start)
    body = script[start:end]

    assert 'version|revision) format="{{index .Config.Labels \\"org.opencontainers.image.$2\\"}}"' in body
    assert INSPECT_FORMATS["version"] == '{{index .Config.Labels "org.opencontainers.image.version"}}'
    assert INSPECT_FORMATS["revision"] == '{{index .Config.Labels "org.opencontainers.image.revision"}}'

    for field in ("chain", "image", "image-id", "release-tag", "running"):
        expected = INSPECT_FORMATS[field]
        assert inspect_format(field) == expected
        assert f"{field}) format='{expected}'" in body, (
            f"{field}: release_ops.sh arm missing exact format {expected!r}"
        )


def test_parse_prev_release_with_overrides() -> None:
    text = (
        "tag=20261028-assistant-emotion-v1\n"
        "commit=3e42b95de956d0b7367a24d6cce2fb62c48a481a\n"
        "services=control-api voice-core-media-bridge\n"
        "overrides=voice-core-media-bridge /opt/memoria/component-releases/x/o.yml "
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa sha256:deadbeef\n"
    )
    parsed = parse_prev_release(text)
    assert parsed["tag"] == "20261028-assistant-emotion-v1"
    assert parsed["commit"] == "3e42b95de956d0b7367a24d6cce2fb62c48a481a"
    assert parsed["services"] == ["control-api", "voice-core-media-bridge"]
    assert parsed["overrides"][0]["service"] == "voice-core-media-bridge"


def test_build_inventory_records_unknowns_and_media_edge() -> None:
    doc = build_inventory(
        evidence_class="lab_fixture",
        captured_at="2026-11-04T00:00:00+00:00",
        memoria_root="/tmp/lab-memoria",
        observations=[
            {
                "role": "control-api",
                "container": "memoria-control-api-1",
                "image": "memoria-control-api:20261029-plan-head-start-v1",
                "image_id": "sha256:" + "a" * 64,
                "registry_digest": UNKNOWN,
                "revision": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
                "stack_commit": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
                "compose_chain": "/opt/memoria/releases/20261029-plan-head-start-v1/docker-compose.production.yml",
                "override_path": UNKNOWN,
                "started_at": "2026-10-29T06:29:49Z",
                "health": "healthy",
                "restarts": 0,
                "effective_flags": {
                    "MEMORIA_RELEASE_TAG": "20261029-plan-head-start-v1",
                    "MEMORIA_RELEASE_COMMIT": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
                    "TTS_PROVIDER": UNKNOWN,
                    "STEPFUN_TTS_ENABLED": UNKNOWN,
                    "STEPFUN_TTS_VOICE": UNKNOWN,
                    "MEMORIA_SPEAKER_AUTHORITY_ENABLED": "false",
                },
            },
            {
                "role": "voice-core-media-bridge",
                "container": "memoria-voice-core-media-bridge-1",
                "image": "memoria-agent:20261102-card-1-3-overlay-v1",
                "image_id": "sha256:" + "b" * 64,
                "registry_digest": UNKNOWN,
                "revision": "",
                "stack_commit": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
                "compose_chain": (
                    "/opt/memoria/releases/20261029-plan-head-start-v1/docker-compose.production.yml,"
                    "/opt/memoria/component-releases/20261102-card-1-3-overlay-v1/agent-component.override.yml"
                ),
                "override_path": (
                    "/opt/memoria/component-releases/20261102-card-1-3-overlay-v1/"
                    "agent-component.override.yml"
                ),
                "started_at": "2026-11-02T07:07:46Z",
                "health": "healthy",
                "restarts": 0,
                "effective_flags": {k: UNKNOWN for k in FLAG_ALLOWLIST},
            },
            {
                "role": "media-edge",
                "container": "memoria-media-edge-1",
                "image": "memoria-media-edge:20261023-edge-fix-v1",
                "image_id": "sha256:" + "c" * 64,
                "registry_digest": UNKNOWN,
                "revision": "c44267c9221f904dd1eebddad920eb81da4bb53a",
                "stack_commit": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
                "compose_chain": "base,override",
                "override_path": "/opt/memoria/component-releases/edge/media-edge-component.override.yml",
                "started_at": "2026-10-23T15:31:43Z",
                "health": "healthy",
                "restarts": 0,
                "effective_flags": {k: UNKNOWN for k in FLAG_ALLOWLIST},
            },
        ],
        stack={
            "current_tag": "20261029-plan-head-start-v1",
            "current_commit": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
            "current_path": "/opt/memoria/releases/20261029-plan-head-start-v1",
        },
        rollback={
            "stack_prev": parse_prev_release(
                "tag=20261028-assistant-emotion-v1\n"
                "commit=3e42b95de956d0b7367a24d6cce2fb62c48a481a\n"
                "services=control-api voice-core-media-bridge\n"
            ),
            "per_component": [
                {
                    "role": "voice-core-media-bridge",
                    "method": "agent-component.rollback.override.yml",
                    "target": "memoria-agent:20261101-asr-failover-v2",
                }
            ],
        },
    )
    errors = validate_inventory(doc)
    assert errors == []
    roles = {c["role"] for c in doc["components"]}
    assert roles == {"control-api", "voice-core-media-bridge", "media-edge"}
    bridge = next(c for c in doc["components"] if c["role"] == "voice-core-media-bridge")
    assert bridge["image_revision"] == UNKNOWN
    assert any("image_revision unknown" in u for u in doc["unknowns"])
    assert doc["rollback"]["stack_prev"]["tag"] == "20261028-assistant-emotion-v1"
    assert doc["rollback"]["stack_prev"]["commit"].startswith("3e42b95d")
    md = render_markdown(doc)
    assert "media-edge" in md
    assert "Unknowns" in md


def test_validate_rejects_short_image_id_prefix() -> None:
    doc = build_inventory(
        evidence_class="handoff_reconstruction",
        observations=[
            {
                "role": "voice-core-media-bridge",
                "image": "memoria-agent:20261102-card-1-3-overlay-v1",
                "image_id": "sha256:" + "2" * 64,
                "revision": "1111111111111111111111111111111111111111",
                "stack_commit": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
                "effective_flags": {k: "x" for k in FLAG_ALLOWLIST},
            }
        ],
    )
    doc["components"][0]["image_id"] = "263748d8bb79"
    errors = validate_inventory(doc)
    assert any("short HANDOFF prefixes are not ids" in e for e in errors)


def test_validate_rejects_revision_equal_to_image_tag() -> None:
    tag = "20261029-plan-head-start-v1"
    doc = {
        "schema_version": 1,
        "evidence_class": "lab_fixture",
        "captured_at": UNKNOWN,
        "memoria_root": UNKNOWN,
        "stack": {
            "current_tag": UNKNOWN,
            "current_commit": UNKNOWN,
            "current_path": UNKNOWN,
        },
        "components": [
            {
                "role": "control-api",
                "image_ref": f"memoria-control-api:{tag}",
                "image_id": "sha256:" + "d" * 64,
                "registry_digest": UNKNOWN,
                "image_revision": tag,
                "stack_commit": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
                "effective_flags": {k: "x" for k in FLAG_ALLOWLIST},
            }
        ],
        "firmware": {},
        "miniprogram": {},
        "rollback": {"stack_prev": {}, "per_component": []},
        "unknowns": [],
    }
    errors = validate_inventory(doc)
    assert any("must not equal image tag" in e for e in errors)


def test_compare_reports_component_diffs() -> None:
    base_obs = {
        "role": "control-api",
        "image": "memoria-control-api:a",
        "image_id": "sha256:" + "e" * 64,
        "revision": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
        "stack_commit": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
        "effective_flags": {k: "x" for k in FLAG_ALLOWLIST},
        "health": "healthy",
        "override_path": UNKNOWN,
    }
    left = build_inventory(evidence_class="lab_fixture", observations=[base_obs])
    right_obs = dict(base_obs)
    right_obs["image_id"] = "sha256:" + "f" * 64
    right = build_inventory(evidence_class="lab_fixture", observations=[right_obs])
    diffs = compare_inventories(left, right)
    assert any(d.startswith("control-api.image_id:") for d in diffs)


class _FixtureBackend:
    def __init__(self, containers: dict[str, dict[str, str | int | dict[str, str]]]) -> None:
        self._containers = containers

    def list_memoria_containers(self) -> list[str]:
        return sorted(self._containers)

    def inspect_field(self, container: str, field: str) -> str:
        return str(self._containers[container][field])

    def inspect_started_at(self, container: str) -> str:
        return str(self._containers[container]["started_at"])

    def inspect_health(self, container: str) -> str:
        return str(self._containers[container]["health"])

    def inspect_restarts(self, container: str) -> int:
        restarts = self._containers[container]["restarts"]
        assert isinstance(restarts, int)
        return restarts

    def inspect_repo_digest(self, container: str) -> str:
        return str(self._containers[container].get("registry_digest", UNKNOWN))

    def inspect_flags(self, container: str) -> dict[str, str]:
        flags = self._containers[container].get("flags", {})
        assert isinstance(flags, dict)
        return {k: str(flags.get(k, UNKNOWN)) for k in FLAG_ALLOWLIST}


def test_capture_from_backend_reads_prev_release(tmp_path: Path) -> None:
    root = tmp_path / "memoria"
    release = root / "releases" / "20261029-plan-head-start-v1"
    cutover = release / ".cutover"
    cutover.mkdir(parents=True)
    (release / ".env").write_text(
        "MEMORIA_RELEASE_TAG=20261029-plan-head-start-v1\n"
        "MEMORIA_RELEASE_COMMIT=2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d\n",
        encoding="utf-8",
    )
    (cutover / "prev-release").write_text(
        "tag=20261028-assistant-emotion-v1\n"
        "commit=3e42b95de956d0b7367a24d6cce2fb62c48a481a\n"
        "services=control-api voice-core-media-bridge\n",
        encoding="utf-8",
    )
    current = root / "current"
    current.symlink_to(release)
    (root / "state").mkdir()
    (root / "state" / "component-overrides.tsv").write_text(
        "voice-core-media-bridge /opt/memoria/component-releases/x/o.yml "
        + ("ab" * 32)
        + " /tmp/o.yml\n",
        encoding="utf-8",
    )

    backend = _FixtureBackend(
        {
            "memoria-control-api-1": {
                "image": "memoria-control-api:20261029-plan-head-start-v1",
                "image-id": "sha256:" + "1" * 64,
                "revision": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
                "version": "20261029-plan-head-start-v1",
                "chain": str(release / "docker-compose.production.yml"),
                "running": "true",
                "release-tag": "20261029-plan-head-start-v1",
                "started_at": "2026-10-29T06:29:49Z",
                "health": "healthy",
                "restarts": 0,
                "registry_digest": UNKNOWN,
                "flags": {
                    "MEMORIA_RELEASE_TAG": "20261029-plan-head-start-v1",
                    "MEMORIA_RELEASE_COMMIT": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
                    "TTS_PROVIDER": "stepfun_primary",
                    "STEPFUN_TTS_ENABLED": "true",
                    "STEPFUN_TTS_VOICE": "wenrounansheng",
                    "MEMORIA_SPEAKER_AUTHORITY_ENABLED": "false",
                },
            },
            "memoria-voice-core-media-bridge-1": {
                "image": "memoria-agent:20261102-card-1-3-overlay-v1",
                "image-id": "sha256:" + "2" * 64,
                "revision": "1111111111111111111111111111111111111111",
                "version": "20261102-card-1-3-overlay-v1",
                "chain": (
                    f"{release / 'docker-compose.production.yml'},"
                    f"{root / 'component-releases' / 'x' / 'agent-component.override.yml'}"
                ),
                "running": "true",
                "release-tag": "20261029-plan-head-start-v1",
                "started_at": "2026-11-02T07:07:46Z",
                "health": "healthy",
                "restarts": 0,
                "registry_digest": UNKNOWN,
                "flags": {
                    "MEMORIA_RELEASE_TAG": "20261029-plan-head-start-v1",
                    "MEMORIA_RELEASE_COMMIT": "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d",
                    "TTS_PROVIDER": "stepfun_primary",
                    "STEPFUN_TTS_ENABLED": "true",
                    "STEPFUN_TTS_VOICE": "wenrounansheng",
                    "MEMORIA_SPEAKER_AUTHORITY_ENABLED": "false",
                },
            },
            "memoria-device-state-redis-1": {
                "image": "redis:7",
                "image-id": "sha256:" + "9" * 64,
                "revision": "",
                "version": "",
                "chain": "x",
                "running": "true",
                "release-tag": "",
                "started_at": "t",
                "health": "healthy",
                "restarts": 0,
                "flags": {},
            },
        }
    )
    doc = capture_from_backend(
        backend,
        evidence_class="lab_fixture",
        memoria_root=root,
        captured_at="2026-11-04T01:00:00+00:00",
    )
    assert validate_inventory(doc) == []
    assert doc["stack"]["current_tag"] == "20261029-plan-head-start-v1"
    assert doc["rollback"]["stack_prev"]["tag"] == "20261028-assistant-emotion-v1"
    roles = {c["role"] for c in doc["components"]}
    assert "control-api" in roles
    assert "voice-core-media-bridge" in roles
    assert "other" not in roles
    bridge = next(c for c in doc["components"] if c["role"] == "voice-core-media-bridge")
    assert bridge["override_path"].endswith("agent-component.override.yml")


def test_seeded_repo_inventory_validates() -> None:
    path = ROOT / "docs" / "ops" / "current-runtime-inventory.json"
    if not path.is_file():
        pytest.skip("seed inventory not written yet")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["evidence_class"] == "handoff_reconstruction"
    assert validate_inventory(doc) == []
    assert doc["unknowns"], "reconstruction must list unknowns explicitly"
