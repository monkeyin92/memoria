from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from scripts import verify_control_release_artifact as verifier


def _labels() -> dict[str, str]:
    return {
        "org.opencontainers.image.revision": "a" * 40,
        "org.opencontainers.image.version": "control-v1",
        "com.memoria.release.role": "control-api",
        "com.memoria.release.kind": "control-api-source-overlay",
    }


def test_safe_environment_removes_network_and_secret_inputs(tmp_path: Path) -> None:
    database_path = tmp_path / "memoria.sqlite3"
    safe = verifier._safe_environment(
        {
            "PATH": os.environ.get("PATH", ""),
            "MEMORIA_ARCHIVE_DATABASE_URL": "postgresql://secret@prod/memoria",
            "MEMORIA_ARCHIVE_OBJECT_ENDPOINT": "https://prod-object-store.example",
            "MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY": "access-key",
            "DASHSCOPE_API_KEY": "secret",
            "MEMORIA_RELEASE_TAG": "candidate",
        },
        database_path=database_path,
    )
    assert safe["ENVIRONMENT"] == "development"
    assert safe["OFFLINE_MOCK"] == "true"
    assert safe["PYTHONPATH"] == str(Path(verifier.__file__).resolve().parents[1])
    assert safe["MEMORIA_DB_PATH"] == str(database_path.resolve())
    assert safe["MEMORIA_SPEAKER_DB_PATH"] == str((tmp_path / "speakers.sqlite3").resolve())
    assert safe["MEMORIA_ARCHIVE_OBJECT_STORE_PATH"] == str(
        (tmp_path / "archive-objects").resolve()
    )
    assert safe["MEMORIA_VOICE_SAMPLE_STORE_PATH"] == str(
        (tmp_path / "voice-samples").resolve()
    )
    assert "MEMORIA_ARCHIVE_DATABASE_URL" not in safe
    assert "MEMORIA_ARCHIVE_OBJECT_ENDPOINT" not in safe
    assert "MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY" not in safe
    assert "DASHSCOPE_API_KEY" not in safe
    assert safe["MEMORIA_RELEASE_TAG"] == "candidate"


def test_verify_labels_accepts_complete_candidate_metadata() -> None:
    verifier.verify_labels(
        _labels(),
        expected_commit="a" * 40,
        expected_tag="control-v1",
        require_metadata=True,
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("org.opencontainers.image.revision", "short"),
        ("org.opencontainers.image.version", "wrong tag"),
        ("com.memoria.release.role", "agent"),
        ("com.memoria.release.kind", ""),
    ],
)
def test_verify_labels_rejects_incomplete_or_wrong_metadata(key: str, value: str) -> None:
    labels = _labels()
    labels[key] = value
    with pytest.raises(ValueError):
        verifier.verify_labels(
            labels,
            expected_commit="a" * 40,
            expected_tag="control-v1",
            require_metadata=True,
        )


def test_verify_labels_fails_closed_when_required_metadata_is_unavailable() -> None:
    with pytest.raises(ValueError, match="metadata is required"):
        verifier.verify_labels(
            None,
            expected_commit=None,
            expected_tag=None,
            require_metadata=True,
        )


@pytest.mark.parametrize(
    ("expected_commit", "expected_tag"),
    [("short", "control-v1"), ("a" * 40, "bad tag")],
)
def test_verify_labels_rejects_invalid_expected_identity(
    expected_commit: str,
    expected_tag: str,
) -> None:
    with pytest.raises(ValueError, match="expected"):
        verifier.verify_labels(
            _labels(),
            expected_commit=expected_commit,
            expected_tag=expected_tag,
            require_metadata=True,
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("org.opencontainers.image.revision", "b" * 40),
        ("org.opencontainers.image.version", "control-v2"),
        ("com.memoria.release.role", "agent"),
        ("com.memoria.release.kind", "agent-source-overlay"),
    ],
)
def test_verify_labels_rejects_metadata_identity_mismatch(key: str, value: str) -> None:
    labels = _labels()
    labels[key] = value
    with pytest.raises(ValueError, match="candidate"):
        verifier.verify_labels(
            labels,
            expected_commit="a" * 40,
            expected_tag="control-v1",
            require_metadata=True,
        )


