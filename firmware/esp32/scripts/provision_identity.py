#!/usr/bin/env python3
"""Create and optionally flash the isolated Memoria device-identity NVS image.

The only permitted flash write is the 64 KiB ``memoria_identity`` partition at
0x10000. This script never prints identity values or seed material.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

PARTITION_OFFSET = 0x10000
PARTITION_SIZE = 0x10000
SEED_SIZE = 32
PUBLIC_KEY_SIZE = 32
NVS_NAMESPACE = "device"
NVS_GENERATOR_MODULE = "esp_idf_nvs_partition_gen"
PROTOCOL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-in", type=Path, required=True)
    parser.add_argument("--activation-pubkey-in", type=Path, required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--certificate-id", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--control-api-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", help="Optional ESP serial port; only the identity range is written")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--idf-python", type=Path)
    parser.add_argument("--esptool-python", type=Path)
    return parser


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"error: {message}")


def _require_regular(path: Path, *, exact_mode: int | None = None) -> None:
    if path.is_symlink():
        _fail(f"input must not be a symbolic link: {path}")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        _fail(f"cannot read input file: {path}")
    if not path.is_file():
        _fail(f"input is not a regular file: {path}")
    if exact_mode is not None and mode != exact_mode:
        _fail(f"seed input must have mode 0600: {path}")


def _read_exact(path: Path, size: int) -> bytes:
    _require_regular(path)
    value = path.read_bytes()
    if len(value) != size:
        _fail(f"input must contain exactly {size} raw bytes: {path}")
    return value


def _validate_protocol_id(name: str, value: str) -> None:
    if PROTOCOL_ID_RE.fullmatch(value) is None:
        _fail(f"{name} must match [A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}")


def _validate_url(value: str) -> None:
    if (
        not value
        or len(value) > 255
        or value != value.strip()
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        _fail("control-api-url must be non-empty printable ASCII and at most 255 bytes")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        _ = parsed.port  # Force malformed-port validation while the URL is local.
    except ValueError:
        _fail("control-api-url must be a valid http(s) URL")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not host:
        _fail("control-api-url must be an http(s) URL with a non-empty host")
    if parsed.username is not None or parsed.password is not None:
        _fail("control-api-url must not contain userinfo")
    if parsed.query or parsed.fragment or "?" in value or "#" in value:
        _fail("control-api-url must not contain a query or fragment")


def _find_idf_python(explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    env = os.environ.get("MEMORIA_PYTHON_BIN")
    if env:
        candidates.append(Path(env))
    for name in ("python3.12", "python3.10", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    home = Path.home()
    candidates.extend(
        home / ".espressif" / "python_env" / f"idf6.0_py{version}_env" / "bin" / "python"
        for version in ("3.12", "3.10")
    )
    for candidate in candidates:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        probe = subprocess.run(
            [str(candidate), "-c", f"import {NVS_GENERATOR_MODULE}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    _fail("ESP-IDF Python with esp_idf_nvs_partition_gen was not found")


def _find_esptool_python(explicit: Path | None, idf_python: Path) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    candidates.append(idf_python)
    found = shutil.which("python3")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        probe = subprocess.run(
            [str(candidate), "-c", "import esptool"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    _fail("Python with esptool was not found; pass --esptool-python")


def _write_csv(path: Path, args: argparse.Namespace) -> None:
    # The CSV references the seed file as a binary input. Secret bytes never
    # enter stdout, the command line, or the temporary CSV itself.
    with open(
        path,
        "w",
        encoding="utf-8",
        newline="",
        opener=lambda p, flags: os.open(p, flags, 0o600),
    ) as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["key", "type", "encoding", "value"])
        writer.writerow([NVS_NAMESPACE, "namespace", "", ""])
        writer.writerow(["device_id", "data", "string", args.device_id])
        writer.writerow(["certificate_id", "data", "string", args.certificate_id])
        writer.writerow(["client_id", "data", "string", args.client_id])
        writer.writerow(["ed25519_seed", "file", "binary", str(args.seed_in)])
        writer.writerow(["activation_pk", "file", "binary", str(args.activation_pubkey_in)])
        writer.writerow(["control_api_url", "data", "string", args.control_api_url])


def _run_generator(idf_python: Path, csv_path: Path, output_path: Path) -> None:
    result = subprocess.run(
        [
            str(idf_python),
            "-m",
            NVS_GENERATOR_MODULE,
            "generate",
            str(csv_path),
            str(output_path),
            str(PARTITION_SIZE),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Do not relay generator output: it may echo a CSV value on failure.
        _fail("NVS identity image generation failed")


def _flash(esptool_python: Path, port: str, baud: int, image: Path) -> None:
    if not port.startswith("/dev/cu."):
        _fail("--port must be a macOS /dev/cu.* device")
    if not 9_600 <= baud <= 2_000_000:
        _fail("baud must be between 9600 and 2000000")
    command = [
        str(esptool_python),
        "-m",
        "esptool",
        "--chip",
        "esp32s3",
        "--port",
        port,
        "--baud",
        str(baud),
        "--before",
        "default-reset",
        "--after",
        "hard-reset",
        "write-flash",
        f"0x{PARTITION_OFFSET:x}",
        str(image),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        _fail("identity-only flash failed")


def main() -> int:
    args = _parser().parse_args()
    _require_regular(args.seed_in, exact_mode=0o600)
    _require_regular(args.activation_pubkey_in)
    _read_exact(args.seed_in, SEED_SIZE)
    _read_exact(args.activation_pubkey_in, PUBLIC_KEY_SIZE)
    _validate_protocol_id("device-id", args.device_id)
    _validate_protocol_id("certificate-id", args.certificate_id)
    _validate_protocol_id("client-id", args.client_id)
    _validate_url(args.control_api_url)
    if args.port is None and args.baud != 460800:
        _fail("--baud requires --port")
    if args.output.exists():
        _fail(f"refusing to overwrite existing output: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    idf_python = _find_idf_python(args.idf_python)
    esptool_python = _find_esptool_python(args.esptool_python, idf_python) if args.port else None
    with tempfile.TemporaryDirectory(
        prefix="memoria-identity-", dir=str(args.output.parent)
    ) as temporary:
        temporary_path = Path(temporary)
        os.chmod(temporary_path, 0o700)
        csv_path = temporary_path / "identity.csv"
        image_path = temporary_path / "identity.bin"
        _write_csv(csv_path, args)
        _run_generator(idf_python, csv_path, image_path)
        if image_path.stat().st_size != PARTITION_SIZE:
            _fail("generated identity image is not exactly 64 KiB")
        os.chmod(image_path, 0o600)
        os.replace(image_path, args.output)
    os.chmod(args.output, 0o600)

    if args.port:
        assert esptool_python is not None
        _flash(esptool_python, args.port, args.baud, args.output)
        print(f"identity image flashed at 0x{PARTITION_OFFSET:05x} only")
    else:
        print(f"identity image generated: {args.output} ({PARTITION_SIZE} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
