"""Host-side checks for the software AEC reference ring (VoCat has no hardware ref).

`memoria_software_aec_ref.h` has no ESP-IDF dependency, so WritePlay / ApplyToFeed compile on the host.
The audio_service patch that wires them at 24 kHz (inject before resample) is checked as patch text.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
HEADER = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria" / "memoria_software_aec_ref.h"
PATCH = FIRMWARE_ROOT / "overlay" / "patches" / "0037-memoria-software-aec-reference.patch"

HARNESS = r"""
#include "memoria_software_aec_ref.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

int main() {
    char line[256];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
        if (strncmp(line, "write ", 6) == 0) {
            // write <n> <first>: n mono samples starting at first (ramp)
            unsigned long n = 0, first = 0;
            if (sscanf(line + 6, "%lu %lu", &n, &first) != 2) {
                return 2;
            }
            std::vector<int16_t> mono(n);
            for (unsigned long i = 0; i < n; ++i) {
                mono[i] = static_cast<int16_t>(first + i);
            }
            memoria::WritePlay(mono.data(), mono.size());
            printf("ok\n");
        } else if (strncmp(line, "apply ", 6) == 0) {
            // apply <frames> <channels> <mic0>: fill L with mic0+i, R with 0x7fff noise, ApplyToFeed, print R then L
            unsigned long frames = 0, channels = 0, mic0 = 0;
            if (sscanf(line + 6, "%lu %lu %lu", &frames, &channels, &mic0) != 3) {
                return 2;
            }
            std::vector<int16_t> interleaved(frames * channels, 0);
            for (unsigned long f = 0; f < frames; ++f) {
                interleaved[f * channels + 0] = static_cast<int16_t>(mic0 + f);
                if (channels >= 2) {
                    interleaved[f * channels + 1] = 0x7fff;
                }
            }
            memoria::ApplyToFeed(interleaved.data(), frames, static_cast<int>(channels));
            for (unsigned long f = 0; f < frames; ++f) {
                const int16_t r = channels >= 2 ? interleaved[f * channels + 1] : 0;
                printf("%d %d\n", static_cast<int>(r), static_cast<int>(interleaved[f * channels + 0]));
            }
        } else if (strcmp(line, "available") == 0) {
            printf("%zu\n", memoria::software_aec_ref.available());
        } else if (strcmp(line, "capacity") == 0) {
            printf("%zu\n", memoria::kSoftwareAecRefCapacity);
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
        binary = pathlib.Path(tmp) / "software_aec_ref_tool"
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


def test_capacity_covers_about_300_ms_at_24_khz(tool: pathlib.Path) -> None:
    capacity = int(_run(tool, "capacity")[0])
    assert 24000 * 200 // 1000 <= capacity <= 24000 * 400 // 1000


def test_write_then_apply_copies_play_into_channel_1_and_leaves_mic(tool: pathlib.Path) -> None:
    # 24 kHz-shaped: 240 mono play samples into 240 stereo frames.
    out = _run(tool, "write 240 1000", "apply 240 2 50")
    assert out[0] == "ok"
    for i, line in enumerate(out[1:]):
        ref, mic = line.split()
        assert int(ref) == 1000 + i
        assert int(mic) == 50 + i


def test_underrun_zeros_channel_1_and_still_leaves_mic(tool: pathlib.Path) -> None:
    out = _run(tool, "apply 8 2 7")
    assert out == [f"0 {7 + i}" for i in range(8)]


def test_channels_less_than_two_is_a_noop(tool: pathlib.Path) -> None:
    out = _run(tool, "write 4 9", "apply 4 1 3", "available")
    assert out[0] == "ok"
    assert out[1:5] == [f"0 {3 + i}" for i in range(4)]
    assert out[5] == "4"


def test_audio_service_patch_wires_write_before_output_and_apply_before_resample() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    added = [
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    text = "\n".join(added)

    assert '#include "memoria_software_aec_ref.h"' in text
    assert "memoria::WritePlay(" in text
    assert "memoria::ApplyToFeed(" in text
    assert "CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT" in text

    # WritePlay sits after RecordPlay / PublishPcm context and before OutputData.
    write_at = patch.index("memoria::WritePlay(")
    output_at = patch.index("codec_->OutputData(task->pcm);", write_at)
    assert output_at > write_at

    # ApplyToFeed is in ReadAudioData, immediately after InputData and before the resampler.
    apply_at = patch.index("memoria::ApplyToFeed(")
    assert "if (input_resampler_ != nullptr)" in patch[apply_at:]
    assert "codec_->InputData(data)" in patch[:apply_at]
