"""Host check for the software-reference versus mic delay estimate.

The header has no ESP-IDF dependency. A delayed copy of a broadband sequence
must come back as that lag. Silence must not produce an estimate.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

HEADER = pathlib.Path(__file__).parents[1] / "overlay" / "files" / "main" / "memoria" / "memoria_ref_mic_delay.h"
PATCH = pathlib.Path(__file__).parents[1] / "overlay" / "patches" / "0044-memoria-aec-delay-probe.patch"

HARNESS = r"""
#include "memoria_ref_mic_delay.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

static int16_t Noise(int t) {
    uint32_t x = static_cast<uint32_t>(t) * 1664525u + 1013904223u;
    return static_cast<int16_t>(x >> 16);
}

int main() {
    char line[128];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
        int ms = 0, lag = 0, amp = 0;
        if (sscanf(line, "feed %d %d %d", &ms, &lag, &amp) != 3 || ms <= 0) {
            return 2;
        }
        const int rate = 24000;
        const int total = rate * ms / 1000;
        const int frame = 240;
        int printed = 0;
        memoria::DelayEstimate last{};
        for (int at = 0; at < total; at += frame) {
            int16_t block[240 * 2];
            for (int i = 0; i < frame; ++i) {
                const int t = at + i;
                const int16_t ref = amp == 0 ? 0 : Noise(t);
                const int16_t mic = (amp == 0 || t < lag) ? 0 : Noise(t - lag);
                block[i * 2] = mic;
                block[i * 2 + 1] = ref;
            }
            memoria::DelayEstimate estimate{};
            if (memoria::ObserveRefMicDelay(block, frame, 2, rate, &estimate)) {
                last = estimate;
                ++printed;
            }
        }
        if (printed == 0) {
            printf("estimates=0\n");
        } else {
            printf("estimates=%d lag_ms=%d lag_samples=%d corr_milli=%d mic_rms=%d ref_rms=%d\n",
                   printed, last.lag_ms, last.lag_samples, last.corr_milli, last.mic_rms, last.ref_rms);
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
        binary = pathlib.Path(tmp) / "delay_tool"
        subprocess.run(
            [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(HEADER.parent), str(source), "-o", str(binary)],
            check=True,
        )
        yield binary


def _run(tool: pathlib.Path, *lines: str) -> list[str]:
    completed = subprocess.run([str(tool)], input="\n".join(lines) + "\n", text=True, capture_output=True, check=True)
    return completed.stdout.splitlines()


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "freertos", "driver/"):
        assert forbidden not in source


def test_a_20_ms_delayed_copy_is_reported_as_20_ms(tool: pathlib.Path) -> None:
    # 480 samples at 24 kHz is 20 ms. One second of audio is the report period.
    assert _run(tool, "feed 1200 480 1") == [
        "estimates=1 lag_ms=20 lag_samples=480 corr_milli=1000 mic_rms=14152 ref_rms=17490"
    ]


def test_zero_delay_is_zero(tool: pathlib.Path) -> None:
    assert _run(tool, "feed 1200 0 1") == [
        "estimates=1 lag_ms=0 lag_samples=0 corr_milli=1000 mic_rms=17490 ref_rms=17490"
    ]


def test_silence_and_a_short_burst_do_not_estimate(tool: pathlib.Path) -> None:
    assert _run(tool, "feed 1200 480 0", "feed 500 480 1") == ["estimates=0", "estimates=0"]


def test_audio_input_logs_the_estimate_after_the_reference_is_injected() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    assert patch.count("memoria::ObserveRefMicDelay(") == 2
    assert 'aec-ref delay lag_ms=%d lag_samples=%d corr=%.3f mic_rms=%d ref_rms=%d' in patch
    apply = patch.index("memoria::ApplyToFeed(")
    observe = patch.index("memoria::ObserveRefMicDelay(", apply)
    assert observe > apply
