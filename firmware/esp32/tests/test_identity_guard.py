from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).parents[1] / "scripts" / "identity_guard.py"
FIRMWARE = Path(__file__).parents[1]
SCRIPTS = FIRMWARE / "scripts"
OVERLAY = FIRMWARE / "overlay"
SECRET = b"SUPER_SECRET_SEED_VALUE!!!!!"
PAGE_SIZE = 4096


def _load_guard():
    spec = importlib.util.spec_from_file_location("identity_guard_under_test", GUARD)
    if spec is None or spec.loader is None:
        raise RuntimeError("identity_guard.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _page(key: str, *, ns: int = 0, written: bool = True, payload: bytes = b"") -> bytes:
    page = bytearray(b"\xff" * PAGE_SIZE)
    if written:
        page[32] = 0xFE
    entry = bytearray(b"\xff" * 32)
    entry[0] = ns
    entry[1] = 0x01
    entry[2] = 1
    entry[3] = 0xFF
    encoded = key.encode()
    entry[8:24] = b"\x00" * 16
    entry[8 : 8 + len(encoded)] = encoded
    page[64:96] = entry
    if payload:
        page[96 : 96 + len(payload)] = payload
    return bytes(page)


def _image(*pages: bytes) -> bytes:
    blob = b"".join(pages)
    if len(blob) < 0x10000:
        blob += b"\xff" * (0x10000 - len(blob))
    return blob


def test_probe_reports_presence_without_printing_payload(tmp_path: Path) -> None:
    image = tmp_path / "identity.bin"
    image.write_bytes(_image(_page("device", payload=SECRET)))

    present = _run("probe", "--image", str(image), "--phase", "before")
    absent_after = _run("probe", "--image", str(image), "--phase", "after")

    assert present.returncode == 0
    assert present.stdout == "device namespace: present\n"
    assert present.stderr == ""
    assert absent_after.returncode == 0
    assert SECRET.decode() not in present.stdout + present.stderr
    assert SECRET.decode() not in absent_after.stdout + absent_after.stderr


def test_verdict_fails_only_when_a_present_namespace_disappears(tmp_path: Path) -> None:
    present = tmp_path / "present.bin"
    present.write_bytes(_image(_page("device", payload=SECRET)))
    absent = tmp_path / "absent.bin"
    absent.write_bytes(b"\xff" * 0x10000)
    value_only = tmp_path / "value.bin"
    value_only.write_bytes(_image(_page("device", ns=1, payload=SECRET)))
    other = tmp_path / "other.bin"
    other.write_bytes(_image(_page("wifi")))
    erased = tmp_path / "erased.bin"
    erased.write_bytes(_image(_page("device", written=False)))
    warning = "WARNING: memoria_identity 尚未写入设备身份，需运行 provision_identity.py"

    lost = _run("verdict", "--before-image", str(present), "--after-image", str(absent))
    lost_value = _run(
        "verdict", "--before-image", str(present), "--after-image", str(value_only)
    )
    lost_other = _run("verdict", "--before-image", str(present), "--after-image", str(other))
    lost_erased = _run(
        "verdict", "--before-image", str(present), "--after-image", str(erased)
    )
    unprovisioned = _run(
        "verdict", "--before-image", str(absent), "--after-image", str(absent)
    )
    kept = _run("verdict", "--before-image", str(present), "--after-image", str(present))
    gained = _run("verdict", "--before-image", str(absent), "--after-image", str(present))

    assert lost.returncode == 1
    assert "absent after flash" in lost.stderr
    assert lost.stdout == ""
    assert lost_value.returncode == 1
    assert lost_other.returncode == 1
    assert lost_erased.returncode == 1
    assert unprovisioned.returncode == 0
    assert unprovisioned.stderr.strip() == warning
    assert unprovisioned.stdout == ""
    assert kept.returncode == 0
    assert kept.stdout == ""
    assert kept.stderr == ""
    assert gained.returncode == 0
    assert gained.stderr == ""
    combined = "".join(
        item.stdout + item.stderr
        for item in (lost, lost_value, lost_other, lost_erased, unprovisioned, kept, gained)
    )
    assert SECRET.decode() not in combined

    before = _run("probe", "--image", str(present), "--phase", "before")
    after = _run("probe", "--image", str(absent), "--phase", "after")
    from_status = _run(
        "verdict",
        "--before-status",
        before.stdout.strip(),
        "--after-status",
        after.stdout.strip(),
    )
    assert from_status.returncode == 1
    assert SECRET.decode() not in from_status.stdout + from_status.stderr
    leaked = _run(
        "verdict",
        "--before-status",
        SECRET.decode(),
        "--after-status",
        "device namespace: absent",
    )
    assert leaked.returncode == 1
    assert SECRET.decode() not in leaked.stdout + leaked.stderr
    assert "unreadable" in leaked.stderr


def test_full_chip_image_is_refused_unless_the_flag_is_set(tmp_path: Path) -> None:
    danger = tmp_path / "memoria-esp-vocat-DANGER_FULL_ERASE.bin"
    danger.write_bytes(b"\xff" * 0x20000)
    merged = tmp_path / "memoria-esp-vocat-merged.bin"
    merged.write_bytes(b"\x00" * 16)
    bootloader = tmp_path / "bootloader.bin"
    bootloader.write_bytes(b"\x00" * 0x8000)

    refused = _run("image", "--image", str(danger), "--offset", "0")
    named = _run("image", "--image", str(merged), "--offset", "0")
    allowed = _run(
        "image",
        "--image",
        str(danger),
        "--offset",
        "0",
        "--allow-danger-full-erase",
    )
    small = _run("image", "--image", str(bootloader), "--offset", "0")

    assert refused.returncode == 2
    assert "--allow-danger-full-erase" in refused.stderr
    assert named.returncode == 2
    assert allowed.returncode == 0
    assert allowed.stdout == ""
    assert small.returncode == 0


def test_flash_plan_refuses_an_identity_overlap(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    app = build / "memoria.bin"
    app.write_bytes(b"\x00" * 32)
    identity = build / "identity.bin"
    identity.write_bytes(b"")
    plan = tmp_path / "flasher_args.json"
    plan.write_text(
        '{"flash_files": {"0x0": "bootloader.bin", "0x20000": "memoria.bin", "0x10000": "identity.bin"}}',
        encoding="utf-8",
    )
    (build / "bootloader.bin").write_bytes(b"\x00" * 0x1000)

    refused = _run("plan", str(plan), "--build-dir", str(build))
    safe = tmp_path / "safe.json"
    safe.write_text(
        '{"flash_files": {"0x0": "bootloader.bin", "0x20000": "memoria.bin"}}',
        encoding="utf-8",
    )
    allowed = _run("plan", str(safe), "--build-dir", str(build))

    assert refused.returncode == 2
    assert "identity.bin" in refused.stderr
    assert allowed.returncode == 0


def test_flash_sh_refuses_a_danger_image_before_it_boots_the_toolchain(tmp_path: Path) -> None:
    image = tmp_path / "board-DANGER_FULL_ERASE.bin"
    image.write_bytes(b"\xff" * 32)
    result = subprocess.run(
        ["bash", str(SCRIPTS / "flash.sh"), "--image", str(image)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--allow-danger-full-erase" in result.stderr


def test_read_prints_only_the_namespace_status(tmp_path: Path) -> None:
    fake = tmp_path / "fake"
    package = fake / "esptool"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if 'read-flash' not in args:\n"
        "    raise SystemExit(9)\n"
        "Path(os.environ['ESPTOOL_ARGV']).write_text('\\n'.join(sys.argv), encoding='utf-8')\n"
        "output = Path(args[-1])\n"
        "output.write_bytes(Path(os.environ['IDENTITY_IMAGE']).read_bytes())\n"
        "print(os.environ['TOOL_NOISE'])\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    image = tmp_path / "identity.bin"
    image.write_bytes(_image(_page("device", payload=SECRET)))
    argv_path = tmp_path / "argv.txt"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(fake)
    env["IDENTITY_IMAGE"] = str(image)
    env["ESPTOOL_ARGV"] = str(argv_path)
    env["TOOL_NOISE"] = SECRET.decode()

    result = _run(
        "read",
        "--port",
        "/dev/cu.fake",
        "--phase",
        "after",
        "--python",
        sys.executable,
        env=env,
    )

    recorded = argv_path.read_text(encoding="utf-8")
    output_path = Path(recorded.splitlines()[-1])
    assert result.returncode == 0
    assert result.stdout == "device namespace: present\n"
    assert SECRET.decode() not in result.stdout
    assert SECRET.decode() not in result.stderr
    assert "read-flash" in recorded
    assert "0x10000" in recorded
    assert "--silent" in recorded
    assert "--no-progress" in recorded
    assert not output_path.exists()


def test_read_after_a_blank_partition_reports_absent(tmp_path: Path) -> None:
    fake = tmp_path / "fake"
    package = fake / "esptool"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[-1]).write_bytes(b'\\xff' * 0x10000)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(fake)
    result = _run(
        "read",
        "--port",
        "/dev/cu.fake",
        "--phase",
        "after",
        "--python",
        sys.executable,
        env=env,
    )
    assert result.returncode == 0
    assert result.stdout == "device namespace: absent\n"


def test_read_hides_tool_errors(tmp_path: Path) -> None:
    fake = tmp_path / "fake"
    package = fake / "esptool"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import os\n"
        "import sys\n"
        "sys.stderr.write(os.environ['TOOL_NOISE'])\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(fake)
    env["TOOL_NOISE"] = SECRET.decode()
    result = _run(
        "read",
        "--port",
        "/dev/cu.fake",
        "--phase",
        "before",
        "--python",
        sys.executable,
        env=env,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert SECRET.decode() not in result.stderr
    assert "identity partition read failed" in result.stderr


def test_write_image_refuses_a_full_chip_file_without_calling_esptool(tmp_path: Path) -> None:
    fake = tmp_path / "fake"
    package = fake / "esptool"
    package.mkdir(parents=True)
    marker = tmp_path / "called.txt"
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['ESPTOOL_CALLED']).write_text('called', encoding='utf-8')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    image = tmp_path / "board-DANGER_FULL_ERASE.bin"
    image.write_bytes(b"\xff" * 0x20000)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(fake)
    env["ESPTOOL_CALLED"] = str(marker)
    result = _run(
        "write-image",
        "--port",
        "/dev/cu.fake",
        "--image",
        str(image),
        "--python",
        sys.executable,
        env=env,
    )
    assert result.returncode == 2
    assert "--allow-danger-full-erase" in result.stderr
    assert not marker.exists()


def test_repository_erase_policy_is_clean() -> None:
    result = _run("scan", str(OVERLAY), str(SCRIPTS))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("esptool erase-flash\n", "whole_flash_erase"),
        ("idf.py erase_flash\n", "whole_flash_erase"),
        ("nvs_flash_erase();\n", "unnamed_nvs_erase"),
        ('nvs_flash_erase_partition("memoria_identity");\n', "identity_erase"),
        ("erase-region 0x10000 0x10000\n", "identity_erase"),
        ("erase-region start length\n", "unqualified_region_erase"),
        (
            'nvs_open_from_partition("memoria_identity", "device", NVS_READWRITE, &handle);\n'
            "nvs_erase_all(handle);\n",
            "identity_erase",
        ),
        (
            "void FactoryResetFromGpio() {\n"
            "  nvs_flash_erase();\n"
            "}\n",
            "unnamed_nvs_erase",
        ),
        (
            "void FactoryResetFromGpio() {\n"
            "  const esp_partition_t* part = esp_partition_find_first("
            "ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, \"nvs\");\n"
            "  esp_partition_erase_range(part, 0, 0x6000);\n"
            "}\n",
            "partition_erase_range",
        ),
        (
            "void ClearRegion() {\n"
            "  esp_flash_erase_region(NULL, 0x10000, 0x1000);\n"
            "}\n",
            "flash_erase_region",
        ),
    ],
)
def test_erase_policy_rejects_factory_reset_and_full_chip_erases(
    tmp_path: Path, source: str, rule: str
) -> None:
    path = tmp_path / "factory_reset.cc"
    path.write_text(source, encoding="utf-8")
    result = _run("scan", str(path))
    assert result.returncode == 1
    assert rule in result.stderr


def test_ota_calls_are_not_treated_as_ranged_erases(tmp_path: Path) -> None:
    path = tmp_path / "memoria_firmware_update.cc"
    path.write_text(
        "void Install() {\n"
        "  esp_ota_begin(slot, OTA_WITH_SEQUENTIAL_WRITES, &handle);\n"
        "  esp_ota_write(handle, buffer, read);\n"
        "  esp_ota_end(handle);\n"
        "}\n",
        encoding="utf-8",
    )
    result = _run("scan", str(path))
    assert result.returncode == 0, result.stderr


def test_listed_function_may_erase_a_range_and_other_functions_may_not(tmp_path: Path) -> None:
    guard = _load_guard()
    assert guard.ERASE_RANGE_ALLOWLIST == ()
    path = "main/memoria/memoria_firmware_update.cc"
    source = (
        "void EraseInactiveAppSlot() {\n"
        "  esp_partition_erase_range(slot, 0, size);\n"
        "}\n"
        "void FactoryResetFromGpio() {\n"
        "  const esp_partition_t* part = esp_partition_find_first("
        "ESP_PARTITION_TYPE_APP, ESP_PARTITION_SUBTYPE_ANY, NULL);\n"
        "  esp_partition_erase_range(part, 0, 0x1000);\n"
        "}\n"
    )
    allowance = guard.EraseRangeAllowance(
        path,
        "EraseInactiveAppSlot",
        "erases the inactive OTA app slot",
    )
    denied = guard.scan_text(path, source)
    allowed = guard.scan_text(path, source, allowlist=(allowance,))
    blank_reason = guard.scan_text(
        path,
        source,
        allowlist=(guard.EraseRangeAllowance(path, "EraseInactiveAppSlot", "  "),),
    )

    assert [item.rule for item in denied] == [
        "partition_erase_range",
        "partition_erase_range",
    ]
    assert len(allowed) == 1
    assert allowed[0].rule == "partition_erase_range"
    assert "part, 0, 0x1000" in source.splitlines()[allowed[0].line - 1]
    assert [item.rule for item in blank_reason] == [
        "partition_erase_range",
        "partition_erase_range",
    ]
    scanned = tmp_path / "memoria_firmware_update.cc"
    scanned.write_text(source, encoding="utf-8")
    cli = _run("scan", str(scanned))
    assert cli.returncode == 1
    assert cli.stderr.count("partition_erase_range") == 2


def test_named_nvs_and_namespace_erases_stay_allowed(tmp_path: Path) -> None:
    path = tmp_path / "factory_reset.cc"
    path.write_text(
        "void FactoryResetFromGpio() {\n"
        '  nvs_flash_erase_partition("nvs");\n'
        "  nvs_flash_erase_partition(NVS_DEFAULT_PART_NAME);\n"
        "  nvs_erase_all(wifi);\n"
        '  nvs_erase_key(ui, "companion");\n'
        "}\n"
        "erase-region 0x20000 0x3f0000\n",
        encoding="utf-8",
    )
    result = _run("scan", str(path))
    assert result.returncode == 0, result.stderr


def test_build_does_not_publish_merged_bin_and_flash_checks_both_sides() -> None:
    build = (SCRIPTS / "build.sh").read_text(encoding="utf-8")
    flash = (SCRIPTS / "flash.sh").read_text(encoding="utf-8")
    check = (SCRIPTS / "check-overlay.sh").read_text(encoding="utf-8")
    assert "artifact_name-merged.bin" not in build
    assert "DANGER_FULL_ERASE" in build
    assert build.index("generated sdkconfig lacks") < build.index('cp "$merged"')
    assert "--allow-danger-full-erase" in flash
    assert flash.index("--phase before") < flash.index('idf.py -p "$port" flash')
    assert flash.index('idf.py -p "$port" flash') < flash.index("--phase after")
    assert flash.index("--phase after") < flash.index(
        'verdict --before-status "$before_line" --after-status "$after_line"'
    )
    assert "identity_guard.py" in check
    help_build = subprocess.run(
        ["bash", str(SCRIPTS / "build.sh"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    help_flash = subprocess.run(
        ["bash", str(SCRIPTS / "flash.sh"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_build.returncode == 0
    assert "--danger-full-erase" in help_build.stdout
    assert help_flash.returncode == 0
    assert "--allow-danger-full-erase" in help_flash.stdout
    assert "--image" in help_flash.stdout
