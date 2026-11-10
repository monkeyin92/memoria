from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release_ops.sh"

from scripts.capture_runtime_inventory import (  # noqa: E402
    FLAG_ALLOWLIST,
    INSPECT_FORMATS,
    UNKNOWN,
    build_inventory,
    capture_from_backend,
    carry_non_host_metadata,
    compare_inventories,
    inspect_format,
    parse_prev_release,
    render_markdown,
    validate_inventory,
)

_COMMIT_A = "a" * 40
_COMMIT_C = "c" * 40
_COMMIT_D = "d" * 40
_SECRET = "do-not-leak-runtime-inventory"


def test_inspect_formats_match_release_ops_case_arms() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index("inspect_container() {")
    end = script.index("\n}\n", start)
    body = script[start:end]

    assert (
        'version|revision) format="{{index .Config.Labels \\"org.opencontainers.image.$2\\"}}"'
        in body
    )
    assert (
        INSPECT_FORMATS["version"] == '{{index .Config.Labels "org.opencontainers.image.version"}}'
    )
    assert (
        INSPECT_FORMATS["revision"]
        == '{{index .Config.Labels "org.opencontainers.image.revision"}}'
    )

    for field in ("chain", "image", "image-id", "release-tag", "running"):
        expected = INSPECT_FORMATS[field]
        assert inspect_format(field) == expected
        assert f"{field}) format='{expected}'" in body, (
            f"{field}: release_ops.sh arm missing exact format {expected!r}"
        )


def test_parse_prev_release_with_overrides() -> None:
    text = (
        "tag=20261028-assistant-emotion-v1\n"
        f"commit={_COMMIT_D}\n"
        "services=control-api voice-core-media-bridge\n"
        "overrides=voice-core-media-bridge /opt/memoria/component-releases/x/o.yml "
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa sha256:deadbeef\n"
    )
    parsed = parse_prev_release(text)
    assert parsed["tag"] == "20261028-assistant-emotion-v1"
    assert parsed["commit"] == _COMMIT_D
    assert parsed["services"] == ["control-api", "voice-core-media-bridge"]
    assert parsed["overrides"][0]["service"] == "voice-core-media-bridge"


