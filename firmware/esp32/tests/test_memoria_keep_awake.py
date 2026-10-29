from __future__ import annotations

import json
import pathlib
import re
import subprocess

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
BOARD = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
BOARD_CC = BOARD / "memoria_esp_vocat.cc"
PATCH = FIRMWARE_ROOT / "overlay" / "patches" / "0039-memoria-keep-awake.patch"
IDLE_ARM_PATCH = (
    FIRMWARE_ROOT / "overlay" / "patches" / "0044-memoria-keep-awake-idle-phase.patch"
)
SCRIPTS = FIRMWARE_ROOT / "scripts"
OVERLAY_FILES = FIRMWARE_ROOT / "overlay" / "files"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _enter_tap_deep_sleep(source: str) -> str:
    start = source.index("void EnterTapDeepSleep()")
    end = source.index("void OnIdleScreenOff()")
    return source[start:end]


def _keep_awake_branch(body: str) -> str:
    start = body.index("#if CONFIG_MEMORIA_KEEP_AWAKE")
    return body[start : body.index("#else", start)]


def _sleep_branch(body: str) -> str:
    start = body.index("#if CONFIG_MEMORIA_KEEP_AWAKE")
    return body[body.index("#else", start) : body.index("#endif", start)]


def test_product_and_bench_manifests_omit_keep_awake() -> None:
    product = json.loads(_read(BOARD / "config.json"))
    bench = json.loads(_read(BOARD / "config.bench.json"))
    product_options = product["builds"][0]["sdkconfig_append"]
    bench_options = bench["builds"][0]["sdkconfig_append"]
    assert "CONFIG_MEMORIA_KEEP_AWAKE=y" not in product_options
    assert "CONFIG_MEMORIA_KEEP_AWAKE=y" not in bench_options
    assert not any("KEEP_AWAKE" in option for option in product_options + bench_options)


def test_keep_awake_manifest_is_the_product_manifest_plus_one_line() -> None:
    product = json.loads(_read(BOARD / "config.json"))
    keep = json.loads(_read(BOARD / "config.keep-awake.json"))
    product_options = product["builds"][0]["sdkconfig_append"]
    assert keep["builds"][0]["sdkconfig_append"] == [
        *product_options,
        "CONFIG_MEMORIA_KEEP_AWAKE=y",
    ]
    keep["builds"][0]["sdkconfig_append"] = product_options
    assert keep == product


