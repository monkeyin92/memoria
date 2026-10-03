"""The OTA rollback drill image (TODOLIST P2-08: "OTA 回滚没演练过").

The drill is the shipping firmware with a newer build number that never confirms itself, so an over-the-air
install must be rolled back by the bootloader at the next reset. The sources are produced from a copy of the
build cache's files and restored afterwards: no drill code is ever committed. These checks pin what the
transformation does and refuses, and that the build script refuses a drill that is not newer than the
shipping build, before it touches anything.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import subprocess

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
RELEASE_HEADER = MEMORIA_DIR / "memoria_firmware_release.h"
UPDATE_SOURCE = MEMORIA_DIR / "memoria_firmware_update.cc"
TRANSFORM = FIRMWARE_ROOT / "scripts" / "make_ota_rollback_drill_sources.py"
BUILD_SCRIPT = FIRMWARE_ROOT / "scripts" / "build_ota_rollback_drill.sh"


def _transform():
    spec = importlib.util.spec_from_file_location("make_ota_rollback_drill_sources", TRANSFORM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shipping_build() -> int:
    match = re.search(r"^#define MEMORIA_FIRMWARE_BUILD (\d+)$", RELEASE_HEADER.read_text(), re.MULTILINE)
    assert match is not None
    return int(match.group(1))


@pytest.fixture
def copies(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    header = tmp_path / "memoria_firmware_release.h"
    source = tmp_path / "memoria_firmware_update.cc"
    header.write_text(RELEASE_HEADER.read_text(encoding="utf-8"), encoding="utf-8")
    source.write_text(UPDATE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    return header, source


def test_the_drill_gets_its_own_build_number_and_never_confirms_itself(
    copies: tuple[pathlib.Path, pathlib.Path],
) -> None:
    header, source = copies
    drill = _shipping_build() + 1

    _transform().apply(header, source, drill)

    assert f"#define MEMORIA_FIRMWARE_BUILD {drill}\n" in header.read_text()
    assert header.read_text().count("MEMORIA_FIRMWARE_BUILD ") == RELEASE_HEADER.read_text().count(
        "MEMORIA_FIRMWARE_BUILD "
    )
    changed = source.read_text()
    assert "OTA rollback drill" in changed
    assert "if (false && esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {" in changed
    # The drill log comes after the boot marker and the PENDING_VERIFY check, so a drill that was
    # installed over the air still reports its build and slot first.
    assert changed.index("memoria_firmware_build_marker,") < changed.index("OTA rollback drill")
    assert changed.index("ESP_OTA_IMG_PENDING_VERIFY") < changed.index("OTA rollback drill")


def test_the_shipping_sources_are_not_touched(copies: tuple[pathlib.Path, pathlib.Path]) -> None:
    before = (RELEASE_HEADER.read_bytes(), UPDATE_SOURCE.read_bytes())

    _transform().apply(*copies, _shipping_build() + 1)

    assert (RELEASE_HEADER.read_bytes(), UPDATE_SOURCE.read_bytes()) == before
    assert "OTA rollback drill" not in UPDATE_SOURCE.read_text()


def test_a_second_pass_or_a_source_that_changed_shape_is_refused(
    copies: tuple[pathlib.Path, pathlib.Path],
) -> None:
    header, source = copies
    transform = _transform()
    transform.apply(header, source, _shipping_build() + 1)

    with pytest.raises(SystemExit, match="confirm call"):
        transform.apply(header, source, _shipping_build() + 2)

    header.write_text("// no build define\n")
    with pytest.raises(SystemExit, match="exactly one"):
        transform.apply(header, source, 99)


def _sandbox(tmp_path: pathlib.Path) -> dict[str, str]:
    """An environment in which the script cannot find a build cache or write artifacts.

    The refusals below must come from the script's own guards. Pointing it at nothing means that a guard
    that stopped working would die on the missing cache, instead of running a real firmware build (which
    a mutation run of this very test once did, overwriting the build artifacts).
    """

    return {
        **os.environ,
        "MEMORIA_ESP32_UPSTREAM_DIR": str(tmp_path / "no-upstream-cache"),
        "MEMORIA_ESP32_ARTIFACT_DIR": str(tmp_path / "artifacts"),
    }


def test_the_build_script_refuses_a_drill_that_is_not_newer_than_the_shipping_build(
    tmp_path: pathlib.Path,
) -> None:
    env = _sandbox(tmp_path)
    for build in (str(_shipping_build()), str(_shipping_build() - 1)):
        completed = subprocess.run(
            ["bash", str(BUILD_SCRIPT), build], capture_output=True, text=True, check=False, env=env
        )
        assert completed.returncode != 0
        assert "newer than the shipping build" in completed.stderr
    refused = subprocess.run(
        ["bash", str(BUILD_SCRIPT), "twenty"], capture_output=True, text=True, check=False, env=env
    )
    assert refused.returncode == 2 and "Usage:" in refused.stderr
    assert not (tmp_path / "artifacts").exists()


def test_without_a_prepared_build_cache_the_script_stops_before_touching_anything(
    tmp_path: pathlib.Path,
) -> None:
    completed = subprocess.run(
        ["bash", str(BUILD_SCRIPT), str(_shipping_build() + 1)],
        capture_output=True,
        text=True,
        check=False,
        env=_sandbox(tmp_path),
    )

    assert completed.returncode != 0
    assert "upstream cache is not prepared" in completed.stderr
    assert not (tmp_path / "artifacts").exists()


def test_the_build_script_restores_the_cache_and_keeps_strict_mode() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^set -euo pipefail$", script, re.MULTILINE)
    assert "trap restore_sources EXIT" in script
    assert script.index("trap restore_sources EXIT") < script.index('"$SCRIPT_DIR/make_ota_rollback_drill_sources.py"')
    # The drill must never be the image that `sign` picks up by default.
    assert "ota-rollback-drill/app-" in script