def _flags(extra: dict[str, str] | None = None) -> dict[str, str]:
    flags = {key: UNKNOWN for key in FLAG_ALLOWLIST}
    if extra:
        flags.update(extra)
    return flags


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
                "revision": _COMMIT_A,
                "compose_chain": "/opt/memoria/releases/20261029-plan-head-start-v1/docker-compose.production.yml",
                "override_path": UNKNOWN,
                "started_at": "2026-10-29T06:29:49Z",
                "health": "healthy",
                "restarts": 0,
                "effective_flags": _flags(
                    {
                        "MEMORIA_RELEASE_TAG": "20261029-plan-head-start-v1",
                        "MEMORIA_RELEASE_COMMIT": _COMMIT_A,
                        "MEMORIA_SPEAKER_AUTHORITY_ENABLED": "false",
                    }
                ),
            },
            {
                "role": "voice-core-media-bridge",
                "container": "memoria-voice-core-media-bridge-1",
                "image": "memoria-agent:20261102-card-1-3-overlay-v1",
                "image_id": "sha256:" + "b" * 64,
                "registry_digest": UNKNOWN,
                "revision": "",
                "compose_chain": (
                    "/opt/memoria/releases/20261029-plan-head-start-v1/docker-compose.production.yml,"
                    "/opt/memoria/component-releases/20261102-card-1-3-overlay-v1/"
                    "agent-component.override.yml"
                ),
                "override_path": (
                    "/opt/memoria/component-releases/20261102-card-1-3-overlay-v1/"
                    "agent-component.override.yml"
                ),
                "started_at": "2026-11-02T07:07:46Z",
                "health": "healthy",
                "restarts": 0,
                "effective_flags": _flags(),
            },
            {
                "role": "media-edge",
                "container": "memoria-media-edge-1",
                "image": "memoria-media-edge:20261023-edge-fix-v1",
                "image_id": "sha256:" + "c" * 64,
                "registry_digest": UNKNOWN,
                "revision": "c44267c9221f904dd1eebddad920eb81da4bb53a",
                "compose_chain": "base,override",
                "override_path": "/opt/memoria/component-releases/edge/media-edge-component.override.yml",
                "started_at": "2026-10-23T15:31:43Z",
                "health": "healthy",
                "restarts": 0,
                "effective_flags": _flags(),
            },
        ],
        stack={
            "current_tag": "20261029-plan-head-start-v1",
            "current_commit": _COMMIT_A,
            "current_path": "/opt/memoria/releases/20261029-plan-head-start-v1",
        },
        rollback={
            "stack_prev": parse_prev_release(
                "tag=20261028-assistant-emotion-v1\n"
                f"commit={_COMMIT_D}\n"
                "services=control-api voice-core-media-bridge\n"
            ),
            "per_component": [
                {
                    "role": "voice-core-media-bridge",
                    "method": "agent-rollback.override.yml",
                    "target": "memoria-agent:20261101-asr-failover-v2",
                }
            ],
        },
    )
    assert validate_inventory(doc) == []
    roles = {c["role"] for c in doc["components"]}
    assert roles == {"control-api", "voice-core-media-bridge", "media-edge"}
    bridge = next(c for c in doc["components"] if c["role"] == "voice-core-media-bridge")
    assert bridge["revision_commit"] == UNKNOWN
    assert any("revision_commit unknown" in item for item in doc["unknowns"])
    assert any("not inferred from image name" in item for item in doc["unknowns"])
    assert doc["rollback"]["stack_prev"]["tag"] == "20261028-assistant-emotion-v1"
    assert doc["rollback"]["stack_prev"]["commit"] == _COMMIT_D
    rendered = render_markdown(doc)
    assert "media-edge" in rendered
    assert "Unknowns" in rendered


def test_validate_accepts_prefix_and_rejects_a_complete_mark() -> None:
    doc = build_inventory(
        evidence_class="handoff_reconstruction",
        observations=[
            {
                "role": "voice-core-media-bridge",
                "image": "memoria-agent:20261102-card-1-3-overlay-v1",
                "image_id": "263748d8bb79",
                "revision": "",
                "effective_flags": _flags(),
            }
        ],
    )
    bridge = doc["components"][0]
    assert bridge["image_id"] == "263748d8bb79"
    assert bridge["image_id_complete"] is False
    assert bridge["revision_commit"] == UNKNOWN
    assert "prefix" in "\n".join(doc["unknowns"])
    assert validate_inventory(doc) == []
    bridge["image_id_complete"] = True
    errors = validate_inventory(doc)
    assert any("not a full id" in err for err in errors)


def test_validate_rejects_invented_looking_incomplete_schema() -> None:
    doc = {
        "schema_version": 1,
        "captured_at": None,
        "memoria_root": None,
        "stack": {
            "current_tag": "20261029-plan-head-start-v1",
            "current_commit": "2c6ff2cc",
            "current_path": UNKNOWN,
        },
        "components": [
            {
                "role": "voice-core-media-bridge",
                "image_ref": "memoria-agent:20261016-readiness-b54189c",
                "image_id": "263748d8bb79",
                "image_id_complete": True,
                "registry_digest": UNKNOWN,
                "revision_commit": "b54189c",
            }
        ],
        "rollback": {
            "stack_prev": {
                "tag": "20261028-assistant-emotion-v1",
                "commit": "3e42b95d",
                "services": UNKNOWN,
                "overrides": [],
            },
            "per_component": [],
        },
        "unknowns": [],
    }
    errors = validate_inventory(doc)
    assert any("evidence_class" in err for err in errors)
    assert any("revision_commit" in err and "40-hex" in err for err in errors)
    assert any("not a full id" in err for err in errors)
    assert any("current_commit" in err for err in errors)
    assert any("stack_prev.commit" in err for err in errors)


