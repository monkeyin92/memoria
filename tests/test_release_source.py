from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from scripts.create_release_manifest import create_manifest
from scripts.package_release_verifier import package_release_verifier
from scripts.verify_release_manifest import verify_image_archive, verify_release_manifest
from scripts.verify_release_source import create_source_archive, verify_source_archive

ROOT = Path(__file__).parents[1]
CHECK = ROOT / "scripts" / "verify_release_source.py"
MANIFEST = ROOT / "scripts" / "create_release_manifest.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "release-test@example.invalid")
    _git(root, "config", "user.name", "Release Test")
    (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
    release_doc = root / "docs" / "releases" / "release-test.md"
    release_doc.parent.mkdir(parents=True)
    release_doc.write_text("# Release test\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "clean release source")
    commit = _git(root, "rev-parse", "HEAD")
    release_tag = "release-test"
    _git(root, "tag", "-a", release_tag, "-m", "release test")
    return root, commit, release_tag


def _check(root: Path, commit: str, release_tag: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECK),
            "--root",
            str(root),
            "--expected-commit",
            commit,
            "--release-tag",
            release_tag,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_source_requires_clean_expected_commit(tmp_path: Path) -> None:
    root, commit, release_tag = _repo(tmp_path)

    clean = _check(root, commit, release_tag)
    assert clean.returncode == 0
    assert clean.stdout.strip() == f"release_source_ok {commit}"

    (root / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    dirty = _check(root, commit, release_tag)
    assert dirty.returncode != 0
    assert "worktree is not clean" in dirty.stderr

    (root / "untracked.txt").unlink()
    mismatch = _check(root, "0" * 40, release_tag)
    assert mismatch.returncode != 0
    assert "does not match expected commit" in mismatch.stderr


def test_release_source_requires_tag_at_expected_commit(tmp_path: Path) -> None:
    root, commit, release_tag = _repo(tmp_path)

    missing = _check(root, commit, "missing-release")
    assert missing.returncode != 0
    assert "release tag does not exist" in missing.stderr

    (root / "tracked.txt").write_text("next\n", encoding="utf-8")
    _git(root, "commit", "-qam", "next clean source")
    next_commit = _git(root, "rev-parse", "HEAD")
    wrong_target = _check(root, next_commit, release_tag)
    assert wrong_target.returncode != 0
    assert "points to" in wrong_target.stderr


def _image_archive(path: Path, *, commit: str, tag: str, bad_role: str | None = None) -> None:
    entries = []
    with tarfile.open(path, "w") as archive:
        for role, image in (
            ("agent", "memoria-agent"),
            ("control-api", "memoria-control-api"),
            ("device-media-gateway", "memoria-device-media-gateway"),
            ("miniprogram-gateway", "memoria-miniprogram-gateway"),
            ("speaker-model", "memoria-speaker-model"),
        ):
            config_name = f"{role}.json"
            config = json.dumps(
                {
                    "config": {
                        "Labels": {
                            "org.opencontainers.image.revision": commit,
                            "org.opencontainers.image.version": tag,
                            "com.memoria.release.role": bad_role or role,
                        }
                    }
                },
                sort_keys=True,
            ).encode()
            info = tarfile.TarInfo(config_name)
            info.size = len(config)
            archive.addfile(info, io.BytesIO(config))
            entries.append({"Config": config_name, "RepoTags": [f"{image}:{tag}"]})
        manifest = json.dumps(entries, sort_keys=True).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))


def test_release_source_archive_is_deterministic_and_commit_bound(tmp_path: Path) -> None:
    root, commit, release_tag = _repo(tmp_path)
    first = tmp_path / "source-1.tar"
    second = tmp_path / "source-2.tar"

    create_source_archive(root=root, expected_commit=commit, release_tag=release_tag, output=first)
    create_source_archive(root=root, expected_commit=commit, release_tag=release_tag, output=second)

    assert first.read_bytes() == second.read_bytes()
    verify_source_archive(archive=first, expected_commit=commit, repository_root=root)
    with pytest.raises(ValueError, match="commit does not match"):
        verify_source_archive(archive=first, expected_commit="b" * 40)

    with tarfile.open(first, "a") as archive:
        injected = b"not tracked by the reviewed commit"
        info = tarfile.TarInfo("memoria/injected.txt")
        info.size = len(injected)
        archive.addfile(info, io.BytesIO(injected))
    with pytest.raises(ValueError, match="exact Git archive"):
        verify_source_archive(
            archive=first,
            expected_commit=commit,
            repository_root=root,
        )


def test_release_verifier_zipapp_is_deterministic_and_runs_from_reviewed_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "verifier-repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "package_h5_artifact.py",
        "verify_release_manifest.py",
        "verify_release_source.py",
    ):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "release-test@example.invalid")
    _git(root, "config", "user.name", "Release Test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "reviewed verifier")
    commit = _git(root, "rev-parse", "HEAD")
    tag = "release-verifier-test"
    _git(root, "tag", "-a", tag, "-m", tag)
    first = tmp_path / "first.pyz"
    second = tmp_path / "second.pyz"

    package_release_verifier(
        root=root,
        expected_commit=commit,
        release_tag=tag,
        output=first,
    )
    package_release_verifier(
        root=root,
        expected_commit=commit,
        release_tag=tag,
        output=second,
    )

    assert first.read_bytes() == second.read_bytes()
    completed = subprocess.run(
        [sys.executable, str(first), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "--manifest" in completed.stdout


def test_release_manifest_is_canonical_and_binds_source_images_and_h5(
    tmp_path: Path,
) -> None:
    root, commit, release_tag = _repo(tmp_path)
    source = tmp_path / "source.tar"
    create_source_archive(root=root, expected_commit=commit, release_tag=release_tag, output=source)
    artifact = tmp_path / "images.tar"
    _image_archive(artifact, commit=commit, tag=release_tag)
    h5_artifact = tmp_path / "h5-dist.tar.gz"
    h5_source = tmp_path / "dist"
    h5_source.mkdir()
    (h5_source / "index.html").write_text("ok", encoding="utf-8")
    from scripts.package_h5_artifact import package_h5_artifact

    package_h5_artifact(
        source=h5_source,
        output=h5_artifact,
        expected_commit=commit,
        release_tag=release_tag,
    )
    output = tmp_path / "release-manifest.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(MANIFEST),
            "--root",
            str(root),
            "--release-tag",
            release_tag,
            "--expected-commit",
            commit,
            "--source-archive",
            str(source),
            "--images-archive",
            str(artifact),
            "--h5-artifact",
            str(h5_artifact),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    manifest = json.loads(output.read_text(encoding="utf-8"))

    assert output.read_text(encoding="utf-8") == (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    )
    assert manifest["release_tag"] == release_tag
    assert manifest["commit"] == commit
    assert manifest["source_archive"] == {
        "name": source.name,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "size": source.stat().st_size,
    }
    assert manifest["images_archive"]["name"] == artifact.name
    assert manifest["h5_artifact"]["name"] == h5_artifact.name
    assert len(manifest["digest"]) == 64


def test_release_manifest_rejects_missing_artifact_and_wrong_tag(tmp_path: Path) -> None:
    root, commit, release_tag = _repo(tmp_path)
    source = tmp_path / "source.tar"
    create_source_archive(root=root, expected_commit=commit, release_tag=release_tag, output=source)
    h5 = tmp_path / "h5-dist.tar.gz"
    h5.write_bytes(b"not an H5 artifact")
    output = tmp_path / "release-manifest.json"

    with pytest.raises(ValueError, match="not a regular file"):
        create_manifest(
            root=root,
            release_tag=release_tag,
            expected_commit=commit,
            source_archive=source,
            images_archive=tmp_path / "missing.tar",
            h5_artifact=h5,
            output=output,
        )
    with pytest.raises(ValueError, match="release tag does not exist"):
        create_manifest(
            root=root,
            release_tag="wrong-release",
            expected_commit=commit,
            source_archive=source,
            images_archive=source,
            h5_artifact=h5,
            output=output,
        )


def test_release_manifest_verifier_binds_expected_source_and_complete_artifact_set(
    tmp_path: Path,
) -> None:
    root, commit, release_tag = _repo(tmp_path)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    source = artifact_dir / "source.tar"
    create_source_archive(root=root, expected_commit=commit, release_tag=release_tag, output=source)
    images = artifact_dir / "images.tar"
    _image_archive(images, commit=commit, tag=release_tag)
    h5_source = tmp_path / "dist"
    h5_source.mkdir()
    (h5_source / "index.html").write_text("ok", encoding="utf-8")
    h5 = artifact_dir / "h5-dist.tar.gz"
    from scripts.package_h5_artifact import package_h5_artifact

    package_h5_artifact(
        source=h5_source, output=h5, expected_commit=commit, release_tag=release_tag
    )
    manifest_path = artifact_dir / "release-manifest.json"
    create_manifest(
        root=root,
        release_tag=release_tag,
        expected_commit=commit,
        source_archive=source,
        images_archive=images,
        h5_artifact=h5,
        output=manifest_path,
    )

    verified = verify_release_manifest(
        manifest_path=manifest_path,
        artifact_dir=artifact_dir,
        expected_tag=release_tag,
        expected_commit=commit,
    )

    assert verified["commit"] == commit


def test_release_manifest_verifier_rejects_tampering_and_wrong_pairing(tmp_path: Path) -> None:
    root, commit, release_tag = _repo(tmp_path)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    source = artifact_dir / "source.tar"
    create_source_archive(root=root, expected_commit=commit, release_tag=release_tag, output=source)
    images = artifact_dir / "images.tar"
    _image_archive(images, commit=commit, tag=release_tag)
    h5_source = tmp_path / "dist"
    h5_source.mkdir()
    (h5_source / "index.html").write_text("ok", encoding="utf-8")
    h5 = artifact_dir / "h5-dist.tar.gz"
    from scripts.package_h5_artifact import package_h5_artifact

    package_h5_artifact(
        source=h5_source, output=h5, expected_commit=commit, release_tag=release_tag
    )
    manifest_path = artifact_dir / "release-manifest.json"
    create_manifest(
        root=root,
        release_tag=release_tag,
        expected_commit=commit,
        source_archive=source,
        images_archive=images,
        h5_artifact=h5,
        output=manifest_path,
    )
    kwargs = {
        "manifest_path": manifest_path,
        "artifact_dir": artifact_dir,
        "expected_tag": release_tag,
        "expected_commit": commit,
    }

    with pytest.raises(ValueError, match="tag does not match"):
        verify_release_manifest(**{**kwargs, "expected_tag": "wrong-tag"})

    images.write_bytes(b"tampered image archive")
    with pytest.raises(ValueError, match="artifact digest"):
        verify_release_manifest(**kwargs)

    _image_archive(images, commit=commit, tag=release_tag)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="canonical JSON"):
        verify_release_manifest(**kwargs)


def test_image_archive_rejects_a_retagged_or_wrong_role_image(tmp_path: Path) -> None:
    archive = tmp_path / "images.tar"
    _image_archive(archive, commit="a" * 40, tag="release-test", bad_role="agent")

    with pytest.raises(ValueError, match="repository tag|roles"):
        verify_image_archive(archive=archive, expected_commit="a" * 40, release_tag="release-test")


def test_manifest_creation_rejects_a_stale_h5_even_if_its_digest_is_recomputed(
    tmp_path: Path,
) -> None:
    root, commit, release_tag = _repo(tmp_path)
    source = tmp_path / "source.tar"
    create_source_archive(root=root, expected_commit=commit, release_tag=release_tag, output=source)
    images = tmp_path / "images.tar"
    _image_archive(images, commit=commit, tag=release_tag)
    h5_source = tmp_path / "dist"
    h5_source.mkdir()
    (h5_source / "index.html").write_text("old release", encoding="utf-8")
    h5 = tmp_path / "h5-dist.tar.gz"
    from scripts.package_h5_artifact import package_h5_artifact

    package_h5_artifact(
        source=h5_source,
        output=h5,
        expected_commit=commit,
        release_tag="old-release",
    )

    with pytest.raises(ValueError, match="commit does not match"):
        create_manifest(
            root=root,
            release_tag=release_tag,
            expected_commit=commit,
            source_archive=source,
            images_archive=images,
            h5_artifact=h5,
            output=tmp_path / "release-manifest.json",
        )


def test_production_runbook_verifies_manifest_and_portable_sidecars() -> None:
    runbook = (ROOT / "HANDOFF.md").read_text(encoding="utf-8")

    assert "scripts/package_release_verifier.py" in runbook
    assert 'python3 "$UPLOAD_DIR/release-verifier.pyz"' in runbook
    assert "--source-archive" in runbook
    assert "--images-archive" in runbook
    assert "--h5-artifact" in runbook
    assert "--verify-imported-images" in runbook
    assert 'docker tag "memoria-speaker-model' not in runbook
    assert 'cd "$ARTIFACT_DIR" && sha256sum "$artifact"' in runbook
    assert 'sha256sum "$ARTIFACT_DIR/$artifact"' not in runbook
    trusted_hash = runbook.index("MEMORIA_RELEASE_VERIFIER_SHA256")
    trusted_manifest = runbook.index("MEMORIA_RELEASE_MANIFEST_SHA256")
    trusted_verify = runbook.index('python3 "$UPLOAD_DIR/release-verifier.pyz"')
    extraction = runbook.index('tar --extract --file "$UPLOAD_DIR/source.tar"')
    assert trusted_hash < trusted_verify < extraction
    assert trusted_manifest < trusted_verify
    assert "新 H5 切流并验收后立即" not in runbook
    assert "原定窗口保留到绝对截止" in runbook
