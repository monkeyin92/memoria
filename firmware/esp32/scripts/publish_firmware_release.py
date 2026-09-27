#!/usr/bin/env python3
"""Sign and publish a Memoria firmware image for over-the-air updates.

    uv run python firmware/esp32/scripts/publish_firmware_release.py sign
    uv run python firmware/esp32/scripts/publish_firmware_release.py upload --remote memoria-prod --build N
    uv run python firmware/esp32/scripts/publish_firmware_release.py withdraw --remote memoria-prod

``sign`` takes the built application image (``artifacts/memoria-esp-vocat-app.bin``),
checks that it is a Memoria image whose embedded ``MEMORIA_FIRMWARE_BUILD``
marker matches ``memoria_firmware_release.h``, signs the release document with
the firmware release key and verifies the result against the public key that
the firmware and Control API embed. It writes
``outputs/firmware-releases/<board>/<build>/{app.bin,release.json}``.

``upload`` copies that directory to the server's release root (the host side
of control-api's ``/data`` bind, read as ``/data/firmware-releases``), re-checks the
image hash there, and only then switches ``current.json`` atomically. Devices
pick it up at their next idle check. ``withdraw`` removes ``current.json`` so
nothing is offered; devices that already installed a build keep it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
FIRMWARE_ROOT = HERE.parents[1]
REPO_ROOT = HERE.parents[3]
sys.path.insert(0, str(REPO_ROOT))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import load_pem_private_key  # noqa: E402
from services.control_api.app.device_firmware import (  # noqa: E402
    FIRMWARE_BOARD,
    MAX_IMAGE_BYTES,
    RELEASE_PUBLIC_KEY,
    RELEASE_SCHEMA_VERSION,
    canonical_release_payload,
    validate_release,
)
from services.device_fleet.bootstrap_domain import b64url_encode  # noqa: E402

RELEASE_HEADER = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria" / "memoria_firmware_release.h"
DEFAULT_IMAGE = FIRMWARE_ROOT / "artifacts" / "memoria-esp-vocat-app.bin"
DEFAULT_KEY = Path.home() / ".config" / "memoria" / "secrets" / "firmware-release-ed25519.key"
DEFAULT_OUT = REPO_ROOT / "outputs" / "firmware-releases"
# Host side of control-api's /data bind; the service reads /data/firmware-releases.
DEFAULT_REMOTE_ROOT = "/var/lib/memoria/firmware-releases"

# esp_image_header_t (24) + esp_image_segment_header_t (8), then esp_app_desc_t:
# magic, secure_version, reserv1[2], version[32], project_name[32], ...
_APP_DESC_OFFSET = 32
_APP_DESC_MAGIC = 0xABCD5432


def fail(message: str) -> None:
    raise SystemExit(f"error: {message}")


def header_build(path: Path = RELEASE_HEADER) -> int:
    match = re.search(r"^#define MEMORIA_FIRMWARE_BUILD (\d+)$", path.read_text(), re.MULTILINE)
    if match is None:
        fail(f"MEMORIA_FIRMWARE_BUILD not found in {path}")
    return int(match.group(1))


def _c_string(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii")


def inspect_image(image: bytes, build: int) -> str:
    """Validate the image and return its esp_app_desc version string."""
    if not image or image[0] != 0xE9:
        fail("not an ESP application image (magic 0xE9 missing)")
    if len(image) > MAX_IMAGE_BYTES:
        fail(f"image is {len(image)} bytes; the OTA slot holds {MAX_IMAGE_BYTES}")
    desc = image[_APP_DESC_OFFSET : _APP_DESC_OFFSET + 256]
    if int.from_bytes(desc[0:4], "little") != _APP_DESC_MAGIC:
        fail("esp_app_desc not found where expected")
    project = _c_string(desc[48:80])
    if project != "memoria":
        fail(f"image project is {project!r}, not 'memoria'")
    markers = re.findall(rb"MEMORIA_FIRMWARE_BUILD=(\d+);", image)
    if markers != [str(build).encode()]:
        fail(
            f"image build markers {markers!r} do not match header build {build}; "
            "rebuild after bumping MEMORIA_FIRMWARE_BUILD"
        )
    return _c_string(desc[16:48])


def load_key(path: Path) -> Ed25519PrivateKey:
    if path.stat().st_mode & 0o077:
        fail(f"{path} must not be readable by group or others (chmod 600)")
    key = load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        fail(f"{path} is not an Ed25519 private key")
    return key


def sign(
    image_path: Path,
    key_path: Path,
    out: Path,
    *,
    build: int | None = None,
    public_key: bytes = RELEASE_PUBLIC_KEY,
) -> Path:
    build = header_build() if build is None else build
    image = image_path.read_bytes()
    app_version = inspect_image(image, build)
    release: dict[str, object] = {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "board": FIRMWARE_BOARD,
        "build": build,
        "version": f"{app_version}+m{build}",
        "size": len(image),
        "sha256": hashlib.sha256(image).hexdigest(),
    }
    release["signature"] = b64url_encode(load_key(key_path).sign(canonical_release_payload(release)))
    # Against the key the firmware embeds: a wrong private key fails here.
    validate_release(release, public_key=public_key)
    target = out / FIRMWARE_BOARD / str(build)
    document = json.dumps(release, indent=2, sort_keys=True) + "\n"
    if target.exists():
        existing = (target / "app.bin").read_bytes() if (target / "app.bin").exists() else b""
        if existing != image:
            fail(f"{target} already holds a different image for build {build}; bump the build")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image_path, target / "app.bin")
    (target / "release.json").write_text(document)
    print(f"signed build {build} ({release['version']}, {len(image)} bytes, sha256 {release['sha256']})")
    print(f"release: {target}")
    return target


def _ssh(remote: str, script: str) -> None:
    subprocess.run(["ssh", remote, "bash -s"], input=script, text=True, check=True)


def upload(remote: str, remote_root: str, build: int, out: Path) -> None:
    source = out / FIRMWARE_BOARD / str(build)
    release = validate_release(json.loads((source / "release.json").read_text()))
    if release["build"] != build:
        fail("release.json build does not match --build")
    board_dir = f"{remote_root}/{FIRMWARE_BOARD}"
    staging = f"{board_dir}/.incoming-{build}"
    _ssh(remote, f"set -euo pipefail\nmkdir -p {shlex.quote(staging)}\n")
    subprocess.run(
        ["rsync", "-a", "--checksum", f"{source}/", f"{remote}:{staging}/"],
        check=True,
    )
    final = f"{board_dir}/{build}"
    _ssh(
        remote,
        f"""set -euo pipefail
