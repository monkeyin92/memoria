#!/usr/bin/env python3
"""Verify the complete, commit-bound release artifact set before activation."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.package_h5_artifact import verify_h5_artifact  # noqa: E402
from scripts.verify_release_source import verify_source_archive  # noqa: E402

_MANIFEST_KEYS = {
    "commit",
    "digest",
    "h5_artifact",
    "images_archive",
    "release_tag",
    "schema_version",
    "source_archive",
}
_PAYLOAD_KEYS = _MANIFEST_KEYS - {"digest"}
_RECORD_KEYS = {"name", "sha256", "size"}
_ROLES = {
    "agent": "memoria-agent",
    "control-api": "memoria-control-api",
    "miniprogram-gateway": "memoria-miniprogram-gateway",
    "speaker-model": "memoria-speaker-model",
}


def _file_record(path: Path, *, name: str) -> dict[str, str | int]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"release input is not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {"name": name, "sha256": digest.hexdigest(), "size": size}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record(value: object, *, label: str) -> dict[str, str | int]:
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS:
        raise ValueError(f"{label} record is invalid")
    name = value.get("name")
    digest = value.get("sha256")
    size = value.get("size")
    if (
        not isinstance(name, str)
        or Path(name).name != name
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise ValueError(f"{label} record is invalid")
    return {"name": name, "sha256": digest, "size": size}


def _labels(config: object) -> dict[str, str]:
    if not isinstance(config, dict):
        raise ValueError("image config is invalid")
    config_section = config.get("config")
    if not isinstance(config_section, dict):
        raise ValueError("image config is invalid")
    labels = config_section.get("Labels")
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        raise ValueError("image labels are invalid")
    return labels


def verify_image_archive(*, archive: Path, expected_commit: str, release_tag: str) -> None:
    """Check the labels stored in a `docker save` tar before import."""
    archive = archive.expanduser().resolve()
    try:
        with tarfile.open(archive, "r:") as image_tar:
            manifest_file = image_tar.extractfile("manifest.json")
            if manifest_file is None:
                raise ValueError("image archive has no manifest")
            manifest = json.load(manifest_file)
            if not isinstance(manifest, list) or len(manifest) != len(_ROLES):
                raise ValueError("image archive must contain every required runtime image")
            roles: set[str] = set()
            for entry in manifest:
                if not isinstance(entry, dict):
                    raise ValueError("image archive manifest is invalid")
                config_name = entry.get("Config")
                repo_tags = entry.get("RepoTags")
                if not isinstance(config_name, str) or not isinstance(repo_tags, list) or len(repo_tags) != 1:
                    raise ValueError("image archive manifest is invalid")
                config_file = image_tar.extractfile(config_name)
                if config_file is None:
                    raise ValueError("image archive config is missing")
                labels = _labels(json.load(config_file))
                role = labels.get("com.memoria.release.role")
                if role not in _ROLES:
                    raise ValueError("image archive role is invalid")
                if labels.get("org.opencontainers.image.revision") != expected_commit:
                    raise ValueError("image archive commit does not match deployment")
                if labels.get("org.opencontainers.image.version") != release_tag:
                    raise ValueError("image archive tag does not match deployment")
                if repo_tags != [f"{_ROLES[role]}:{release_tag}"]:
                    raise ValueError("image archive repository tag does not match deployment")
                roles.add(role)
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise ValueError("image archive is not a valid docker save tar") from exc
    if roles != set(_ROLES):
        raise ValueError("image archive roles do not match deployment")


def verify_imported_images(*, expected_commit: str, release_tag: str) -> None:
    """Repeat label checks after `docker load`; tags alone are never evidence."""
    for role, repository in _ROLES.items():
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", f"{repository}:{release_tag}"],
                check=True,
                capture_output=True,
                text=True,
            )
            inspected = json.loads(result.stdout)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise ValueError(f"imported image is missing or unreadable: {repository}") from exc
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise ValueError(f"imported image is invalid: {repository}")
        config = inspected[0].get("Config") if isinstance(inspected[0], dict) else None
        labels = _labels({"config": config})
        if (
            labels.get("com.memoria.release.role") != role
            or labels.get("org.opencontainers.image.revision") != expected_commit
            or labels.get("org.opencontainers.image.version") != release_tag
        ):
            raise ValueError(f"imported image provenance does not match deployment: {repository}")


def verify_release_manifest(
    *,
    manifest_path: Path,
    artifact_dir: Path,
    expected_tag: str,
    expected_commit: str,
) -> dict[str, object]:
    manifest_path = manifest_path.expanduser().resolve()
    artifact_dir = artifact_dir.expanduser().resolve()
    raw = manifest_path.read_text(encoding="utf-8")
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("release manifest is not valid JSON") from exc
    if raw != _canonical(manifest) + "\n":
        raise ValueError("release manifest is not canonical JSON")
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise ValueError("release manifest shape is invalid")
    if manifest.get("schema_version") != 2:
        raise ValueError("release manifest schema version is unsupported")
    if manifest.get("release_tag") != expected_tag:
        raise ValueError("release manifest tag does not match deployment")
    if manifest.get("commit") != expected_commit:
        raise ValueError("release manifest commit does not match deployment")
    payload = {key: manifest[key] for key in _PAYLOAD_KEYS}
    digest = manifest.get("digest")
    if not isinstance(digest, str) or digest != hashlib.sha256(_canonical(payload).encode()).hexdigest():
        raise ValueError("release manifest digest does not match payload")
    records = {
        label: _record(manifest.get(label), label=label.replace("_", " "))
        for label in ("source_archive", "images_archive", "h5_artifact")
    }
    names = [str(record["name"]) for record in records.values()]
    if len(names) != len(set(names)):
        raise ValueError("release artifact names are not unique")
    paths = {label: artifact_dir / str(record["name"]) for label, record in records.items()}
    for label, path in paths.items():
        if _file_record(path, name=path.name) != records[label]:
            raise ValueError(f"release artifact digest does not match manifest: {path.name}")
    verify_source_archive(archive=paths["source_archive"], expected_commit=expected_commit)
    verify_image_archive(
        archive=paths["images_archive"], expected_commit=expected_commit, release_tag=expected_tag
    )
    verify_h5_artifact(
        artifact=paths["h5_artifact"], expected_commit=expected_commit, release_tag=expected_tag
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--expected-tag", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--verify-imported-images", action="store_true")
    args = parser.parse_args()
    try:
        verify_release_manifest(
            manifest_path=args.manifest,
            artifact_dir=args.artifact_dir,
            expected_tag=args.expected_tag,
            expected_commit=args.expected_commit,
        )
        if args.verify_imported_images:
            verify_imported_images(
                expected_commit=args.expected_commit, release_tag=args.expected_tag
            )
    except (OSError, ValueError) as exc:
        print(f"release manifest rejected: {exc}", file=sys.stderr)
        return 1
    print(f"release_manifest_verified {args.expected_tag} {args.expected_commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
