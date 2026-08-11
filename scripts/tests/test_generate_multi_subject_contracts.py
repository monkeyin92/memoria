"""Regression tests for the multi-subject canonical contract generator (ADR-0033).

These tests pin the enum value sets, fail-closed defaults and generated
artifacts to the canonical schema. Any documented product change must update
the schema AND these pinned expectations in the same change.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import generate_multi_subject_contracts as gen

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / gen.SCHEMA_REL_PATH
GENERATED_ROOT = REPO_ROOT / "packages/contracts/generated"
MINIPROGRAM_GENERATED = REPO_ROOT / "apps/miniprogram/generated/multi-subject-contracts.js"

EXPECTED_VALUES: dict[str, tuple[str, ...]] = {
    "DeviceDeclaredMode": ("parent_for_child", "self_use", "child_for_parent", "family_shared"),
    "ServiceMode": (
        "student_minor",
        "adult_companion",
        "senior_companion",
        "family_shared",
        "adult_archive",
        "self_preview",
        "legacy_access",
        "unknown_safe",
    ),
    "SubjectCategory": ("unknown", "minor", "adult"),
    "AgeBand": ("unknown", "under_14", "14_17", "adult"),
    "AgeEvidenceStatus": ("unverified", "verified", "disputed"),
    "BindingRole": (
        "account_owner",
        "device_admin",
        "primary_subject",
        "guardian",
        "delegate",
        "emergency_contact",
        "member",
    ),
    "RelationshipStatus": ("pending", "active", "suspended", "revoked", "expired", "disputed"),
    "SpeakerState": ("unknown", "unconfirmed", "confirmed"),
    "MemoryScope": (
        "unknown",
        "session_ephemeral",
        "personal_private",
        "guardian_summary",
        "family_shared",
        "legacy_archive",
    ),
    "PolicyEffect": ("deny", "allow", "allow_with_obligations"),
    "PolicyObligation": (
        "DO_NOT_PERSIST",
        "DO_NOT_WRITE_LEARNING_PROGRESS",
        "PERSIST_AGGREGATE_ONLY",
        "REDACT_TRANSCRIPT",
        "MINIMAL_NOTIFICATION_CONTENT",
        "REQUIRE_SPEAKER_CONFIRMATION",
        "REQUIRE_GUARDIAN_APPROVAL",
        "REQUIRE_SUBJECT_APPROVAL",
        "REQUIRE_STEP_UP_AUTH",
        "MAX_SESSION_SECONDS",
        "QUIET_HOURS",
        "DEPENDENCY_GUARD",
        "REALITY_REMINDER",
        "AI_IDENTITY_CLARIFICATION",
        "NO_MODEL_TRAINING",
        "RETENTION_TTL",
        "NOTIFY_EMERGENCY_CONTACT",
        "WRITE_SUBJECT_SCOPED_PROGRESS",
        "WRITE_POLICY_RECEIPT",
    ),
    "Capability": (
        "chat",
        "tutor",
        "english_practice",
        "memory_capture",
        "memory_promotion",
        "family_shared_memory_proposal",
        "family_shared_memory_approval",
        "family_shared_memory_promotion",
        "memory_recall_private",
        "guardian_summary_view",
        "voice_profile_create",
        "voice_clone_use",
        "digital_self_preview",
        "legacy_grant_create",
        "payment",
        "raw_audio_retention",
        "model_training_contribution",
        "crisis_notification",
        "device_ownership_transfer",
    ),
}

EXPECTED_DEFAULTS: dict[str, str | None] = {
    "DeviceDeclaredMode": None,
    "ServiceMode": "unknown_safe",
    "SubjectCategory": "unknown",
    "AgeBand": "unknown",
    "AgeEvidenceStatus": "unverified",
    "BindingRole": None,
    "RelationshipStatus": None,
    "SpeakerState": "unknown",
    "MemoryScope": "unknown",
    "PolicyEffect": "deny",
    "PolicyObligation": None,
    "Capability": None,
}


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_python_module(path: Path) -> object:
    name = "multi_subject_contracts"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_schema_enum_sets_match_documented_values() -> None:
    defs = _schema()["$defs"]
    for name, values in EXPECTED_VALUES.items():
        assert tuple(defs[name]["enum"]) == values, f"{name} value set drifted"


def test_schema_fail_closed_defaults() -> None:
    defs = _schema()["$defs"]
    for name, expected in EXPECTED_DEFAULTS.items():
        meta = defs[name]["x-memoria"]
        assert meta["missing_value_policy"] == "fail_closed", f"{name} must fail closed"
        assert meta.get("default") == expected, f"{name} default drifted"


def test_schema_metadata_is_complete() -> None:
    data = _schema()
    assert data["properties"]["schema_version"]["const"] == 2
    defs = data["$defs"]
    assert tuple(defs) == gen.REQUIRED_ENUMS
    version_two = {"Capability", "CancelReason", "MemoryEventType"}
    for name, schema in defs.items():
        meta = schema["x-memoria"]
        assert meta["frozen_by"] == "ADR-0033"
        assert meta["contract_version"] == (2 if name in version_two else 1)
        assert set(meta["value_notes"]) == set(schema["enum"])


def test_v2_structured_obligations_are_marked_unique_items() -> None:
    object_defs = _schema()["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]
    for name in ("RuntimeProfileV2", "RuntimeProfileSignedV2", "PolicyDecision", "PolicyReceiptV2"):
        assert object_defs[name]["properties"]["obligations"]["uniqueItems"] is True


def test_generated_artifacts_match_fresh_generation(tmp_path: Path) -> None:
    schema_tmp = tmp_path / gen.SCHEMA_REL_PATH
    schema_tmp.parent.mkdir(parents=True)
    schema_tmp.write_bytes(SCHEMA_PATH.read_bytes())
    fresh = gen.build_outputs(tmp_path)
    assert {path.relative_to(tmp_path) for path in fresh} == {Path(rel) for rel in gen.OUTPUT_FILES}
    for rel in gen.OUTPUT_FILES:
        on_disk = REPO_ROOT / rel
        assert on_disk.read_text(encoding="utf-8") == fresh[tmp_path / rel]
        assert on_disk.read_text(encoding="utf-8") != "", f"empty artifact: {on_disk.relative_to(REPO_ROOT)}"


def test_generation_is_deterministic(tmp_path: Path) -> None:
    outputs: list[dict[Path, str]] = []
    for index in range(2):
        root = tmp_path / str(index)
        schema_tmp = root / gen.SCHEMA_REL_PATH
        schema_tmp.parent.mkdir(parents=True)
        schema_tmp.write_bytes(SCHEMA_PATH.read_bytes())
        outputs.append(gen.build_outputs(root))
    first = list(outputs[0].items())
    second = list(outputs[1].items())
    assert [path.name for path, _ in first] == [path.name for path, _ in second]
    for (_, a), (_, b) in zip(first, second, strict=True):
        assert a == b


def test_check_mode_detects_drift(tmp_path: Path) -> None:
    schema_tmp = tmp_path / gen.SCHEMA_REL_PATH
    schema_tmp.parent.mkdir(parents=True)
    schema_tmp.write_bytes(SCHEMA_PATH.read_bytes())
    assert gen.run(["--write", "--output-root", str(tmp_path)]) == 0
    assert gen.run(["--check", "--output-root", str(tmp_path)]) == 0
    python_out = tmp_path / "packages/contracts/generated/python/multi_subject_contracts.py"
    python_out.write_text(python_out.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    assert gen.run(["--check", "--output-root", str(tmp_path)]) == 1
    assert gen.run(["--write", "--output-root", str(tmp_path)]) == 0
    assert gen.run(["--check", "--output-root", str(tmp_path)]) == 0


def test_python_artifact_importable_and_fail_closed() -> None:
    module = _load_python_module(GENERATED_ROOT / "python/multi_subject_contracts.py")
    assert len(module.ENUM_REGISTRY) == len(gen.REQUIRED_ENUMS)
    for name, values in EXPECTED_VALUES.items():
        enum_cls = module.ENUM_REGISTRY[name]
        assert tuple(member.value for member in enum_cls) == values
        assert enum_cls.from_value(values[0]) is not None
        assert enum_cls.from_value("not-a-canonical-value") is None
        assert enum_cls.from_value(None) is None
    assert module.SERVICE_MODE_DEFAULT == module.ServiceMode.SERVICE_MODE_UNKNOWN_SAFE
    assert module.POLICY_EFFECT_DEFAULT == module.PolicyEffect.POLICY_EFFECT_DENY
    assert module.SUBJECT_CATEGORY_DEFAULT == module.SubjectCategory.SUBJECT_CATEGORY_UNKNOWN
    assert module.DEVICE_DECLARED_MODE_DEFAULT is None
    assert module.AgeBand.AGE_BAND_14_17.value == "14_17"


def test_typescript_artifact_syntax_and_values() -> None:
    ts_path = GENERATED_ROOT / "typescript/multi_subject_contracts.ts"
    text = ts_path.read_text(encoding="utf-8")
    for name, values in EXPECTED_VALUES.items():
        assert f"export const {name} =" in text
        assert f"export function is{name}(" in text
        for value in values:
            assert f'"{value}"' in text
    assert "AgeBand14_17" in text
    if shutil.which("node") is None:
        pytest.skip("node unavailable; skipping syntax check")
    result = subprocess.run(["node", "--check", str(ts_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_miniprogram_commonjs_artifact_is_canonical_and_executable() -> None:
    text = MINIPROGRAM_GENERATED.read_text(encoding="utf-8")
    assert 'const CONTRACTS_SCHEMA_VERSION = 2;' in text
    assert f'const SOURCE_HASH = "{hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()}";' in text
    assert "const ENUM_REGISTRY = Object.freeze({" in text
    assert "function requireNewProducerContract(name)" in text
    for name, values in EXPECTED_VALUES.items():
        assert f"const {name} = Object.freeze({{" in text
        assert f"function is{name}(value)" in text
        for value in values:
            assert f'"{value}"' in text

    if shutil.which("node") is None:
        pytest.skip("node unavailable; skipping CommonJS runtime check")
    script = f"""
