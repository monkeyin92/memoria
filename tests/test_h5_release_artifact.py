from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from scripts.package_h5_artifact import package_h5_artifact, verify_h5_artifact


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_h5_artifact_is_deterministic_and_contains_only_normalized_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dist"
    (source / "assets").mkdir(parents=True)
    (source / "worklets").mkdir()
    (source / "index.html").write_text("<main>Memoria</main>\n", encoding="utf-8")
    (source / "assets" / "app.js").write_text("console.log('ok')\n", encoding="utf-8")
    (source / "worklets" / "counter.js").write_text("registerProcessor()\n", encoding="utf-8")
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    package_h5_artifact(
        source=source,
        output=first,
        expected_commit="a" * 40,
        release_tag="release-test",
    )
    (source / "index.html").touch()
    package_h5_artifact(
        source=source,
        output=second,
        expected_commit="a" * 40,
        release_tag="release-test",
    )

    assert _sha256(first) == _sha256(second)
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [
            "assets/app.js",
            "index.html",
            "memoria-release.json",
            "worklets/counter.js",
        ]
        assert all(member.isfile() for member in members)
        assert all(member.mtime == 0 for member in members)
        assert all(member.uid == member.gid == 0 for member in members)
        assert all(member.uname == member.gname == "" for member in members)
        assert all(member.mode == 0o644 for member in members)
        assert archive.extractfile("index.html").read() == b"<main>Memoria</main>\n"
        assert json.loads(archive.extractfile("memoria-release.json").read()) == {
            "commit": "a" * 40,
            "release_tag": "release-test",
            "schema_version": 1,
        }
    assert int.from_bytes(first.read_bytes()[4:8], byteorder="little") == 0
    verify_h5_artifact(
        artifact=first,
        expected_commit="a" * 40,
        release_tag="release-test",
    )
    with pytest.raises(ValueError, match="commit does not match"):
        verify_h5_artifact(
            artifact=first,
            expected_commit="b" * 40,
            release_tag="release-test",
        )


def test_h5_artifact_rejects_empty_source_symlinks_and_output_inside_source(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no regular files"):
        package_h5_artifact(
            source=empty,
            output=tmp_path / "empty.tar.gz",
            expected_commit="a" * 40,
            release_tag="release-test",
        )

    source = tmp_path / "dist"
    source.mkdir()
    (source / "index.html").write_text("ok", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the H5 source"):
        package_h5_artifact(
            source=source,
            output=source / "release.tar.gz",
            expected_commit="a" * 40,
            release_tag="release-test",
        )

    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    (source / "link.txt").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        package_h5_artifact(
            source=source,
            output=tmp_path / "linked.tar.gz",
            expected_commit="a" * 40,
            release_tag="release-test",
        )


def test_h5_artifact_verifier_rejects_unsafe_or_duplicate_member_names(
    tmp_path: Path,
) -> None:
    provenance = (
        json.dumps(
            {
                "commit": "a" * 40,
                "release_tag": "release-test",
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()

    for label, names in {
        "unsafe": ["../escape", "memoria-release.json"],
        "duplicate": ["index.html", "index.html", "memoria-release.json"],
    }.items():
        artifact = tmp_path / f"{label}.tar.gz"
        with tarfile.open(artifact, "w:gz") as archive:
            for name in names:
                content = provenance if name == "memoria-release.json" else b"safe"
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                info.uid = 0
                info.gid = 0
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))

        with pytest.raises(ValueError, match="unsafe|duplicate"):
            verify_h5_artifact(
                artifact=artifact,
                expected_commit="a" * 40,
                release_tag="release-test",
            )