def test_validate_rejects_revision_equal_to_image_tag() -> None:
    tag = "a" * 40
    doc = build_inventory(
        evidence_class="lab_fixture",
        observations=[
            {
                "role": "control-api",
                "image": f"memoria-control-api:{tag}",
                "image_id": "sha256:" + "d" * 64,
                "revision": tag,
                "effective_flags": _flags(),
            }
        ],
    )
    errors = validate_inventory(doc)
    assert any("must not equal image tag" in err for err in errors)


def test_compare_reports_component_diffs() -> None:
    base_obs = {
        "role": "control-api",
        "image": "memoria-control-api:a",
        "image_id": "sha256:" + "e" * 64,
        "revision": _COMMIT_A,
        "effective_flags": _flags(),
        "health": "healthy",
        "override_path": UNKNOWN,
    }
    left = build_inventory(evidence_class="lab_fixture", observations=[base_obs])
    right_obs = dict(base_obs)
    right_obs["image_id"] = "sha256:" + "f" * 64
    right = build_inventory(evidence_class="lab_fixture", observations=[right_obs])
    diffs = compare_inventories(left, right)
    assert any(item.startswith("control-api.image_id:") for item in diffs)


class _FixtureBackend:
    def __init__(self, containers: dict[str, dict[str, object]]) -> None:
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
        return {str(key): str(value) for key, value in flags.items()}


def _override_text(service: str, image: str) -> str:
    return f"services:\n  {service}:\n    image: {image}\n"


