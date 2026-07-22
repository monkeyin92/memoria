#!/usr/bin/env python3
"""Reject production artifacts that are not built from one clean Git commit."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError("release source must be a Git worktree") from exc


def verify(root: Path, *, expected_commit: str, release_tag: str) -> str:
    head = _git(root, "rev-parse", "HEAD")
    if head != expected_commit:
        raise ValueError(f"HEAD {head} does not match expected commit {expected_commit}")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=normal"):
        raise ValueError("release source worktree is not clean")
    if not release_tag.strip():
        raise ValueError("release tag must not be blank")
    try:
        tagged_commit = _git(root, "rev-parse", "--verify", f"refs/tags/{release_tag}^{{}}")
    except ValueError as exc:
        raise ValueError(f"release tag does not exist: {release_tag}") from exc
    if tagged_commit != expected_commit:
        raise ValueError(
            f"release tag {release_tag} points to {tagged_commit}, expected {expected_commit}"
        )
    return head


def create_source_archive(
    *, root: Path, expected_commit: str, release_tag: str, output: Path
) -> None:
    """Archive exactly the reviewed Git tree after verifying its clean tag."""
    root = root.expanduser().resolve()
    output = output.expanduser().resolve()
    verify(root, expected_commit=expected_commit, release_tag=release_tag)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with temporary.open("wb") as stream:
            subprocess.run(
                ["git", "archive", "--format=tar", "--prefix=memoria/", expected_commit],
                cwd=root,
                check=True,
                stdout=stream,
            )
        verify_source_archive(
            archive=temporary,
            expected_commit=expected_commit,
            repository_root=root,
        )
        temporary.replace(output)
    except subprocess.CalledProcessError as exc:
        raise ValueError("could not create release source archive") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_source_archive(
    *,
    archive: Path,
    expected_commit: str,
    repository_root: Path | None = None,
) -> None:
    """Reject a source archive that was not emitted by ``git archive`` for this commit."""
    archive = archive.expanduser().resolve()
    try:
        with archive.open("rb") as stream:
            result = subprocess.run(
                ["git", "get-tar-commit-id"],
                stdin=stream,
                check=True,
                capture_output=True,
                text=True,
            )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("source archive has no Git commit identity") from exc
    if result.stdout.strip() != expected_commit:
        raise ValueError("source archive commit does not match deployment")
    try:
        with tarfile.open(archive, "r:") as source:
            members = [member for member in source.getmembers() if member.type != tarfile.XGLTYPE]
    except (OSError, tarfile.TarError) as exc:
        raise ValueError("source archive is not a valid tar archive") from exc
    names = [member.name for member in members]
    unsafe: list[str] = []
    for member in members:
        path = PurePosixPath(member.name)
        in_root = member.name in {"memoria", "memoria/"} or (
            path.parts and path.parts[0] == "memoria" and len(path.parts) > 1
        )
        if (
            not in_root
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or (not member.isfile() and not member.isdir())
        ):
            unsafe.append(member.name)
    if not members or unsafe or len(names) != len(set(names)):
        raise ValueError("source archive members are unsafe")
    if repository_root is not None:
        root = repository_root.expanduser().resolve()
        try:
            with tempfile.NamedTemporaryFile() as expected:
                subprocess.run(
                    [
                        "git",
                        "archive",
                        "--format=tar",
                        "--prefix=memoria/",
                        expected_commit,
                    ],
                    cwd=root,
                    check=True,
                    stdout=expected,
                )
                expected.flush()
                if _sha256(Path(expected.name)) != _sha256(archive):
                    raise ValueError("source artifact is not the exact Git archive")
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError("could not reproduce the expected Git archive") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--create-archive", type=Path)
    args = parser.parse_args()
    try:
        root = args.root.expanduser().resolve()
        commit = verify(
            root,
            expected_commit=args.expected_commit,
            release_tag=args.release_tag,
        )
        if args.create_archive:
            create_source_archive(
                root=root,
                expected_commit=commit,
                release_tag=args.release_tag,
                output=args.create_archive,
            )
    except ValueError as exc:
        print(f"release source rejected: {exc}", file=sys.stderr)
        return 1
    print(f"release_source_ok {commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
