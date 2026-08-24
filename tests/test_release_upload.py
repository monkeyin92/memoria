from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "upload_release_artifacts.sh"
ARTIFACTS = (
    "source.tar",
    "source.tar.sha256",
    "images.tar",
    "images.tar.sha256",
    "h5-dist.tar.gz",
    "h5-dist.tar.gz.sha256",
    "release-manifest.json",
    "release-verifier.pyz",
)


def _artifacts(tmp_path: Path) -> Path:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    for name in ("source.tar", "images.tar", "h5-dist.tar.gz"):
        payload = f"artifact:{name}\n".encode()
        (artifact_dir / name).write_bytes(payload)
        (artifact_dir / f"{name}.sha256").write_text(
            f"{hashlib.sha256(payload).hexdigest()}  {name}\n",
            encoding="utf-8",
        )
    (artifact_dir / "release-manifest.json").write_text("{}\n", encoding="utf-8")
    (artifact_dir / "release-verifier.pyz").write_bytes(b"verifier\n")
    (artifact_dir / "must-not-upload.secret").write_text("secret\n", encoding="utf-8")
    return artifact_dir


def _fake_commands(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'ssh %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
        "count_file=\"$COMMAND_LOG.ssh-count\"\n"
        "count=0\n"
        "[[ ! -f \"$count_file\" ]] || count=\"$(cat \"$count_file\")\"\n"
        "printf '%s\\n' \"$((count + 1))\" >\"$count_file\"\n"
        "[[ \"$count\" != 0 ]] || printf '%s\\n' \"${FAKE_UPLOAD_MODE:-seeded}\"\n",
        encoding="utf-8",
    )
    rsync = bin_dir / "rsync"
    rsync.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'rsync %s\\n' \"$*\" >>\"$COMMAND_LOG\"\n"
        "printf 'Literal data: 15,790,150 bytes\\nMatched data: 2,364,486,267 bytes\\n'\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    rsync.chmod(0o755)
    return bin_dir, log


def test_release_upload_uses_a_remote_seed_and_only_the_allowlisted_artifacts(
    tmp_path: Path,
) -> None:
    artifact_dir = _artifacts(tmp_path)
    bin_dir, log = _fake_commands(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
    }

    completed = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--artifact-dir",
            str(artifact_dir),
            "--remote",
            "memoria-prod",
            "--release-tag",
            "20260731-120000",
            "--base-tag",
            "20260730-092236",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert "release_upload_mode=seeded" in completed.stdout
    assert "Literal data: 15,790,150 bytes" in completed.stdout
    commands = log.read_text(encoding="utf-8")
    assert "--checksum" in commands
    assert "--partial" in commands
    assert "--rsync-path=sudo -n rsync" in commands
    assert "/opt/memoria/incoming/20260731-120000/" in commands
    assert all(str(artifact_dir / name) in commands for name in ARTIFACTS)
    assert "must-not-upload.secret" not in commands


def test_release_upload_dry_run_has_no_remote_side_effect(tmp_path: Path) -> None:
    artifact_dir = _artifacts(tmp_path)
    bin_dir, log = _fake_commands(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
    }

    completed = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--artifact-dir",
            str(artifact_dir),
            "--remote",
            "memoria-prod",
            "--release-tag",
            "20260731-120000",
            "--base-tag",
            "20260730-092236",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert "release_upload_dry_run=PASS" in completed.stdout
    assert not log.exists()


def test_release_upload_rejects_missing_artifacts_before_connecting(tmp_path: Path) -> None:
    artifact_dir = _artifacts(tmp_path)
    (artifact_dir / "images.tar").unlink()
    bin_dir, log = _fake_commands(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
    }

    completed = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--artifact-dir",
            str(artifact_dir),
            "--remote",
            "memoria-prod",
            "--release-tag",
            "20260731-120000",
            "--base-tag",
            "20260730-092236",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert completed.returncode != 0
    assert "missing release artifact: images.tar" in completed.stderr
    assert not log.exists()


def test_release_upload_rejects_an_invalid_remote_seed_mode(tmp_path: Path) -> None:
    artifact_dir = _artifacts(tmp_path)
    bin_dir, log = _fake_commands(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
        "FAKE_UPLOAD_MODE": "seeded;unsafe",
    }

    completed = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--artifact-dir",
            str(artifact_dir),
            "--remote",
            "memoria-prod",
            "--release-tag",
            "20260731-120000",
            "--base-tag",
            "20260730-092236",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert completed.returncode != 0
    assert "invalid remote upload mode" in completed.stderr
    assert "rsync " not in log.read_text(encoding="utf-8")


def test_production_runbook_uses_the_seeded_uploader_before_server_verification() -> None:
    runbook = (ROOT / "HANDOFF.md").read_text(encoding="utf-8")

    upload = runbook.index("scripts/upload_release_artifacts.sh")
    verify = runbook.index('python3 "$UPLOAD_DIR/release-verifier.pyz"')
    assert upload < verify
    assert "--dry-run" in runbook
    assert "--base-tag" in runbook
    assert "不要对 basis 使用 `rsync --inplace`" in runbook
    assert "images.tar + images.tar.sha256" in runbook
