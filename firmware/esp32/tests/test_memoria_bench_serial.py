"""How the bench build's serial screenshot and status (TODOLIST M-2) are wired into the firmware sources.

The encoder, the status line and the PC scripts have their own tests; what only builds inside ESP-IDF is read
here as text. The point of these checks is the one rule of the feature: a product image has no way to put the
screen on the USB port. The QR card shows the binding payload, so the code that photographs it exists only
when CONFIG_MEMORIA_BENCH_SERIAL is set, and every place that mentions it is behind that switch.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MAIN = FIRMWARE_ROOT / "overlay" / "files" / "main"
BOARD = MAIN / "boards" / "memoria" / "esp-vocat"
PATCHES = FIRMWARE_ROOT / "overlay" / "patches"
SCRIPTS = FIRMWARE_ROOT / "scripts"

SOURCES = sorted(path for path in MAIN.rglob("*") if path.suffix in {".c", ".cc", ".h"})
BENCH_SOURCE = BOARD / "memoria_mascot_bench.cc"
DISPLAY_CC = BOARD / "memoria_mascot_display.cc"
DISPLAY_H = BOARD / "memoria_mascot_display.h"
BOARD_CC = BOARD / "memoria_esp_vocat.cc"
USB_HEADER = MAIN / "memoria" / "memoria_usb_command.h"

SWITCH = "CONFIG_MEMORIA_BENCH_SERIAL"

# The two headers that ARE the feature (host-compiled by their own tests, ESP-free). They are only ever included
# from a bench branch, which the "memoria_*.h" patterns below check; what they define is not "outside the switch".
BENCH_HEADERS = {BOARD / "memoria_mascot_status.h", MAIN / "memoria" / "memoria_bench_snap.h"}

# Everything that exists only for the bench image. None of it may appear in what a product build compiles.
BENCH_ONLY = [
    r"\bBenchSendSnapshot\b",
    r"\bBenchRequestStatus\b",
    r"\bBenchLogStatus\b",
    r"\bbench_status_requested_\b",
    r"\bBenchStatus\b",
    r"\bbench\.",
    r"\bHandleUsbSnap\b",
    r"\bHandleUsbStatus\b",
    r"\bUsbLineEvent::kSnap\b",
    r"\bUsbLineEvent::kStatus\b",
    r"\bUsbCommand::kSnap\b",
    r"\bUsbCommand::kStatus\b",
    r"\bmemoria_bench_build_marker\b",
    r"MEMORIA_BENCH_BUILD",
    r"\bMemoriaBench\b",
    r"memoria_mascot_status\.h",
    r"memoria_bench_snap\.h",
    r"\blv_snapshot_take\b",
    r"\bStreamPicture\b",
    r"O_WRONLY",
    r'"snap"',
    r'"status"',
]


def product_text(source: str) -> str:
    """What a product build compiles of `source`: every `#if CONFIG_MEMORIA_BENCH_SERIAL` branch removed, its
    `#else` branch kept. Other conditionals (and `#ifndef` of the switch itself) are left alone."""
    kept: list[str] = []
    stack: list[
        str
    ] = []  # "bench" inside a bench branch, "product" inside its #else, "other" otherwise
    for line in source.splitlines():
        directive = re.match(r"^\s*#\s*(\w+)\s*(.*)$", line)
        name, rest = (directive[1], directive[2].strip()) if directive else ("", "")
        if name in {"if", "ifdef", "ifndef"}:
            is_bench = name == "if" and rest == SWITCH
            stack.append("bench" if is_bench else "other")
            if is_bench or "bench" in stack:
                continue
        elif name == "elif" and stack and stack[-1] == "bench":
            raise AssertionError("#elif inside a bench branch is not handled: " + line)
        elif name == "else" and stack and stack[-1] in {"bench", "product"}:
            stack[-1] = "product"
            continue
        elif name == "endif":
            top = stack.pop()
            if top in {"bench", "product"} or "bench" in stack:
                continue
        if "bench" in stack:
            continue
        kept.append(line)
    assert not stack, "unbalanced conditionals"
    return "\n".join(kept)


def _strip_comments(source: str) -> str:
    return re.sub(r"//[^\n]*", "", source)


def _block(source: str, opening: str) -> str:
    """The text between the braces that follow `opening` (comments removed), nested braces balanced."""
    text = _strip_comments(source)
    start = text.index(opening)
    brace = (
        text.index("{", start + len(opening) - 1)
        if "{" not in opening
        else start + opening.rindex("{")
    )
    depth = 0
    for position in range(brace, len(text)):
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1 : position]
    raise AssertionError(f"no closing brace after {opening!r}")


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_product_view_of_a_file_drops_exactly_the_bench_branches() -> None:
    sample = "\n".join(
        [
            "a",
            f"#if {SWITCH}",
            "b",
            "#if OTHER",
            "c",
            "#endif",
            "d",
            "#else",
            "e",
            "#if OTHER2",
            "f",
            "#endif",
            "#endif",
            "g",
            f"#ifndef {SWITCH}",
            "h",
            "#endif",
            "#if OTHER3",
            "i",
            "#endif",
        ]
    )
    assert product_text(sample).split("\n") == [
        "a",
        "e",
        "#if OTHER2",
        "f",
        "#endif",
        "g",
        f"#ifndef {SWITCH}",
        "h",
        "#endif",
        "#if OTHER3",
        "i",
        "#endif",
    ]
    with pytest.raises(AssertionError):
        product_text(f"#if {SWITCH}\nb\n")


@pytest.mark.parametrize(
    "path",
    [path for path in SOURCES if path not in BENCH_HEADERS],
    ids=lambda path: str(path.relative_to(MAIN)),
)
def test_a_product_build_compiles_nothing_of_the_bench_feature(path: pathlib.Path) -> None:
    compiled = _strip_comments(product_text(_read(path)))
    for pattern in BENCH_ONLY:
        assert not re.search(pattern, compiled), f"{path.name}: {pattern} outside the bench switch"


@pytest.mark.parametrize("pattern", BENCH_ONLY)
def test_every_bench_only_name_is_still_somewhere_in_the_sources(pattern: str) -> None:
    # Otherwise the check above would pass for a name that was renamed and is now unguarded.
    assert any(re.search(pattern, _strip_comments(_read(path))) for path in SOURCES), pattern


def test_the_bench_source_is_an_empty_unit_in_a_product_build() -> None:
    code = [
        line.strip()
        for line in _strip_comments(product_text(_read(BENCH_SOURCE))).splitlines()
        if line.strip()
    ]
    assert code == ['#include "memoria_mascot_display.h"']
    # And the switch is defined before it is tested: the display header pulls sdkconfig.h in.
    assert '#include "sdkconfig.h"' in _read(DISPLAY_H)


def test_only_the_bench_source_ever_opens_the_console_for_writing() -> None:
    writers = [
        path.name for path in SOURCES if re.search(r"O_WRONLY|O_RDWR", _strip_comments(_read(path)))
    ]
    assert writers == [BENCH_SOURCE.name]
    assert _strip_comments(_read(BENCH_SOURCE)).count('open("/dev/secondary", O_WRONLY)') == 1
    # The command task itself stays a reader.
    assert 'open("/dev/secondary", O_RDONLY)' in _read(BOARD_CC)


def test_the_picture_is_written_whole_lines_straight_to_the_console_never_through_the_log() -> None:
    source = _read(BENCH_SOURCE)
    emit = _block(source, "auto emit = [&](const char* line, std::size_t length) {")
    assert "write(fd, framed, length + 1)" in emit
    assert "ESP_LOG" not in emit and "printf" not in emit
    # One write per line: the terminator is part of the buffer, so the console's lock covers the whole line.
    assert "framed[length] = '\\n';" in emit
    snapshot = _block(source, "void MemoriaMascotDisplay::BenchSendSnapshot() {")
    assert "StreamPicture(" in snapshot and ", emit)" in snapshot
    # The only log lines of the function are its own failure and summary lines, never picture data.
    for logged in re.findall(r"ESP_LOG[IWE]\(kTag, \"([^\"]*)\"", snapshot):
        assert logged.startswith("snap id=")


def test_the_console_gets_air_between_lines_so_the_task_watchdog_is_fed() -> None:
    source = _read(BENCH_SOURCE)
    assert re.search(r"constexpr std::size_t kLinesPerPause = \d+;", source)
    emit = _block(source, "auto emit = [&](const char* line, std::size_t length) {")
    assert re.search(r"if \(\+\+lines % kLinesPerPause == 0\) \{\s*vTaskDelay\(1\);", emit)


def test_the_display_lock_is_held_for_the_snapshot_only() -> None:
    body = _block(_read(BENCH_SOURCE), "void MemoriaMascotDisplay::BenchSendSnapshot() {")
    lock = body.index("Lock(1000)")
    take = body.index("lv_snapshot_take(lv_screen_active(), LV_COLOR_FORMAT_RGB565)")
    unlock = body.index("Unlock();")
    stream = body.index("StreamPicture(")
    assert lock < take < unlock < stream
    held = body[lock:unlock]
    assert "write(" not in held and "emit(" not in held and "StreamPicture" not in held
    # A picture that was taken is always released, whatever the wire did.
    assert body.index("lv_draw_buf_destroy(picture)") > stream
    assert body.index("close(fd)") > stream


def test_status_is_asked_by_a_flag_and_answered_by_the_animation_task() -> None:
    header = _read(DISPLAY_H)
    assert "void BenchRequestStatus() { bench_status_requested_.store(true); }" in header
    assert "std::atomic<bool> bench_status_requested_{false};" in header
    loop = _block(_read(DISPLAY_CC), "void MemoriaMascotDisplay::AnimationLoop() {")
    answer = loop.index("if (bench_status_requested_.exchange(false))")
    wait = loop.index("vTaskDelayUntil(&wake, interval);")
    assert answer < wait
    request = loop[answer:wait]
    assert "BenchLogStatus(bench);" in request
    for field in (
        "up_ms",
        "phase",
        "mood",
        "frame",
        "screen_off",
        "sleeping",
        "captioned",
        "extra_ms",
    ):
        assert f"bench.{field} =" in request, field
    # The counters the status line reports are the ones the loop keeps itself.
    for counter in (
        "++bench.frames",
        "++bench.drawn",
        "bench.render_us +=",
        "bench.busy_us +=",
        "bench.pixels +=",
        "bench.composed_px +=",
    ):
        assert counter in loop, counter
    assert loop.count("BenchLogStatus(") == 1


def test_every_bench_statement_of_the_animation_loop_sits_behind_the_switch() -> None:
    loop = _block(_read(DISPLAY_CC), "void MemoriaMascotDisplay::AnimationLoop() {")
    compiled = product_text("void f() {\n" + loop + "\n}")
    assert "bench" not in compiled and "Bench" not in compiled


def test_the_status_log_line_is_written_by_the_animation_task_only() -> None:
    callers = [
        path.name
        for path in SOURCES
        if path.suffix == ".cc"
        and path != BENCH_SOURCE
        and re.search(r"\bBenchLogStatus\(\w", _strip_comments(_read(path)))
    ]
    assert callers == [DISPLAY_CC.name]


def test_the_bench_manifest_is_the_product_manifest_plus_the_two_bench_options() -> None:
    product = json.loads(_read(BOARD / "config.json"))
    bench = json.loads(_read(BOARD / "config.bench.json"))
    product_options = product["builds"][0]["sdkconfig_append"]
    assert not any("BENCH" in option or "SNAPSHOT" in option for option in product_options)
    assert bench["builds"][0]["sdkconfig_append"] == [
        *product_options,
        "CONFIG_MEMORIA_BENCH_SERIAL=y",
        "CONFIG_LV_USE_SNAPSHOT=y",
    ]
    bench["builds"][0]["sdkconfig_append"] = product_options
    assert bench == product  # the build name stays the board's: the protocol hello reports it


def test_the_kconfig_switch_defaults_off_and_belongs_to_the_memoria_board() -> None:
    added = "\n".join(
        line[1:]
        for line in _read(PATCHES / "0035-memoria-bench-serial.patch").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    block = added.split("config MEMORIA_BENCH_SERIAL\n", 1)[1]
    assert "bool " in block.split("help", 1)[0]
    assert "depends on BOARD_TYPE_MEMORIA_ESP_VOCAT" in block
    assert re.search(r"^\s*default n$", block, re.MULTILINE)


def test_the_marker_the_publisher_refuses_is_defined_once_and_logged_at_boot() -> None:
    definitions = [
        path.name
        for path in SOURCES
        if re.search(r'memoria_bench_build_marker\[\] = "MEMORIA_BENCH_BUILD=1;"', _read(path))
    ]
    assert definitions == [BENCH_SOURCE.name]
    boot_log = _read(BOARD_CC)
    assert (
        '"usb command console ready commands=wake,snap,status %s", memoria_bench_build_marker'
        in boot_log
    )


@pytest.mark.parametrize("script", ["build.sh", "flash.sh", "check-overlay.sh"])
def test_the_shell_scripts_still_parse(script: str) -> None:
    subprocess.run(["bash", "-n", str(SCRIPTS / script)], check=True)


def test_the_scripts_keep_a_bench_image_and_a_product_image_apart() -> None:
    build = _read(SCRIPTS / "build.sh")
    flash = _read(SCRIPTS / "flash.sh")
    # build.sh: the artifact name carries "-bench", the bench manifest is passed explicitly, and the generated
    # sdkconfig is checked both ways before anything is copied.
    assert 'artifact_name="$MEMORIA_BOARD_NAME-bench"' in build
    assert "build_args+=(--config config.bench.json)" in build
    assert "CONFIG_MEMORIA_BENCH_SERIAL=y" in build
    assert build.index("generated sdkconfig lacks") < build.index('cp "$merged"')
    assert build.index("product build carries") < build.index('cp "$merged"')
    # flash.sh: what is in the tree must be what was asked for, in both directions.
    assert "the build in the tree is a BENCH image" in flash
    assert "--bench was given but the build in the tree is a product image" in flash
    assert flash.index("tree_is_bench") < flash.index("idf.py -p")
