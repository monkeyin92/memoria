"""Playback stop and standby-after-playback stay on unless a poisoned image compiles one out."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE = pathlib.Path(__file__).parents[1]
HEADER = FIRMWARE / "overlay" / "files" / "main" / "memoria" / "memoria_playback_bisect.h"
PATCH = FIRMWARE / "overlay" / "patches" / "0043-memoria-playback-bisect.patch"
BOARD = FIRMWARE / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
COMPOSE = FIRMWARE / "scripts" / "compose_bisect_config.py"
BUILD = FIRMWARE / "scripts" / "build.sh"

HARNESS = r"""
#include "memoria_playback_bisect.h"

#include <cstdio>

int main() {
    if constexpr (!memoria::kPlaybackStopEnabled) {
        printf("stop=build_disabled\n");
    } else {
        printf("stop=run\n");
    }
    if constexpr (memoria::kStandbyAfterPlaybackEnabled) {
        printf("standby=honor\n");
    } else {
        printf("standby=ignore\n");
    }
    return 0;
}
"""


def _compile(flags: list[str]) -> list[str]:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "bisect"
        subprocess.run(
            [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(HEADER.parent), *flags, str(source), "-o", str(binary)],
            check=True,
        )
        completed = subprocess.run([str(binary)], text=True, capture_output=True, check=True)
        return completed.stdout.splitlines()


def test_each_switch_compiles_out_on_its_own() -> None:
    assert _compile(["-DCONFIG_MEMORIA_PLAYBACK_STOP=1", "-DCONFIG_MEMORIA_STANDBY_AFTER_PLAYBACK=1"]) == [
        "stop=run",
        "standby=honor",
    ]
    assert _compile(["-DCONFIG_MEMORIA_PLAYBACK_STOP=0", "-DCONFIG_MEMORIA_STANDBY_AFTER_PLAYBACK=1"]) == [
        "stop=build_disabled",
        "standby=honor",
    ]
    assert _compile(["-DCONFIG_MEMORIA_PLAYBACK_STOP=1", "-DCONFIG_MEMORIA_STANDBY_AFTER_PLAYBACK=0"]) == [
        "stop=run",
        "standby=ignore",
    ]
    assert _compile([]) == ["stop=build_disabled", "standby=ignore"]


def test_the_firmware_uses_those_constants_to_drop_the_path() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    assert "if constexpr (!memoria::kPlaybackStopEnabled)" in patch
    assert 'ESP_LOGI(TAG, "stop-arm speaking armed=0 reason=build_disabled")' in patch
    assert "audio_service_.EnableWakeWordDetection(false);" in patch
    assert "if constexpr (memoria::kStandbyAfterPlaybackEnabled)" in patch
    assert "standby_protocol->ConsumeStandbyAfterPlayback()" in patch
    assert "config MEMORIA_PLAYBACK_STOP" in patch
    assert "config MEMORIA_STANDBY_AFTER_PLAYBACK" in patch
    stop = patch.split("config MEMORIA_PLAYBACK_STOP\n", 1)[1].split("config MEMORIA_STANDBY", 1)[0]
    standby = patch.split("config MEMORIA_STANDBY_AFTER_PLAYBACK\n", 1)[1]
    assert "\n+    default y\n" in stop
    assert "\n+    default y\n" in standby


def _compose():
    import importlib.util

    spec = importlib.util.spec_from_file_location("compose_bisect_config", COMPOSE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_poisoned_bench_image_can_drop_one_path_and_keep_heap_poisoning() -> None:
    bench = json.loads((BOARD / "config.bench.json").read_text(encoding="utf-8"))
    module = _compose()
    dropped = module.compose(bench, disable_playback_stop=True, disable_standby=False)
    options = dropped["builds"][0]["sdkconfig_append"]
    assert options[-1] == "CONFIG_MEMORIA_PLAYBACK_STOP=n"
    assert "CONFIG_HEAP_POISONING_COMPREHENSIVE=y" in options
    assert "CONFIG_MEMORIA_STANDBY_AFTER_PLAYBACK=n" not in options
    both = module.compose(bench, disable_playback_stop=True, disable_standby=True)
    assert both["builds"][0]["sdkconfig_append"][-2:] == [
        "CONFIG_MEMORIA_PLAYBACK_STOP=n",
        "CONFIG_MEMORIA_STANDBY_AFTER_PLAYBACK=n",
    ]
    product = json.loads((BOARD / "config.json").read_text(encoding="utf-8"))
    with pytest.raises(SystemExit, match="CONFIG_HEAP_POISONING_COMPREHENSIVE=y"):
        module.compose(product, disable_playback_stop=True, disable_standby=False)


def test_bisect_flags_are_refused_on_a_product_build() -> None:
    completed = subprocess.run([str(BUILD), "--disable-playback-stop"], text=True, capture_output=True)
    assert completed.returncode != 0
    assert "bisect switches require --bench or --keep-awake so heap poisoning stays on" in completed.stderr
