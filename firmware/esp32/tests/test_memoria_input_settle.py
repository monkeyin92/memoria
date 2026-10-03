"""Host-side checks for the input settling window (firmware build 21, TODOLIST N-13).

`memoria_input_settle.h` has no ESP-IDF dependency, so the window arithmetic compiles on the host. The audio
service patch that arms it and drops blocks is checked as patch text, because it only builds inside ESP-IDF.

Why it exists: in round 10 (2026-10-02) the AFE's VAD fired within 0.2 s of every wake that had to power the
microphone up, three times at a loud level (rms 0.06-0.48), and the server held the wake greeting back for
3.3 s each time, taking the artifact for the child starting to speak. Two causes fit the serial log (the ADC's
start-up transient, and the listening cue ringing out of the speaker while the mic is already open); both are
short and both sit at the very start of the stream, so both are cut before the audio engine sees them.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
HEADER = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria" / "memoria_input_settle.h"
PATCH = FIRMWARE_ROOT / "overlay" / "patches" / "0033-memoria-input-settle.patch"

HARNESS = r"""
#include "memoria_input_settle.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

int main() {
    memoria::InputSettle settle;
    char line[256];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
        if (strcmp(line, "arm") == 0) {
            settle.Arm();
        } else if (strncmp(line, "arm ", 4) == 0) {
            settle.Arm(static_cast<uint32_t>(strtoul(line + 4, nullptr, 10)));
        } else if (strncmp(line, "cue ", 4) == 0) {
            settle.NoteLocalSound(strtoull(line + 4, nullptr, 10));
        } else if (strncmp(line, "drop ", 5) == 0) {
            // drop <block samples> <count> <start ms> <step ms>: how many of the blocks are dropped
            unsigned long samples = 0, count = 0;
            unsigned long long start = 0, step = 0;
            if (sscanf(line + 5, "%lu %lu %llu %llu", &samples, &count, &start, &step) != 4) {
                return 2;
            }
            unsigned long dropped = 0;
            for (unsigned long i = 0; i < count; ++i) {
                if (settle.ShouldDrop(static_cast<uint32_t>(samples), start + i * step)) {
                    ++dropped;
                }
            }
            printf("%lu\n", dropped);
        } else if (strcmp(line, "remaining") == 0) {
            printf("%u\n", settle.remaining());
        } else if (strcmp(line, "constants") == 0) {
            printf("%u %u\n", memoria::kInputSettleSamples, memoria::kLocalCueMuteMs);
        } else {
            return 2;
        }
        fflush(stdout);
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def tool():
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "input_settle_tool"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O0",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(HEADER.parent),
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        yield binary


def _run(tool: pathlib.Path, *lines: str) -> list[str]:
    completed = subprocess.run(
        [str(tool)], input="\n".join(lines) + "\n", text=True, capture_output=True, check=True
    )
    return completed.stdout.splitlines()


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "lv_", "freertos", "nvs", "settings.h", "driver/"):
        assert forbidden not in source
    assert re.findall(r'#include "([^"]+)"', source) == []


def test_the_windows_are_300_ms_of_input_and_800_ms_after_a_local_sound(tool: pathlib.Path) -> None:
    assert _run(tool, "constants") == ["4800 800"]


def test_a_stream_that_was_never_armed_and_heard_no_cue_drops_nothing(tool: pathlib.Path) -> None:
    assert _run(tool, "drop 160 50 5000 10", "remaining") == ["0", "0"]


def test_an_armed_window_drops_exactly_its_300_ms_of_10_ms_blocks(tool: pathlib.Path) -> None:
    assert _run(tool, "arm", "drop 160 100 5000 10", "remaining") == ["30", "0"]


def test_a_block_that_straddles_the_end_is_dropped_and_the_next_one_is_fed(tool: pathlib.Path) -> None:
    assert _run(tool, "arm 100", "drop 160 1 5000 10", "remaining", "drop 160 1 5010 10") == [
        "1",
        "0",
        "0",
    ]


def test_arming_again_restarts_the_window_after_a_power_cycle(tool: pathlib.Path) -> None:
    assert _run(
        tool, "arm", "drop 160 10 5000 10", "remaining", "arm", "remaining", "drop 160 100 6000 10"
    ) == ["10", "3200", "4800", "30"]


def test_a_zero_window_drops_nothing(tool: pathlib.Path) -> None:
    assert _run(tool, "arm 0", "drop 160 5 5000 10") == ["0"]


def test_a_local_cue_mutes_the_input_for_exactly_800_ms_and_then_ends_by_itself(
    tool: pathlib.Path,
) -> None:
    # Blocks every 10 ms from the moment the cue is queued: 80 of them are inside the window.
    assert _run(tool, "cue 1000", "drop 160 200 1000 10") == ["80"]
    # Blocks read once the window is over are not touched.
    assert _run(tool, "cue 1000", "drop 160 50 1800 10") == ["0"]


def test_a_second_cue_extends_the_window_it_never_shortens_it(tool: pathlib.Path) -> None:
    assert _run(tool, "cue 1000", "cue 1500", "drop 160 200 1000 10") == ["130"]


def test_the_two_windows_run_side_by_side_so_a_drop_never_outlasts_the_longer_one(
    tool: pathlib.Path,
) -> None:
    # Input powered up as the cue is queued: 30 settling blocks sit inside the 80-block cue window.
    assert _run(tool, "arm", "cue 1000", "drop 160 200 1000 10", "remaining") == ["80", "0"]
    # Input powered up 0.6 s after the cue: the settling window ends later than the cue's mute.
    assert _run(
        tool, "cue 1000", "drop 160 60 1000 10", "arm", "drop 160 100 1600 10"
    ) == ["60", "30"]


def test_the_audio_service_arms_when_it_powers_the_input_up_and_drops_before_the_engine() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    added = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    text = "\n".join(added)

    assert '#include "memoria_input_settle.h"' in text
    assert "memoria::InputSettle input_settle_;" in text
    # Armed right after the codec input is enabled, on the Memoria board only.
    assert re.search(
        r"#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT\n\s*input_settle_\.Arm\(\);\n#endif", text
    )
    # Every local sound notes itself first, before anything else in PlaySound.
    assert re.search(
        r"void AudioService::PlaySound\(const std::string_view& ogg\) \{\n"
        r"#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT\n\s*input_settle_\.NoteLocalSound\("
        r"static_cast<uint64_t>\(esp_timer_get_time\(\) / 1000\)\);\n#endif",
        patch.replace("\n+", "\n").replace("\n ", "\n"),
    )
    # Dropped in front of the engine feed, never the audio-testing queue.
    assert re.search(
        r"#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT\n\s*if \(input_settle_\.ShouldDrop\(samples, "
        r"static_cast<uint64_t>\(esp_timer_get_time\(\) / 1000\)\)\) \{\n"
        r"\s*continue;.*\n\s*\}\n#endif",
        text,
    )
    context_after_drop = patch.split("input_settle_.ShouldDrop(samples,")[1]
    assert "audio_engine_->Feed(std::move(data));" in context_after_drop
    assert "PushTaskToEncodeQueue" not in patch
