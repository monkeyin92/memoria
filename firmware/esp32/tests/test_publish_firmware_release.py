"""The OTA publisher signs only real, build-matched Memoria images."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from services.control_api.app import device_firmware

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"


def _publisher():
    path = FIRMWARE_ROOT / "scripts" / "publish_firmware_release.py"
    spec = importlib.util.spec_from_file_location("publish_firmware_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _image(build: int, *, project: bytes = b"memoria", markers: int = 1, bench: bool = False) -> bytes:
    header = bytes([0xE9]) + b"\0" * 31
    desc = (0xABCD5432).to_bytes(4, "little") + b"\0" * 12
    desc += b"2.4.2".ljust(32, b"\0") + project.ljust(32, b"\0") + b"\0" * 176
    marker = f"MEMORIA_FIRMWARE_BUILD={build};".encode()
    bench_marker = b"MEMORIA_BENCH_BUILD=1;" if bench else b""
    return header + desc + b"\x11" * 4096 + marker * markers + bench_marker + b"\x22" * 128


def _key(tmp_path: pathlib.Path) -> tuple[pathlib.Path, bytes]:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "release.key"
    path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    path.chmod(0o600)
    return path, key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def test_sign_writes_a_release_the_server_accepts(tmp_path: pathlib.Path) -> None:
    publisher = _publisher()
    key_path, public_key = _key(tmp_path)
    image = tmp_path / "app.bin"
    image.write_bytes(_image(7))
    target = publisher.sign(image, key_path, tmp_path / "out", build=7, public_key=public_key)
    release = json.loads((target / "release.json").read_text())
    assert release["build"] == 7
    assert release["version"] == "2.4.2+m7"
    assert (target / "app.bin").read_bytes() == image.read_bytes()
    directory = device_firmware.FirmwareReleaseDirectory(tmp_path / "out", public_key=public_key)
    (tmp_path / "out" / "memoria-esp-vocat" / "current.json").write_text(json.dumps(release))
    current = directory.current()
    assert current is not None and current.build == 7


@pytest.mark.parametrize(
    ("image", "message"),
    [
        (_image(6), "markers"),  # built before the header was bumped
        (_image(7, markers=2), "markers"),
        (_image(7, project=b"xiaozhi"), "project"),
        (b"\x00" + _image(7)[1:], "magic"),
        # A test-rig image has the right project and the right build marker: only its own marker gives it away.
        (_image(7, bench=True), "bench"),
    ],
)
def test_sign_refuses_foreign_or_mismatched_images(
    tmp_path: pathlib.Path, image: bytes, message: str
) -> None:
    publisher = _publisher()
    key_path, public_key = _key(tmp_path)
    path = tmp_path / "app.bin"
    path.write_bytes(image)
    with pytest.raises(SystemExit, match=message):
        publisher.sign(path, key_path, tmp_path / "out", build=7, public_key=public_key)


def test_the_bench_marker_is_the_one_the_bench_build_carries() -> None:
    publisher = _publisher()
    board = MEMORIA_DIR.parent / "boards" / "memoria" / "esp-vocat"
    source = (board / "memoria_mascot_bench.cc").read_text(encoding="utf-8")
    defined = re.findall(r'memoria_bench_build_marker\[\] = "([^"]+)";', source)
    assert defined == [publisher.BENCH_MARKER.decode("ascii")]
    # Defined once, inside the bench switch, and nowhere a product image compiles.
    assert source.index("#if CONFIG_MEMORIA_BENCH_SERIAL") < source.index("memoria_bench_build_marker[] =")
    for path in (FIRMWARE_ROOT / "overlay" / "files" / "main").rglob("*"):
        if path.suffix in {".cc", ".h", ".c"} and path.name != "memoria_mascot_bench.cc":
            assert publisher.BENCH_MARKER.decode("ascii") not in path.read_text(encoding="utf-8"), path.name


def test_sign_refuses_a_key_the_firmware_does_not_trust(tmp_path: pathlib.Path) -> None:
    publisher = _publisher()
    key_path, _ = _key(tmp_path)
    path = tmp_path / "app.bin"
    path.write_bytes(_image(7))
    with pytest.raises(device_firmware.InvalidFirmwareRelease):
        publisher.sign(path, key_path, tmp_path / "out", build=7)  # embedded key


def test_firmware_and_server_share_the_release_key_and_board() -> None:
    header = (MEMORIA_DIR / "memoria_firmware_release.h").read_text(encoding="utf-8")
    key_block = header[header.index("kFirmwareReleasePublicKey = {") :]
    key_block = key_block[: key_block.index("};")]
    key = bytes(int(value, 16) for value in re.findall(r"0x([0-9a-f]{2})", key_block))
    assert key == device_firmware.RELEASE_PUBLIC_KEY
    assert f'kFirmwareBoard = "{device_firmware.FIRMWARE_BOARD}"' in header
    assert _publisher().header_build() >= 1


def test_firmware_rebuilds_the_signed_bytes_exactly() -> None:
    source = (MEMORIA_DIR / "memoria_firmware_update.cc").read_text(encoding="utf-8")
    assert 'kSigningDomain = "memoria-firmware-release-v1\\n"' in source
    assert (
        '"%s{\\"board\\":\\"%s\\",\\"build\\":%lu,\\"schema_version\\":1,\\"sha256\\":\\"%s\\","\n'
        '        "\\"size\\":%lu,\\"version\\":\\"%s\\"}"'
    ) in source
    # Fields are restricted to escape-free ASCII, so the printf form equals
    # the server's sorted compact JSON.
    printf = '{"board":"%s","build":%d,"schema_version":1,"sha256":"%s","size":%d,"version":"%s"}'
    fields = ("memoria-esp-vocat", 9, "ab" * 32, 1234, "2.4.2+m9")
    rebuilt = ("memoria-firmware-release-v1\n" + printf % fields).encode()  # noqa: UP031
    assert rebuilt == device_firmware.canonical_release_payload(
        {
            "schema_version": 1,
            "board": "memoria-esp-vocat",
            "build": 9,
            "version": "2.4.2+m9",
            "size": 1234,
            "sha256": "ab" * 32,
            "signature": "x",
        }
    )
    assert device_firmware.MAX_IMAGE_BYTES == 0x3F0000
    partitions = (FIRMWARE_ROOT / "overlay" / "files" / "partitions" / "v2" / "32m.csv").read_text()
    assert re.search(r"^ota_0,\s+app,\s+ota_0,\s+0x20000,\s+0x3f0000,", partitions, re.MULTILINE)


def test_firmware_installs_only_newer_signed_images_and_confirms_after_the_server() -> None:
    source = (MEMORIA_DIR / "memoria_firmware_update.cc").read_text(encoding="utf-8")
    assert "release.build <= kFirmwareBuild" in source
    assert "crypto_sign_verify_detached(" in source
    assert "kFirmwareReleasePublicKey.data()" in source
    assert "OTA_WITH_SEQUENTIAL_WRITES" in source
    assert "ToHex(hash.data(), hash.size()) != release.sha256" in source
    assert source.index("!= release.sha256") < source.index("esp_ota_set_boot_partition(slot)")
    assert source.index("esp_ota_end(handle)") < source.index("esp_ota_set_boot_partition(slot)")
    assert 'std::strcmp(description.project_name, "memoria") != 0' in source
    assert "may_continue && !may_continue()" in source
    assert "esp_ota_mark_app_valid_cancel_rollback()" in source
    assert "ESP_OTA_IMG_PENDING_VERIFY" in source

    protocol = (MEMORIA_DIR / "memoria_protocol.cc").read_text(encoding="utf-8")
    start = protocol[protocol.index("bool MemoriaProtocol::Start()") :]
    start = start[: start.index("void MemoriaProtocol::StartActivationRetry")]
    assert "activation_result == ESP_OK || activation_result == ESP_ERR_INVALID_STATE" in start
    assert "MemoriaFirmwareUpdate::ConfirmRunningImage();" in start
    task = protocol[protocol.index("void MemoriaProtocol::DisplayProfileTask") :]
    task = task[: task.index("void MemoriaProtocol::CheckFirmwareUpdate")]
    assert "GetDeviceState() == kDeviceStateIdle" in task
    assert "protocol->CheckFirmwareUpdate(base, &next_firmware_check_ms);" in task
    check = protocol[protocol.index("void MemoriaProtocol::CheckFirmwareUpdate") :]
    check = check[: check.index("bool MemoriaProtocol::ConfirmServerRelease")]
    assert check.index("while (!idle())") < check.index("esp_restart();")

    patch = (FIRMWARE_ROOT / "overlay" / "patches" / "0029-memoria-firmware-update.patch").read_text()
    assert '+            "memoria/memoria_firmware_update.cc"' in patch
    sdk = (FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat" / "config.json").read_text()
    assert '"CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y"' in sdk


def test_the_cli_signs_the_header_build_unless_told_another(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = _publisher()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        publisher,
        "sign",
        lambda image, key, out, *, build=None, public_key=None: calls.append({"build": build}),
    )

    monkeypatch.setattr("sys.argv", ["publish_firmware_release.py", "sign"])
    publisher.main()
    monkeypatch.setattr("sys.argv", ["publish_firmware_release.py", "sign", "--build", "22"])
    publisher.main()

    assert calls == [{"build": None}, {"build": 22}]