def test_kconfig_defaults_off_and_depends_on_the_memoria_board() -> None:
    added = "\n".join(
        line[1:]
        for line in _read(PATCH).splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    block = added.split("config MEMORIA_KEEP_AWAKE\n", 1)[1]
    assert "bool " in block.split("help", 1)[0]
    assert "depends on BOARD_TYPE_MEMORIA_ESP_VOCAT" in block
    assert re.search(r"^\s*default n$", block, re.MULTILINE)
    assert not re.search(r"^\s*default y$", block, re.MULTILINE)


def test_product_text_still_deep_sleeps_and_keep_awake_text_does_not() -> None:
    body = _enter_tap_deep_sleep(_read(BOARD_CC))
    assert body.count("#if CONFIG_MEMORIA_KEEP_AWAKE") == 1
    assert body.count("#else") == 1
    assert body.count("#endif") == 1
    awake = _keep_awake_branch(body)
    sleep = _sleep_branch(body)
    assert "SetPhase(memoria::PowerPhase::kWifiHold)" in awake
    assert "wifi_hold_since_ms_.store(kWifiHoldNotArmed, std::memory_order_relaxed)" in awake
    assert 'ESP_LOGW(TAG, "chip deep sleep suppressed %s", memoria_keep_awake_build_marker)' in awake
    assert 'memoria_keep_awake_build_marker[] = "MEMORIA_KEEP_AWAKE_BUILD=1;"' in _read(BOARD_CC)
    assert "kWifiHoldNotArmed" in awake
    assert "kWaking" not in awake
    assert "kConversing" not in awake
    assert "esp_deep_sleep_start" not in awake
    assert "esp_sleep_enable_ext0_wakeup" not in awake
    assert "SetPhase(memoria::PowerPhase::kDeepSleep)" in sleep
    assert "esp_sleep_enable_ext0_wakeup(TP_PIN_NUM_INT, 0)" in sleep
    assert "esp_deep_sleep_start();" in sleep
    assert "kWifiHold" not in sleep


def _on_idle_screen_off(source: str) -> str:
    start = source.index("void OnIdleScreenOff()")
    return source[start : source.index("void MaybeEnterDeepSleepFromWifiHold()", start)]


def _patched_hunk(patch: str, must_contain: str) -> str:
    hunks: list[list[str]] = []
    current: list[str] | None = None
    for line in patch.splitlines():
        if line.startswith("@@"):
            if current is not None:
                hunks.append(current)
            current = []
            continue
        if current is None or line.startswith(("diff ", "index ", "--- ", "+++ ")):
            continue
        if line.startswith("+"):
            current.append(line[1:])
        elif line.startswith("-"):
            continue
        elif line.startswith("\\"):
            continue
        else:
            current.append(line[1:] if line.startswith(" ") else line)
    if current is not None:
        hunks.append(current)
    matches = ["\n".join(hunk) for hunk in hunks if must_contain in "\n".join(hunk)]
    assert len(matches) == 1
    return matches[0]


def test_keep_awake_idle_arm_does_not_enter_wifi_hold() -> None:
    assert IDLE_ARM_PATCH.is_file()
    patch = _read(IDLE_ARM_PATCH)
    assert 'extern "C" const char memoria_keep_awake_build_marker[];' in patch
    body = _patched_hunk(patch, "void Application::ArmIdleWakeWord()")
    before_gate = body.split("#if CONFIG_MEMORIA_KEEP_AWAKE", 1)[0]
    assert "SetPhase" not in before_gate
    assert body.count("#if CONFIG_MEMORIA_KEEP_AWAKE") == 1
    assert body.count("#else") == 2
    assert body.count("#endif") == 2
    assert "EnableWakeWordDetection(false)" in body
    assert "EnableWakeWordDetection(true)" in body
    awake = _keep_awake_branch(body)
    product = _sleep_branch(body)
    assert "SetPhase" not in awake
    assert "kWifiHold" not in awake
    assert "wifi hold suppressed" in awake
    assert "memoria_keep_awake_build_marker" in awake
    assert "SetPhase(memoria::PowerPhase::kWifiHold)" in product
    assert "MEMORIA_KEEP_AWAKE" not in product
    assert "wifi hold suppressed" not in product


def test_keep_awake_screen_off_does_not_enter_wifi_hold() -> None:
    body = _on_idle_screen_off(_read(BOARD_CC))
    assert body.count("#if CONFIG_MEMORIA_KEEP_AWAKE") == 1
    assert body.count("#else") == 1
    assert body.count("#endif") == 1
    awake = _keep_awake_branch(body)
    product = _sleep_branch(body)
    assert "SetPhase" not in awake
    assert "kWifiHold" not in awake
    assert "wifi hold started" not in awake
    assert "wifi hold suppressed" in awake
    assert "memoria_keep_awake_build_marker" in awake
    assert "SetPhase(memoria::PowerPhase::kWifiHold)" in product
    assert 'ESP_LOGI(TAG, "wifi hold started hold_ms=%u", power.wifi_hold_ms());' in product
    assert "MEMORIA_KEEP_AWAKE" not in product


def test_both_callers_still_enter_tap_deep_sleep() -> None:
    source = _read(BOARD_CC)
    hold = source[
        source.index("void MaybeEnterDeepSleepFromWifiHold()") : source.index(
            "void HandleScreenTouchRelease(std::uint32_t hold_ms)"
        )
    ]
    touch = source[
        source.index("void HandleScreenTouchRelease(std::uint32_t hold_ms)") : source.index(
            "void HandleUsbWake()"
        )
    ]
    usb_at = source.index("void HandleUsbWake()")
    usb = source[usb_at : source.index("#if CONFIG_MEMORIA_BENCH_SERIAL", usb_at)]
    assert hold.count("EnterTapDeepSleep()") == 1
    # Night soothe plus offline menu idle both call EnterTapDeepSleep.
    assert touch.count("EnterTapDeepSleep()") >= 1
    assert "MEMORIA_KEEP_AWAKE" not in hold
    assert "MEMORIA_KEEP_AWAKE" not in touch
    assert "MEMORIA_KEEP_AWAKE" not in usb
    assert "SetPhase(memoria::PowerPhase::kWaking)" in touch
    assert "DispatchTapWakeAction(" in usb
    assert source.count("#if CONFIG_MEMORIA_KEEP_AWAKE") == 3
    assert source.count("esp_deep_sleep_start();") == 1


def test_only_the_board_source_reads_the_switch() -> None:
    hits = sorted(
        path.relative_to(OVERLAY_FILES).as_posix()
        for path in OVERLAY_FILES.rglob("*")
        if path.suffix in {".c", ".cc", ".h"} and "CONFIG_MEMORIA_KEEP_AWAKE" in _read(path)
    )
    assert hits == ["main/boards/memoria/esp-vocat/memoria_esp_vocat.cc"]


def test_scripts_select_the_keep_awake_profile_and_keep_it_off_the_product() -> None:
    build = _read(SCRIPTS / "build.sh")
    flash = _read(SCRIPTS / "flash.sh")
    check = _read(SCRIPTS / "check-overlay.sh")
    assert 'artifact_name="$MEMORIA_BOARD_NAME-keep-awake"' in build
    assert "build_args+=(--config config.keep-awake.json)" in build
    assert "pass only one of --bench and --keep-awake" in build
    assert build.index("generated sdkconfig lacks CONFIG_MEMORIA_KEEP_AWAKE=y") < build.index(
        'cp "$merged"'
    )
    assert build.index(
        "product build carries CONFIG_MEMORIA_KEEP_AWAKE=y"
    ) < build.index('cp "$merged"')
    assert "the build in the tree is a KEEP-AWAKE image" in flash
    assert "--keep-awake was given but the build in the tree is not a keep-awake image" in flash
    assert flash.index("tree_is_keep_awake") < flash.index('idf.py -p "$port" flash')
    assert '["MEMORIA_BENCH_SERIAL", "MEMORIA_KEEP_AWAKE"]' in check
    assert "config.json and config.bench.json must omit MEMORIA_KEEP_AWAKE" in check
    assert "config.keep-awake.json must be config.json plus CONFIG_MEMORIA_KEEP_AWAKE=y" in check
    assert "this is not a keep-awake build" in check


def test_build_and_flash_reject_bench_and_keep_awake_together() -> None:
    for name in ("build.sh", "flash.sh"):
        completed = subprocess.run(
            [str(SCRIPTS / name), "--bench", "--keep-awake"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0
        assert "pass only one of --bench and --keep-awake" in completed.stderr
