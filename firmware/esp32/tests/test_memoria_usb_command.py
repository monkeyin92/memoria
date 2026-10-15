"""Host-side checks for USB `wake` / `sleep` / `poweroff` and the bench-only `snap`/`status`.

`memoria_usb_command.h` has no ESP-IDF dependency, so the exact parser, line assembler and wake policy
compile on the host, in both configurations: a product image (`CONFIG_MEMORIA_BENCH_SERIAL` off) and a bench
build (on). The task that reads the port and the board handler are checked as source text, because those
only build inside ESP-IDF.

Product rules: computer-driven tests wake over USB; `sleep` / `poweroff` stop speaking/listening and park
without enabling chip deep sleep (USB still powers the board). A bench build also answers read-only
`snap` / `status` / pcm verbs.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
HEADER = MEMORIA_DIR / "memoria_usb_command.h"
BOARD = BOARD_DIR / "memoria_esp_vocat.cc"

HARNESS = r"""
#include "memoria_usb_command.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

static const char* EventName(memoria::UsbLineEvent event) {
    switch (event) {
        case memoria::UsbLineEvent::kNone: return nullptr;
        case memoria::UsbLineEvent::kWake: return "wake";
        case memoria::UsbLineEvent::kSleep: return "sleep";
        case memoria::UsbLineEvent::kPowerOff: return "poweroff";
        case memoria::UsbLineEvent::kUnknown: return "unknown";
        case memoria::UsbLineEvent::kTooLong: return "too-long";
#if CONFIG_MEMORIA_BENCH_SERIAL
        case memoria::UsbLineEvent::kSnap: return "snap";
        case memoria::UsbLineEvent::kStatus: return "status";
        case memoria::UsbLineEvent::kPcmArm: return "pcmarm";
        case memoria::UsbLineEvent::kPcmDump: return "pcmdump";
#endif
    }
    return nullptr;
}

static const char* CommandName(memoria::UsbCommand command) {
    switch (command) {
        case memoria::UsbCommand::kUnknown: return "unknown";
        case memoria::UsbCommand::kWake: return "wake";
        case memoria::UsbCommand::kSleep: return "sleep";
        case memoria::UsbCommand::kPowerOff: return "poweroff";
#if CONFIG_MEMORIA_BENCH_SERIAL
        case memoria::UsbCommand::kSnap: return "snap";
        case memoria::UsbCommand::kStatus: return "status";
        case memoria::UsbCommand::kPcmArm: return "pcmarm";
        case memoria::UsbCommand::kPcmDump: return "pcmdump";
#endif
    }
    return "unknown";
}

