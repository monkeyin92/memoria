#!/usr/bin/env python3
"""Offline integrity checks for a Control API source-overlay candidate."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

EXPECTED_ROLE = "control-api"
EXPECTED_KIND = "control-api-source-overlay"
REQUIRED_IMPORTS = (
    "services.control_api.app.main",
    "services.archive.domain",
    "services.archive.life_archive",
    "services.archive.memory_catalog",
    "services.archive.postgres_archive",
)
SAFE_ENV_DEFAULTS = {
    "ENVIRONMENT": "development",
    "OFFLINE_MOCK": "true",
}
_TRUTHY_OFFLINE_VALUES = {"1", "true", "yes", "on"}
_SENSITIVE_ENV_FRAGMENTS = (
    "ACCESS_KEY",
    "API_KEY",
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "DATABASE",
    "DB_",
    "DSN",
    "ENDPOINT",
    "HOST",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
    "URL",
)
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_environment(
    environ: Mapping[str, str],
    *,
    database_path: Path,
) -> dict[str, str]:
    """Return a network-free child environment without echoing any values."""

    child = dict(environ)
    for key in tuple(child):
        upper = key.upper()
        if any(marker in upper for marker in _SENSITIVE_ENV_FRAGMENTS):
            child.pop(key, None)
    child.update(SAFE_ENV_DEFAULTS)
    # The verifier intentionally runs from a fresh temporary cwd.  Do not rely
    # on an editable install (or a parent process' cwd) to make ``scripts``
    # importable in the child; the source tree is /app in the shipped image.
    child["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    child["MEMORIA_DB_PATH"] = str(database_path.resolve())
    child["MEMORIA_SPEAKER_DB_PATH"] = str(
        database_path.with_name("speakers.sqlite3").resolve()
    )
    child["MEMORIA_ARCHIVE_OBJECT_STORE_PATH"] = str(
        database_path.with_name("archive-objects").resolve()
    )
    child["MEMORIA_VOICE_SAMPLE_STORE_PATH"] = str(
        database_path.with_name("voice-samples").resolve()
    )
    return child


def _check_imports() -> None:
    for module in REQUIRED_IMPORTS:
        importlib.import_module(module)
    main = importlib.import_module("services.control_api.app.main")
    app = getattr(main, "app", None)
    if app is None or not callable(getattr(app, "openapi", None)):
        raise ValueError("Control API app was not constructed")
    print("control_api_and_archive_imports=PASS")


def _run_import_check(
    environ: Mapping[str, str] | None = None,
    *,
    temporary_root: Path | None = None,
) -> None:
    code = (
        "from scripts.verify_control_release_artifact import _check_imports; "
        "_check_imports()"
    )
    source_environment = os.environ if environ is None else environ
    with tempfile.TemporaryDirectory(
        prefix="memoria-control-release-",
        dir=temporary_root,
    ) as temporary_directory:
        database_path = Path(temporary_directory) / "memoria.sqlite3"
        child_environment = _safe_environment(
            source_environment,
            database_path=database_path,
        )
        verify_environment(
            child_environment,
            expected_database_path=database_path,
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            env=child_environment,
            cwd=temporary_directory,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    if completed.returncode != 0:
        # Tracebacks contain module names but can also include configuration
        # values. Keep the public failure deliberately value-free.
        raise ValueError("Control API/archive import smoke failed in the isolated child")
    print(completed.stdout.strip())


def _image_labels(image: str) -> dict[str, str]:
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        labels = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError("candidate image labels are unavailable") from exc
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        raise ValueError("candidate image labels are invalid")
    return labels


def _visible_labels(environ: Mapping[str, str]) -> dict[str, str] | None:
    values = {
        "org.opencontainers.image.revision": environ.get("MEMORIA_RELEASE_COMMIT", ""),
        "org.opencontainers.image.version": environ.get("MEMORIA_RELEASE_TAG", ""),
        "com.memoria.release.role": environ.get("MEMORIA_RELEASE_ROLE", ""),
        "com.memoria.release.kind": environ.get("MEMORIA_RELEASE_KIND", ""),
    }
    return values if any(values.values()) else None


def verify_labels(
    labels: Mapping[str, str] | None,
    *,
    expected_commit: str | None,
    expected_tag: str | None,
    require_metadata: bool,
) -> None:
    if expected_commit is not None and not _COMMIT_RE.fullmatch(expected_commit):
        raise ValueError("expected commit is invalid")
    if expected_tag is not None and not _TAG_RE.fullmatch(expected_tag):
        raise ValueError("expected tag is invalid")
    if labels is None:
        if require_metadata or expected_commit or expected_tag:
            raise ValueError("candidate metadata is required but unavailable")
        print("candidate_metadata=NOT_VISIBLE")
        return
    revision = labels.get("org.opencontainers.image.revision", "")
    version = labels.get("org.opencontainers.image.version", "")
    role = labels.get("com.memoria.release.role", "")
    kind = labels.get("com.memoria.release.kind", "")
    if not _COMMIT_RE.fullmatch(revision) or not _TAG_RE.fullmatch(version):
        raise ValueError("candidate revision/version labels are missing or invalid")
    if role != EXPECTED_ROLE or kind != EXPECTED_KIND:
        raise ValueError("candidate role/kind labels do not describe a Control API overlay")
    if expected_commit and revision != expected_commit:
        raise ValueError("candidate revision label does not match expected commit")
    if expected_tag and version != expected_tag:
        raise ValueError("candidate version label does not match expected tag")
    print("candidate_metadata=PASS")


def verify_environment(
    environ: Mapping[str, str],
    *,
    expected_database_path: Path,
) -> None:
    if environ.get("OFFLINE_MOCK", "").strip().lower() not in _TRUTHY_OFFLINE_VALUES:
        raise ValueError("offline artifact verification requires OFFLINE_MOCK=true")
    if environ.get("ENVIRONMENT", "").strip().lower() == "production":
        raise ValueError("offline artifact verification must not run in production mode")
    configured_database_path = Path(environ.get("MEMORIA_DB_PATH", "").strip())
    if (
        not configured_database_path.is_absolute()
        or configured_database_path != expected_database_path.resolve()
    ):
        raise ValueError("offline artifact verification requires its controlled database path")
    print("offline_environment=PASS")


def verify_invocation_environment(environ: Mapping[str, str]) -> None:
    """Reject production or non-offline settings before starting any checks."""

    if environ.get("OFFLINE_MOCK", "").strip().lower() not in _TRUTHY_OFFLINE_VALUES:
        raise ValueError("control release verification requires OFFLINE_MOCK=true")
    if environ.get("ENVIRONMENT", "").strip().lower() == "production":
        raise ValueError("control release verification must not run in production mode")


def verify_schema_boundary() -> None:
    """Assert that schema work is not part of offline artifact verification."""

    print("authoritative_schema_verification=DEFERRED_TO_PRE_CUTOVER_GATE")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Inspect OCI labels on a local candidate image")
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-tag")
    parser.add_argument(
        "--require-metadata",
        action="store_true",
        help="Fail if neither --image nor visible MEMORIA_RELEASE_* metadata is available",
    )
    args = parser.parse_args(argv)
    try:
        verify_invocation_environment(os.environ)
        labels = _image_labels(args.image) if args.image else _visible_labels(os.environ)
        verify_labels(
            labels,
            expected_commit=args.expected_commit,
            expected_tag=args.expected_tag,
            require_metadata=args.require_metadata,
        )
        verify_schema_boundary()
        _run_import_check()
    except (ValueError, subprocess.TimeoutExpired) as exc:
        print(f"control release artifact rejected: {exc}", file=sys.stderr)
        return 1
    print("control release artifact verification PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
