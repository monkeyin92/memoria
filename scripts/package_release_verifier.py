#!/usr/bin/env python3
"""Package a deterministic, commit-reviewed release verifier zipapp."""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_release_source import verify  # noqa: E402

_VERIFIER_SOURCES = (
    "scripts/package_h5_artifact.py",
    "scripts/verify_release_manifest.py",
    "scripts/verify_release_source.py",
)
_MAIN = b"from scripts.verify_release_manifest import main\nraise SystemExit(main())\n"


def _git_blob(root: Path, commit: str, name: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "show", f"{commit}:{name}"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"release verifier source is missing from commit: {name}") from exc


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def package_release_verifier(
    *,
    root: Path,
    expected_commit: str,
    release_tag: str,
    output: Path,
) -> None:
    root = root.expanduser().resolve()
    output = output.expanduser().resolve()
    commit = verify(root, expected_commit=expected_commit, release_tag=release_tag)
    if output == root or output.is_relative_to(root):
        raise ValueError("release verifier output must be outside the release source")
    entries = {
        "__main__.py": _MAIN,
        "scripts/__init__.py": b"",
        **{name: _git_blob(root, commit, name) for name in _VERIFIER_SOURCES},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, content in sorted(entries.items()):
                archive.writestr(_zip_info(name), content)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        package_release_verifier(
            root=args.root,
            expected_commit=args.expected_commit,
            release_tag=args.release_tag,
            output=args.output,
        )
    except ValueError as exc:
        print(f"release verifier rejected: {exc}", file=sys.stderr)
        return 1
    print(f"release_verifier_ok {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
