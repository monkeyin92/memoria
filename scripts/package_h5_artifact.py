#!/usr/bin/env python3
"""Build one deterministic archive from the reviewed H5 distribution."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import tarfile
from pathlib import Path, PurePosixPath

_PROVENANCE_NAME = "memoria-release.json"


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
        and path.as_posix() == name
    )


def _provenance(*, expected_commit: str, release_tag: str) -> bytes:
    if len(expected_commit) != 40 or any(
        character not in "0123456789abcdef" for character in expected_commit
    ):
        raise ValueError("H5 artifact commit must be a 40-character lowercase Git commit")
    if not release_tag.strip():
        raise ValueError("H5 artifact release tag must not be blank")
    return (
        json.dumps(
            {
                "commit": expected_commit,
                "release_tag": release_tag,
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _regular_files(source: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        if path.is_symlink():
            raise ValueError(f"H5 artifact source must not contain a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"H5 artifact source contains a non-regular file: {path}")
        files.append(path)
    if not files:
        raise ValueError("H5 artifact source contains no regular files")
    return files


def package_h5_artifact(
    *, source: Path, output: Path, expected_commit: str, release_tag: str
) -> None:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"H5 artifact source is not a directory: {source}")
    if output == source or output.is_relative_to(source):
        raise ValueError("H5 artifact output must be outside the H5 source")
    files = _regular_files(source)
    if any(path.relative_to(source).as_posix() == _PROVENANCE_NAME for path in files):
        raise ValueError(f"H5 artifact source must not contain {_PROVENANCE_NAME}")
    provenance = _provenance(expected_commit=expected_commit, release_tag=release_tag)
    members: list[tuple[str, Path | None]] = [
        (path.relative_to(source).as_posix(), path) for path in files
    ]
    members.append((_PROVENANCE_NAME, None))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.USTAR_FORMAT,
                ) as archive:
                    for relative, path in sorted(members, key=lambda member: member[0]):
                        info = tarfile.TarInfo(name=relative)
                        info.size = len(provenance) if path is None else path.stat().st_size
                        info.mode = 0o644
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = 0
                        if path is None:
                            archive.addfile(info, io.BytesIO(provenance))
                        else:
                            with path.open("rb") as stream:
                                archive.addfile(info, stream)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def verify_h5_artifact(*, artifact: Path, expected_commit: str, release_tag: str) -> None:
    artifact = artifact.expanduser().resolve()
    expected = _provenance(expected_commit=expected_commit, release_tag=release_tag)
    try:
        with tarfile.open(artifact, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)) or any(
                not _safe_member_name(name) for name in names
            ):
                raise ValueError("H5 artifact contains an unsafe or duplicate member")
            if (
                names != sorted(names)
                or names.count(_PROVENANCE_NAME) != 1
            ):
                raise ValueError("H5 artifact provenance is missing or non-canonical")
            if any(
                not member.isfile()
                or member.mtime != 0
                or member.uid != 0
                or member.gid != 0
                or member.mode != 0o644
                for member in members
            ):
                raise ValueError("H5 artifact members are not normalized regular files")
            provenance_file = archive.extractfile(_PROVENANCE_NAME)
            if provenance_file is None or provenance_file.read() != expected:
                raise ValueError("H5 artifact commit does not match deployment")
    except (OSError, tarfile.TarError) as exc:
        raise ValueError("H5 artifact is not a valid gzip tar archive") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--release-tag", required=True)
    args = parser.parse_args()
    try:
        package_h5_artifact(
            source=args.source,
            output=args.output,
            expected_commit=args.expected_commit,
            release_tag=args.release_tag,
        )
    except ValueError as exc:
        print(f"H5 artifact rejected: {exc}", file=sys.stderr)
        return 1
    print(f"h5_artifact_ok {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