cd {shlex.quote(staging)}
test "$(stat -c %s app.bin)" = {release["size"]}
test "$(sha256sum app.bin | cut -d' ' -f1)" = {release["sha256"]}
rm -rf {shlex.quote(final)}
mv {shlex.quote(staging)} {shlex.quote(final)}
chmod 0755 {shlex.quote(final)}
chmod 0644 {shlex.quote(final)}/app.bin {shlex.quote(final)}/release.json
cp {shlex.quote(final)}/release.json {shlex.quote(board_dir)}/.current.json.new
mv -f {shlex.quote(board_dir)}/.current.json.new {shlex.quote(board_dir)}/current.json
echo "current -> build {build}"
""",
    )


def withdraw(remote: str, remote_root: str) -> None:
    _ssh(
        remote,
        f"set -euo pipefail\nrm -f {shlex.quote(remote_root)}/{FIRMWARE_BOARD}/current.json\n"
        "echo 'no firmware release offered'\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sign_parser = commands.add_parser("sign")
    sign_parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    sign_parser.add_argument("--key", type=Path, default=DEFAULT_KEY)
    sign_parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    upload_parser = commands.add_parser("upload")
    upload_parser.add_argument("--remote", required=True)
    upload_parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    upload_parser.add_argument("--build", type=int, required=True)
    upload_parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    withdraw_parser = commands.add_parser("withdraw")
    withdraw_parser.add_argument("--remote", required=True)
    withdraw_parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    args = parser.parse_args()
    for value in (getattr(args, "remote", None), getattr(args, "remote_root", None)):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9@._/-]+", value):
            fail(f"invalid value {value!r}")
    if args.command == "sign":
        sign(args.image, args.key, args.out)
    elif args.command == "upload":
        upload(args.remote, args.remote_root, args.build, args.out)
    else:
        withdraw(args.remote, args.remote_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