const contracts = require({json.dumps(str(MINIPROGRAM_GENERATED))});
if (Object.keys(contracts.ENUM_REGISTRY).length !== {len(gen.REQUIRED_ENUMS)}) process.exit(10);
if (!contracts.isCapability("family_shared_memory_approval")) process.exit(11);
if (contracts.isCapability("unknown_capability")) process.exit(12);
if (contracts.isNewProducerContract("RuntimeProfile")) process.exit(13);
if (!contracts.isNewProducerContract("RuntimeProfileV2")) process.exit(14);
try {{ contracts.requireNewProducerContract("PolicyReceipt"); process.exit(15); }} catch (error) {{}}
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_miniprogram_utils_seam_reexports_generated_artifact() -> None:
    """ADR-0033 6: the Mini Program seam must never hand-mirror enums again."""
    seam = REPO_ROOT / "apps/miniprogram/utils/multi-subject-contracts.js"
    text = seam.read_text(encoding="utf-8")
    assert 'require("../generated/multi-subject-contracts")' in text
    assert text.count("module.exports") == 1
    for name, values in EXPECTED_VALUES.items():
        assert f"const {name} =" not in text
        for value in values:
            assert f'"{value}"' not in text


def test_go_artifact_gofmt_and_values() -> None:
    go_path = GENERATED_ROOT / "go/multi_subject_contracts.go"
    text = go_path.read_text(encoding="utf-8")
    for name, values in EXPECTED_VALUES.items():
        assert f"type {name} string" in text
        assert f"func All{name}()" in text
        assert f"func IsValid{name}(" in text
        for value in values:
            assert f'"{value}"' in text
    if shutil.which("gofmt") is None:
        pytest.skip("gofmt unavailable; skipping format check")
    result = subprocess.run(["gofmt", "-l", str(go_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_firmware_compact_json_matches_schema() -> None:
    payload = json.loads((GENERATED_ROOT / "firmware/multi_subject_contracts.compact.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    defs = _schema()["$defs"]
    assert [entry["name"] for entry in payload["enums"]] == list(defs)
    for entry in payload["enums"]:
        meta = defs[entry["name"]]["x-memoria"]
        assert entry["values"] == defs[entry["name"]]["enum"]
        assert entry["missing_value_policy"] == meta["missing_value_policy"]
        assert entry.get("default") == meta.get("default")