int main() {
    memoria::UsbLineAssembler assembler;
    char line[1024];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        }
        if (strncmp(line, "parse ", 6) == 0 || strcmp(line, "parse") == 0) {
            const char* text = line[5] == '\0' ? "" : line + 6;
            printf("%s\n", CommandName(memoria::ParseUsbCommand(text, strlen(text))));
        } else if (strcmp(line, "null") == 0) {
            printf("%s\n", CommandName(memoria::ParseUsbCommand(nullptr, 3)));
        } else if (strcmp(line, "config") == 0) {
            printf("%d\n", CONFIG_MEMORIA_BENCH_SERIAL);
        } else if (strcmp(line, "stack") == 0) {
            printf("%zu\n", memoria::kUsbTaskStackBytes);
        } else if (strncmp(line, "feed ", 5) == 0) {
            // feed <now_ms> <hex byte> <hex byte> ...
            char* cursor = line + 5;
            unsigned long now = strtoul(cursor, &cursor, 10);
            bool any = false;
            while (*cursor != '\0') {
                while (*cursor == ' ') {
                    ++cursor;
                }
                if (*cursor == '\0') {
                    break;
                }
                unsigned long byte = strtoul(cursor, &cursor, 16);
                const char* name =
                    EventName(assembler.Feed(static_cast<std::uint8_t>(byte), static_cast<std::uint32_t>(now)));
                if (name != nullptr) {
                    printf("%s%s", any ? " " : "", name);
                    any = true;
                }
            }
            printf("%s\n", any ? "" : "-");
        } else if (strcmp(line, "pending") == 0) {
            printf("%zu\n", assembler.pending());
        } else if (strncmp(line, "decide ", 7) == 0) {
            char name[64];
            int idle = 0;
            if (sscanf(line + 7, "%63s %d", name, &idle) != 2) {
                return 2;
            }
            memoria::WakeMode mode;
            if (!memoria::ParseWakeMode(name, &mode)) {
                return 3;
            }
            printf("%s %s\n", memoria::UsbWakeDecisionName(memoria::UsbWakeDecisionFor(mode, idle != 0)),
                   memoria::TapActionFor(mode, idle != 0) == memoria::TapAction::kStartConversation
                       ? "tap-starts"
                       : "tap-ignores");
        } else {
            return 2;
        }
        fflush(stdout);
    }
    return 0;
}
"""


def _compile(destination: pathlib.Path, *flags: str, source: str = HARNESS) -> subprocess.CompletedProcess:
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    code = destination.parent / f"{destination.name}.cc"
    code.write_text(source, encoding="utf-8")
    return subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O0",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(MEMORIA_DIR),
            *flags,
            str(code),
            "-o",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _build(destination: pathlib.Path, *flags: str) -> pathlib.Path:
    completed = _compile(destination, *flags)
    assert completed.returncode == 0, completed.stderr
    return destination


@pytest.fixture(scope="module")
def tool():
    """The parser as a product image has it: the bench switch off."""
    with tempfile.TemporaryDirectory() as tmp:
        yield _build(pathlib.Path(tmp) / "usb_command_tool", "-DCONFIG_MEMORIA_BENCH_SERIAL=0")


@pytest.fixture(scope="module")
def bench_tool():
    """The parser of a bench build: the same code with CONFIG_MEMORIA_BENCH_SERIAL on."""
    with tempfile.TemporaryDirectory() as tmp:
        yield _build(pathlib.Path(tmp) / "usb_command_bench_tool", "-DCONFIG_MEMORIA_BENCH_SERIAL=1")


def _run(tool: pathlib.Path, *lines: str) -> list[str]:
    completed = subprocess.run(
        [str(tool)], input="\n".join(lines) + "\n", text=True, capture_output=True, check=True
    )
    return completed.stdout.splitlines()


def _hex(text: str) -> str:
    return " ".join(f"{byte:02x}" for byte in text.encode("latin-1"))


def _feed(now_ms: int, text: str) -> str:
    return f"feed {now_ms} {_hex(text)}"


def _function(source: str, signature: str) -> str:
    """The body of the first function whose definition starts with `signature`."""
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace : index + 1]
    raise AssertionError(f"unterminated function {signature}")


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "lv_", "freertos", "nvs", "settings.h", "driver/"):
        assert forbidden not in source
    # The only project header it needs is the wake policy it shares with the screen tap, plus the generated
    # sdkconfig.h that carries the bench switch (guarded: the host tests have none).
    assert re.findall(r'#include "([^"]+)"', source) == ["sdkconfig.h", "memoria_wake_mode.h"]
    assert '#if __has_include("sdkconfig.h")' in source


def test_only_the_exact_product_verbs_are_commands(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "parse wake",
        "parse   wake",
        "parse wake  ",
        "parse \twake\t",
        "parse sleep",
        "parse   sleep  ",
        "parse poweroff",
        "parse \tpoweroff\t",
    ) == ["wake"] * 4 + ["sleep"] * 2 + ["poweroff"] * 2
    # A stray terminal, a pasted log line or line noise cannot wake the robot.
    assert _run(
        tool,
        "parse",
        "parse Wake",
        "parse WAKE",
        "parse wakeup",
        "parse wake up",
        "parse wake wake",
        "parse w ake",
        "parse wak",
        "parse xwake",
        "parse screen_tap",
        "parse Sleep",
        "parse sleep now",
        "parse power-off",
        "parse power off",
        "parse I (1234) MemoriaUsbCommand: usb wake accepted",
        "null",
    ) == ["unknown"] * 16


def test_cr_lf_and_crlf_all_end_a_line(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        _feed(0, "wake\n"),
        _feed(0, "wake\r"),
        _feed(0, "wake\r\n"),
        _feed(0, "\r\n"),
        _feed(0, "\n\n"),
    ) == ["wake", "wake", "wake", "-", "-"]


def test_lines_assemble_across_reads_and_bursts(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        _feed(0, "wa"),
        "pending",
        _feed(10, "ke\n"),
        "pending",
        _feed(20, "wake\nwake\n"),
        _feed(30, "foo\nwake\n"),
        _feed(40, "wake now\n"),
    ) == ["-", "2", "wake", "0", "wake wake", "unknown wake", "unknown"]


def test_a_line_that_does_not_fit_is_dropped_whole_and_the_next_one_starts_clean(
    tool: pathlib.Path,
) -> None:
    exactly_full = "wake" + " " * 28  # 32 bytes: the longest line that fits, and it is the command
    one_too_long = "wake" + " " * 29  # 33 bytes: dropped, even though its first 32 bytes parse as `wake`
    assert len(exactly_full) == 32 and len(one_too_long) == 33
    assert _run(
        tool,
        _feed(0, exactly_full + "\n"),
        _feed(0, one_too_long + "\n"),
        _feed(0, "wake\n"),
        _feed(0, "x" * 200 + "\n"),
        _feed(0, "wake\n"),
        # Terminators inside the dropped line end it: what follows is a fresh line.
        _feed(0, "x" * 40 + "\nwake\n"),
    ) == ["wake", "too-long", "wake", "too-long", "wake", "too-long wake"]


def test_binary_noise_is_not_a_command_and_does_not_poison_the_next_line(
    tool: pathlib.Path,
) -> None:
    assert _run(
        tool,
        "feed 0 00 ff 80 1b 5b 41 0a",  # NUL, high bytes, an ANSI escape
        _feed(0, "wake\n"),
        "feed 0 77 61 6b 65 00 0a",  # "wake" followed by a NUL is not "wake"
        "feed 0 77 00 61 6b 65 0a",
    ) == ["unknown", "wake", "unknown", "unknown"]


def test_a_half_written_line_expires_so_a_late_tail_cannot_complete_it(
    tool: pathlib.Path,
) -> None:
    assert _run(
        tool,
        _feed(0, "wak"),
        _feed(1500, "e\n"),  # inside the window: the command completes
        _feed(10_000, "wak"),
        _feed(12_000, "e\n"),  # exactly 2000 ms after the first byte: still inside
        _feed(20_000, "wak"),
        _feed(22_001, "e\n"),  # 2001 ms: the "wak" was dropped, "e" is a line of its own
        _feed(30_000, "wake\n"),
    ) == ["-", "wake", "-", "wake", "-", "unknown", "wake"]


def test_the_stale_window_survives_the_millisecond_counter_wrapping(tool: pathlib.Path) -> None:
    start = 2**32 - 100
    assert _run(
        tool,
        _feed(start, "wak"),
        _feed(96, "e\n"),  # 196 ms later, across the wrap
        _feed(start, "wak"),
        _feed(1900, "e\n"),  # exactly 2000 ms later, across the wrap: still inside the window
        _feed(start, "wak"),
        _feed(1901, "e\n"),  # 2001 ms later, across the wrap: expired
    ) == ["-", "wake", "-", "wake", "-", "unknown"]


def test_the_wake_policy_is_the_screen_taps_gate_in_every_mode_and_state(tool: pathlib.Path) -> None:
    # Tap-only product: legacy keyword wire values coerce to button; idle starts.
    lines = [
        f"decide {mode} {idle}"
        for mode in ("keyword", "button", "button_or_keyword")
        for idle in (0, 1)
    ]
    results = _run(tool, *lines)
    assert results == [
        "not_idle tap-ignores",
        "start tap-starts",
        "not_idle tap-ignores",
        "start tap-starts",
        "not_idle tap-ignores",
        "start tap-starts",
    ]
    for result in results:
        decision, tap = result.split()
        assert (decision == "start") == (tap == "tap-starts")


def test_a_product_image_knows_no_bench_verbs(tool: pathlib.Path) -> None:
    assert _run(tool, "config") == ["0"]
    assert _run(
        tool,
        "parse snap",
        "parse status",
        "parse pcmarm",
        "parse pcmdump",
        _feed(0, "snap\n"),
        _feed(0, "status\r\n"),
        _feed(0, "snap\nwake\nstatus\n"),
        _feed(0, "pcmarm\nwake\npcmdump\n"),
    ) == ["unknown"] * 6 + ["unknown wake unknown"] * 2


def test_the_task_stack_grows_only_in_a_bench_build(tool: pathlib.Path, bench_tool: pathlib.Path) -> None:
    # The product's task only assembles lines; the bench's also runs LVGL's software renderer for `snap`.
    assert _run(tool, "stack") == [str(4 * 1024)]
    assert _run(bench_tool, "stack") == [str(12 * 1024)]


def test_a_product_image_cannot_even_name_the_bench_commands(tmp_path: pathlib.Path) -> None:
    snippet = '#include "memoria_usb_command.h"\nint main() { return static_cast<int>(memoria::%s); }\n'
    for name in (
        "UsbCommand::kSnap",
        "UsbCommand::kStatus",
        "UsbCommand::kPcmArm",
        "UsbCommand::kPcmDump",
        "UsbLineEvent::kSnap",
        "UsbLineEvent::kStatus",
        "UsbLineEvent::kPcmArm",
        "UsbLineEvent::kPcmDump",
    ):
        source = snippet % name
        product = _compile(tmp_path / "product", "-DCONFIG_MEMORIA_BENCH_SERIAL=0", source=source)
        assert product.returncode != 0, name
        assert name.split("::")[1] in product.stderr
        # The same line compiles in a bench build, so the refusal above is the missing enumerator.
        bench = _compile(tmp_path / "bench", "-DCONFIG_MEMORIA_BENCH_SERIAL=1", source=source)
        assert bench.returncode == 0, bench.stderr


def test_a_bench_build_reads_the_pcm_verbs_exactly(bench_tool: pathlib.Path) -> None:
    assert _run(
        bench_tool,
        "parse pcmarm",
        "parse  pcmarm\t",
        "parse pcmdump",
        "parse \tpcmdump ",
    ) == ["pcmarm", "pcmarm", "pcmdump", "pcmdump"]
    # No arguments ride the line: the window is compiled in and the dump has no options.
    assert _run(
        bench_tool,
        "parse PCMARM",
        "parse pcmarm 3000",
        "parse pcmar",
        "parse pcmrec",
        "parse pcmdumpx",
        "parse pcmdump now",
        "parse pcm",
        "parse PCMD 0001a2b3 failed reason=empty",
    ) == ["unknown"] * 8
    assert _run(
        bench_tool,
        _feed(0, "pcmarm\r\n"),
        _feed(0, "pcm"),
        _feed(10, "dump\n"),
        _feed(0, "pcmarm\nstatus\npcmdump\n"),
    ) == ["pcmarm", "-", "pcmdump", "pcmarm status pcmdump"]


def test_a_bench_build_reads_the_two_extra_verbs_as_strictly_as_wake(bench_tool: pathlib.Path) -> None:
    assert _run(bench_tool, "config") == ["1"]
    assert _run(
        bench_tool,
        "parse snap",
        "parse   snap  ",
        "parse \tsnap\t",
        "parse status",
        "parse  status ",
        "parse wake",
    ) == ["snap", "snap", "snap", "status", "status", "wake"]
    # Exact verbs: nothing after them, no other case, no noise, no log line pasted back.
    assert _run(
        bench_tool,
        "parse",
        "parse Snap",
        "parse SNAP",
        "parse snapshot",
        "parse snap now",
        "parse snap snap",
        "parse sna",
        "parse xsnap",
        "parse Status",
        "parse statuses",
        "parse status now",
        "parse stat",
        "parse snapstatus",
        "parse snap status",
        "parse SNAP 00001234 360x360 1/675 deadbeef AAAA",
        "null",
    ) == ["unknown"] * 16


def test_a_bench_build_assembles_the_extra_verbs_like_any_other_line(bench_tool: pathlib.Path) -> None:
    assert _run(
        bench_tool,
        _feed(0, "snap\n"),
        _feed(0, "status\r\n"),
        _feed(0, "snap\nstatus\nwake\n"),
        _feed(0, "sna"),
        _feed(10, "p\n"),
        _feed(0, "snap" + " " * 29 + "\n"),  # 33 bytes: dropped, although its first 32 parse as `snap`
        _feed(0, "stat"),
        _feed(5000, "us\n"),  # the half line expired: "us" is a line of its own
    ) == ["snap", "status", "snap status wake", "-", "snap", "too-long", "-", "unknown"]


def test_the_wake_policy_and_the_line_rules_do_not_depend_on_the_bench_switch(
    tool: pathlib.Path, bench_tool: pathlib.Path
) -> None:
    script = [
        _feed(0, "wake\n"),
        _feed(0, "wake\r\n"),
        _feed(0, "x" * 200 + "\n"),
        _feed(0, "x" * 40 + "\nwake\n"),
        "feed 0 00 ff 80 1b 5b 41 0a",
        "feed 0 77 61 6b 65 00 0a",
        _feed(0, "wak"),
        _feed(2001, "e\n"),
        "decide keyword 0",
        "decide keyword 1",
        "decide button 1",
        "decide button_or_keyword 0",
        "decide button_or_keyword 1",
    ]
    assert _run(tool, *script) == _run(bench_tool, *script)


@pytest.mark.parametrize(
    ("sdkconfig", "bench"),
    [
        ("#define CONFIG_MEMORIA_BENCH_SERIAL 1\n", True),
        ("", False),  # an option that is off is not defined at all
        ("#define CONFIG_SOMETHING_ELSE 1\n", False),
    ],
)
def test_the_switch_comes_from_the_sdkconfig_esp_idf_generates(
    tmp_path: pathlib.Path, sdkconfig: str, bench: bool
) -> None:
    include = tmp_path / "config"
    include.mkdir()
    (include / "sdkconfig.h").write_text(sdkconfig, encoding="utf-8")
    binary = _build(tmp_path / "from_sdkconfig", "-I", str(include))
    expected = ["1", "snap", "status"] if bench else ["0", "unknown", "unknown"]
    assert _run(binary, "config", "parse snap", "parse status") == expected


def test_board_handler_only_wakes_an_idle_device_under_the_taps_gate() -> None:
    source = BOARD.read_text(encoding="utf-8")
    handler = _function(source, "void HandleUsbWake()")
    # Pairing and start-up come first and keep their behaviour, as for a tap.
    assert handler.index("MemoriaBootstrap") < handler.index("UsbWakeDecisionFor")
    assert handler.index("kDeviceStateStarting") < handler.index("UsbWakeDecisionFor")
    assert "WakeModeRegistry::GetInstance().mode()" in handler
    assert "UsbWakeDecisionFor(mode, state == kDeviceStateIdle)" in handler
    # The application is invoked once, only on the accepted branch, with its own source name.
    assert handler.count("WakeWordInvoke(") == 1
    assert 'app.WakeWordInvoke("usb_wake")' in handler
    accepted = handler[handler.index("kStartConversation") :]
    assert accepted.index("usb wake accepted") < accepted.index("WakeWordInvoke")
    assert "usb wake ignored" in handler
    # It can wake, nothing else: no stop, abort, close, toggle, reboot or state change.
    for forbidden in (
        "StopListening",
        "AbortSpeaking",
        "ToggleChatState",
        "CloseAudioChannel",
        "SetDeviceState",
        "Reboot",
        "Settings",
        "Apply(",
    ):
        assert forbidden not in handler, f"the USB wake must not call {forbidden}"


def test_the_task_only_reads_the_secondary_console_and_product_verbs_reach_handlers() -> None:
    source = BOARD.read_text(encoding="utf-8")
    task = _function(source, "static void usb_command_task(void* arg)")
    # Read-only device node of the secondary (USB-Serial-JTAG) console: no driver, no interrupt, no TX.
    assert 'open("/dev/secondary", O_RDONLY)' in task
    for forbidden in ("write(", "fwrite", "printf(", "usb_serial_jtag_", "O_WRONLY", "O_RDWR"):
        assert forbidden not in task, f"the command task must not use {forbidden}"
    assert "memoria::UsbLineAssembler assembler;" in task
    wake_case = task.index("case memoria::UsbLineEvent::kWake:")
    assert task.index("self->HandleUsbWake()") > wake_case
    assert task.count("HandleUsbWake()") == 1
    assert "case memoria::UsbLineEvent::kSleep:" in task
    assert "case memoria::UsbLineEvent::kPowerOff:" in task
    assert "HandleUsbSleepOrPowerOff(false)" in task
    assert "HandleUsbSleepOrPowerOff(true)" in task
    # A failed open ends the task instead of spinning.
    assert "vTaskDelete(NULL)" in task[: task.index("ready commands=wake,sleep,poweroff")]
    assert "ready commands=wake,sleep,poweroff" in source
    # The poll yields every round, so the loop can never starve a lower-priority task.
    assert "vTaskDelay(pdMS_TO_TICKS(kUsbPollMs))" in task
    assert "kUsbPollMs = 25" in source


def test_usb_sleep_and_poweroff_stop_without_deep_sleep() -> None:
    source = BOARD.read_text(encoding="utf-8")
    handler = _function(source, "void HandleUsbSleepOrPowerOff(bool stop_wifi)")
    assert "AbortSpeaking" in handler
    assert "StopListening" in handler
    assert "ToggleChatState" in handler
    assert "PowerPhase::kWifiHold" in handler
    assert "kWifiHoldNotArmed" in handler
    assert "esp_deep_sleep_start" not in handler
    assert "StopStation" in handler
    assert "USB still powers the board" in handler


def test_the_task_is_started_with_the_board_at_low_priority_off_the_audio_core() -> None:
    source = BOARD.read_text(encoding="utf-8")
    start = _function(source, "void InitializeUsbCommand()")
    assert re.search(
        r'xTaskCreatePinnedToCore\(usb_command_task, "usb_cmd", memoria::kUsbTaskStackBytes, this, 1,\s*'
        r"&usb_command_task_handle_, 0\)",
        start,
    )
    constructor = _function(source, "MemoriaEspVocat() : boot_button_(BOOT_BUTTON_GPIO)")
    assert constructor.index("InitializeButtons();") < constructor.index("InitializeUsbCommand();")
    destructor = _function(source, "~MemoriaEspVocat()")
    assert "vTaskDelete(usb_command_task_handle_)" in destructor


def test_the_command_does_not_leak_into_other_boards_or_the_network_stack() -> None:
    # The board file is the only place that reads the port; the protocol code never sees it.
    for path in MEMORIA_DIR.glob("*.cc"):
        assert "/dev/secondary" not in path.read_text(encoding="utf-8"), path.name
    patches = (FIRMWARE_ROOT / "overlay" / "patches").glob("*.patch")
    for patch in patches:
        assert "usb_wake" not in patch.read_text(encoding="utf-8"), patch.name


def test_build_number_moved_past_the_last_flashed_image() -> None:
    release = (MEMORIA_DIR / "memoria_firmware_release.h").read_text(encoding="utf-8")
    build = int(re.search(r"#define MEMORIA_FIRMWARE_BUILD (\d+)", release).group(1))
    assert build >= 20