def test_image_labels_uses_docker_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    labels = _labels()

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[0] == [
            "docker",
            "image",
            "inspect",
            "memoria-control-api:control-v1",
            "--format",
            "{{json .Config.Labels}}",
        ]
        return subprocess.CompletedProcess(args[0], 0, json.dumps(labels), "")

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    assert verifier._image_labels("memoria-control-api:control-v1") == labels


def test_import_check_runs_with_sanitized_child_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, str] = {}
    temporary_root = tmp_path / "controlled-temp"
    temporary_root.mkdir()

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        assert Path(kwargs["cwd"]).parent == temporary_root  # type: ignore[arg-type]
        return subprocess.CompletedProcess(args[0], 0, "control_api_and_archive_imports=PASS\n", "")

    monkeypatch.setenv("MEMORIA_ARCHIVE_DATABASE_URL", "postgresql://secret@prod/memoria")
    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    verifier._run_import_check(temporary_root=temporary_root)
    assert "MEMORIA_ARCHIVE_DATABASE_URL" not in captured
    assert captured["OFFLINE_MOCK"] == "true"
    database_path = Path(captured["MEMORIA_DB_PATH"])
    assert database_path.name == "memoria.sqlite3"
    assert database_path.parent.parent == temporary_root
    assert not database_path.parent.exists()


def test_import_check_does_not_create_colon_memory_in_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    working_directory = tmp_path / "repository-root"
    temporary_root = tmp_path / "controlled-temp"
    working_directory.mkdir()
    temporary_root.mkdir()
    monkeypatch.chdir(working_directory)
    # Exercise the non-editable-image case: the child must import from the
    # explicit source root even when neither cwd nor an inherited PYTHONPATH
    # points at the checkout.
    monkeypatch.delenv("PYTHONPATH", raising=False)

    verifier._run_import_check(temporary_root=temporary_root)

    assert not (working_directory / ":memory:").exists()
    assert list(temporary_root.iterdir()) == []


def test_import_check_rejects_child_failure_without_leaking_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "child-private-config-value"

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 17, "", f"traceback: {secret}")

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="isolated child") as exc_info:
        verifier._run_import_check(temporary_root=tmp_path)
    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(
    ("offline_mock", "environment", "database_path"),
    [
        ("false", "development", "controlled"),
        ("true", "production", "controlled"),
        ("true", "development", "wrong"),
    ],
)
def test_verify_environment_fails_closed(
    tmp_path: Path,
    offline_mock: str,
    environment: str,
    database_path: str,
) -> None:
    expected = tmp_path / "controlled" / "memoria.sqlite3"
    configured = expected if database_path == "controlled" else tmp_path / "other.sqlite3"
    with pytest.raises(ValueError):
        verifier.verify_environment(
            {
                "OFFLINE_MOCK": offline_mock,
                "ENVIRONMENT": environment,
                "MEMORIA_DB_PATH": str(configured),
            },
            expected_database_path=expected,
        )


def test_main_runs_offline_without_optional_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verifier, "_run_import_check", lambda: None)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    for name in (
        "MEMORIA_RELEASE_COMMIT",
        "MEMORIA_RELEASE_TAG",
        "MEMORIA_RELEASE_ROLE",
        "MEMORIA_RELEASE_KIND",
    ):
        monkeypatch.delenv(name, raising=False)
    assert verifier.main([]) == 0


@pytest.mark.parametrize(
    ("environment", "offline_mock"),
    [("production", "true"), ("development", "false"), ("development", None)],
)
def test_main_rejects_production_or_non_offline_invocation(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    offline_mock: str | None,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", environment)
    if offline_mock is None:
        monkeypatch.delenv("OFFLINE_MOCK", raising=False)
    else:
        monkeypatch.setenv("OFFLINE_MOCK", offline_mock)
    monkeypatch.setattr(
        verifier,
        "_run_import_check",
        lambda: pytest.fail("rejected invocation must not start the child check"),
    )

    assert verifier.main([]) != 0


def test_main_requires_candidate_metadata_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verifier, "_run_import_check", lambda: None)
    for name in (
        "MEMORIA_RELEASE_COMMIT",
        "MEMORIA_RELEASE_TAG",
        "MEMORIA_RELEASE_ROLE",
        "MEMORIA_RELEASE_KIND",
    ):
        monkeypatch.delenv(name, raising=False)
    assert verifier.main(["--require-metadata"]) == 1