def test_capture_from_backend_reads_host_layout(tmp_path: Path) -> None:
    root = tmp_path / "memoria"
    release = root / "releases" / "20261029-plan-head-start-v1"
    cutover = release / ".cutover"
    cutover.mkdir(parents=True)
    (release / ".env").write_text(
        "MEMORIA_RELEASE_TAG=20261029-plan-head-start-v1\n"
        f"MEMORIA_RELEASE_COMMIT={_COMMIT_C}\n"
        f"API_KEY={_SECRET}\n",
        encoding="utf-8",
    )
    (cutover / "prev-release").write_text(
        "tag=20261028-assistant-emotion-v1\n"
        f"commit={_COMMIT_D}\n"
        "services=control-api voice-core-media-bridge\n",
        encoding="utf-8",
    )
    current = root / "current"
    current.symlink_to(release)

    bridge_rel = "component-releases/card/agent-component.override.yml"
    edge_rel = "component-releases/edge/media-edge-component.override.yml"
    bridge_file = root / bridge_rel
    edge_file = root / edge_rel
    bridge_file.parent.mkdir(parents=True)
    edge_file.parent.mkdir(parents=True)
    bridge_body = _override_text(
        "voice-core-media-bridge", "memoria-agent:20261016-readiness-b54189c"
    )
    edge_body = _override_text("media-edge", "memoria-media-edge:20261023-edge-fix-v1")
    bridge_file.write_text(bridge_body, encoding="utf-8")
    edge_file.write_text(edge_body, encoding="utf-8")
    edge_file.with_name("media-edge-rollback.override.yml").write_text(
        _override_text("media-edge", "memoria-media-edge:20261023-protected-crisis-v1"),
        encoding="utf-8",
    )
    bridge_file.with_name("agent-component.rollback.override.yml").write_text(
        _override_text("voice-core-media-bridge", "memoria-agent:20261101-asr-failover-v2"),
        encoding="utf-8",
    )
    tsv_sha = "ab" * 32
    (root / "state").mkdir()
    (root / "state" / "component-overrides.tsv").write_text(
        "voice-core-media-bridge "
        f"/opt/memoria/{bridge_rel} {tsv_sha} /tmp/agent-component.override.yml\n",
        encoding="utf-8",
    )
    bridge_chain = (
        "/opt/memoria/releases/20261029-plan-head-start-v1/docker-compose.production.yml,"
        "/opt/memoria/component-releases/card/pre-cutover-live.override.yml,"
        f"/opt/memoria/{bridge_rel}"
    )
    edge_chain = (
        "/opt/memoria/releases/20261004-first-warm-v1/docker-compose.production.yml,"
        f"/opt/memoria/{edge_rel}"
    )
    allow = _flags(
        {
            "MEMORIA_RELEASE_TAG": "20261029-plan-head-start-v1",
            "MEMORIA_RELEASE_COMMIT": _COMMIT_C,
            "TTS_PROVIDER": "stepfun_primary",
        }
    )
    leaked = dict(allow)
    leaked["API_KEY"] = _SECRET

    backend = _FixtureBackend(
        {
            "memoria-control-api-1": {
                "image": "memoria-control-api:20261029-plan-head-start-v1",
                "image-id": "sha256:" + "1" * 64,
                "revision": _COMMIT_C,
                "version": "20261029-plan-head-start-v1",
                "chain": str(release / "docker-compose.production.yml"),
                "running": "true",
                "release-tag": "20261029-plan-head-start-v1",
                "started_at": "2026-10-29T06:29:49Z",
                "health": "healthy",
                "restarts": 0,
                "registry_digest": UNKNOWN,
                "flags": leaked,
            },
            "memoria-voice-core-media-bridge-1": {
                "image": "memoria-agent:20261016-readiness-b54189c",
                "image-id": "263748d8bb79",
                "revision": "",
                "version": "20261016-readiness-b54189c",
                "chain": bridge_chain,
                "running": "true",
                "release-tag": "20261029-plan-head-start-v1",
                "started_at": "2026-11-02T07:07:46Z",
                "health": "healthy",
                "restarts": 0,
                "registry_digest": UNKNOWN,
                "flags": leaked,
            },
            "memoria-media-edge-1": {
                "image": "memoria-media-edge:20261023-edge-fix-v1",
                "image-id": "sha256:" + "c" * 64,
                "revision": "c44267c9221f904dd1eebddad920eb81da4bb53a",
                "version": "20261023-edge-fix-v1",
                "chain": edge_chain,
                "running": "true",
                "release-tag": "",
                "started_at": "2026-10-23T15:31:43Z",
                "health": "healthy",
                "restarts": 0,
                "registry_digest": UNKNOWN,
                "flags": allow,
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
                "flags": {"API_KEY": _SECRET},
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
    assert _SECRET not in json.dumps(doc)
    assert doc["evidence_class"] == "lab_fixture"
    assert doc["stack"]["current_tag"] == "20261029-plan-head-start-v1"
    assert doc["stack"]["current_commit"] == _COMMIT_C
    prev = doc["rollback"]["stack_prev"]
    assert prev["tag"] == "20261028-assistant-emotion-v1"
    assert prev["commit"] == _COMMIT_D
    assert prev["services"] == ["control-api", "voice-core-media-bridge"]
    roles = {c["role"] for c in doc["components"]}
    assert roles == {"control-api", "voice-core-media-bridge", "media-edge"}
    bridge = next(c for c in doc["components"] if c["role"] == "voice-core-media-bridge")
    assert bridge["revision_commit"] == UNKNOWN
    assert bridge["image_id"] == "263748d8bb79"
    assert bridge["image_id_complete"] is False
    assert "b54189c" not in bridge["revision_commit"]
    file_sha = hashlib.sha256(bridge_body.encode("utf-8")).hexdigest()
    assert bridge["override_path"] == f"/opt/memoria/{bridge_rel}"
    assert bridge["override_sha256"] == file_sha
    assert bridge["override_sha256"] != tsv_sha
    assert any("disagrees with" in item for item in doc["unknowns"])
    assert any("not inferred from image name" in item for item in doc["unknowns"])
    assert any("prefix" in item for item in doc["unknowns"])
    anchors = doc["rollback"]["per_component"]
    assert {
        "role": "media-edge",
        "method": "media-edge-rollback.override.yml",
        "target": "memoria-media-edge:20261023-protected-crisis-v1",
    } in anchors
    assert {
        "role": "voice-core-media-bridge",
        "method": "agent-component.rollback.override.yml",
        "target": "memoria-agent:20261101-asr-failover-v2",
    } in anchors
    assert {
        "role": "control-api",
        "method": "release_ops.rollback",
        "target": f"20261028-assistant-emotion-v1@{_COMMIT_D}",
    } in anchors
    control = next(c for c in doc["components"] if c["role"] == "control-api")
    assert control["effective_flags"]["TTS_PROVIDER"] == "stepfun_primary"
    assert "API_KEY" not in control["effective_flags"]


def test_validate_rejects_unknown_line_for_a_filled_field() -> None:
    doc = build_inventory(
        evidence_class="handoff_reconstruction",
        observations=[],
        firmware={
            "build": "25",
            "app_commit": UNKNOWN,
            "assets_hash": "0be9af66",
            "evidence": "USB flash",
        },
    )
    assert validate_inventory(doc) == []
    assert "firmware.app_commit unknown" in doc["unknowns"]
    doc["unknowns"].append("firmware.build unknown")
    errors = validate_inventory(doc)
    assert any("firmware.build" in err and "still calls it unknown" in err for err in errors)


def test_carry_forward_keeps_non_host_metadata() -> None:
    previous = {
        "components": [
            {
                "role": "voice-core-media-bridge",
                "status": {"code": UNKNOWN, "wired": "2026-11-02"},
            },
            {
                "role": "firmware",
                "container": UNKNOWN,
                "image_ref": UNKNOWN,
                "image_id": UNKNOWN,
                "registry_digest": UNKNOWN,
                "revision_commit": UNKNOWN,
                "status": {"code": "2026-10-23"},
            },
        ],
        "firmware": {
            "build": "25",
            "app_commit": UNKNOWN,
            "assets_hash": "0be9af66",
            "evidence": "USB flash",
        },
        "miniprogram": {"version": UNKNOWN, "evidence": "local upload"},
    }
    doc = build_inventory(
        evidence_class="live_capture",
        observations=[
            {
                "role": "voice-core-media-bridge",
                "image": "memoria-agent:20261106-card5-committed-boundary-v1",
                "image_id": "sha256:" + "1" * 64,
                "revision": _COMMIT_A,
                "effective_flags": _flags(),
            }
        ],
        firmware=previous["firmware"],
        miniprogram=previous["miniprogram"],
        captured_at="2026-11-10T07:00:00+00:00",
    )
    notes = carry_non_host_metadata(doc, previous)
    bridge = next(c for c in doc["components"] if c["role"] == "voice-core-media-bridge")
    assert bridge["status"]["wired"] == "2026-11-02"
    assert any(c["role"] == "firmware" for c in doc["components"])
    assert "firmware.app_commit unknown" in doc["unknowns"]
    assert "firmware.build unknown" not in doc["unknowns"]
    assert "miniprogram.version unknown" in doc["unknowns"]
    assert len(notes) == 2
    doc["unknowns"].extend(notes)
    assert validate_inventory(doc) == []


def test_seeded_repo_inventory_validates() -> None:
    path = ROOT / "docs" / "ops" / "current-runtime-inventory.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["evidence_class"] == "live_capture"
    assert doc["captured_at"] is not None
    assert validate_inventory(doc) == []
    bridge = next(c for c in doc["components"] if c["role"] == "voice-core-media-bridge")
    assert bridge["image_ref"] == "memoria-agent:20261106-card5-committed-boundary-v1"
    assert bridge["revision_commit"] == "b03756b3c5268c90596bd1acda32a7b314094d9a"
    assert bridge["image_id"].startswith("sha256:")
    assert doc["stack"]["current_tag"] == "20261029-plan-head-start-v1"
    assert doc["stack"]["current_commit"] == "2c6ff2ccf3212e9d8b9b45f94ffad28dd8b0c82d"
    assert doc["rollback"]["stack_prev"]["tag"] == "20261028-assistant-emotion-v1"
    assert doc["rollback"]["stack_prev"]["commit"] == "3e42b95de956d0b7367a24d6cce2fb62c48a481a"
    assert "None" not in render_markdown(doc)
    assert doc["unknowns"]
