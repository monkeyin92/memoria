#!/usr/bin/env python3
"""Create a canonical manifest for one commit-bound release artifact set."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.package_h5_artifact import verify_h5_artifact  # noqa: E402
from scripts.verify_release_manifest import verify_image_archive  # noqa: E402
from scripts.verify_release_source import verify, verify_source_archive  # noqa: E402

_PAYLOAD_KEYS = {
    "commit",
    "h5_artifact",
    "images_archive",
    "release_tag",
    "schema_version",
    "source_archive",
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


def _record(path: Path) -> dict[str, str | int]:
    path = path.expanduser().resolve()
    return _file_record(path, name=path.name)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_manifest(
    *,
    root: Path,
    release_tag: str,
    expected_commit: str,
    source_archive: Path,
    images_archive: Path,
    h5_artifact: Path,
    output: Path,
) -> dict[str, object]:
    root = root.expanduser().resolve()
    commit = verify(root, expected_commit=expected_commit, release_tag=release_tag)
    source_archive = source_archive.expanduser().resolve()
    images_archive = images_archive.expanduser().resolve()
    h5_artifact = h5_artifact.expanduser().resolve()
    records = {
        "source_archive": _record(source_archive),
        "images_archive": _record(images_archive),
        "h5_artifact": _record(h5_artifact),
    }
    verify_source_archive(
        archive=source_archive,
        expected_commit=commit,
        repository_root=root,
    )
    verify_image_archive(archive=images_archive, expected_commit=commit, release_tag=release_tag)
    verify_h5_artifact(artifact=h5_artifact, expected_commit=commit, release_tag=release_tag)
    payload: dict[str, object] = {
        "commit": commit,
        "h5_artifact": records["h5_artifact"],
        "images_archive": records["images_archive"],
        "release_tag": release_tag,
        "schema_version": 2,
        "source_archive": records["source_archive"],
    }
    digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
    manifest = {**payload, "digest": digest}
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_canonical(manifest) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--source-archive", required=True, type=Path)
    parser.add_argument("--images-archive", required=True, type=Path)
    parser.add_argument("--h5-artifact", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        create_manifest(
            root=args.root,
            release_tag=args.release_tag,
            expected_commit=args.expected_commit,
            source_archive=args.source_archive,
            images_archive=args.images_archive,
            h5_artifact=args.h5_artifact,
            output=args.output,
        )
    except ValueError as exc:
        print(f"release manifest rejected: {exc}", file=sys.stderr)
        return 1
    print(f"release_manifest_ok {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
