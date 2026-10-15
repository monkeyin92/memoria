"""The bench 1 kHz probe is a sine, not a spoken line, and only a bench image plays it."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

HEADER = pathlib.Path(__file__).parents[1] / "overlay" / "files" / "main" / "memoria" / "memoria_bench_tone.h"
BOARD = (
    pathlib.Path(__file__).parents[1]
    / "overlay"
    / "files"
    / "main"
    / "boards"
    / "memoria"
    / "esp-vocat"
    / "memoria_esp_vocat.cc"
)

HARNESS = r"""
#include "memoria_bench_tone.h"

#include <cstdint>
#include <cstdio>

int main() {
    int16_t samples[48];
    uint32_t phase = 0;
    const size_t wrote = memoria::FillBenchTone(samples, 48, 24000, 1000, &phase, 1000);
    printf("wrote=%zu phase=%u\n", wrote, phase);
    for (int i = 0; i < 24; ++i) {
        printf("%d\n", static_cast<int>(samples[i]));
    }
    printf("period=%d\n", samples[0] == samples[24] && samples[6] == samples[30] ? 1 : 0);
    return 0;
}
"""


@pytest.fixture(scope="module")
def samples() -> list[str]:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "tone"
        subprocess.run(
            [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(HEADER.parent), str(source), "-o", str(binary)],
            check=True,
        )
        completed = subprocess.run([str(binary)], text=True, capture_output=True, check=True)
        return completed.stdout.splitlines()


def test_one_khz_at_24_khz_is_one_cycle_every_24_samples(samples: list[str]) -> None:
    assert samples[0] == "wrote=48 phase=48"
    assert [samples[1], samples[7], samples[13], samples[19]] == ["0", "1000", "0", "-1000"]
    assert samples[-1] == "period=1"


def test_the_bench_command_plays_that_sine_while_the_codec_is_read() -> None:
    source = BOARD.read_text(encoding="utf-8")
    handler = source[source.index("void HandleUsbTone()") : source.index("void usb_command_task")]
    assert "memoria::FillBenchTone(" in handler
    assert "memoria::diag::Arm();" in handler
    assert "memoria::diag::RecordPlay(" in handler
    assert "memoria::WritePlay(" in handler
    assert "codec->OutputData(play);" in handler
    assert "codec->InputData(mic);" in handler
    assert 'usb tone ignored reason=busy' in handler
    assert 'usb tone ignored reason=mic_open' in handler
    assert 'ESP_LOGI(kUsbTag, "usb tone hz=1000 rate=%d bits=16 frames=%d raw_slots=4"' in handler
    assert source.index("#if CONFIG_MEMORIA_BENCH_SERIAL") < source.index("void HandleUsbTone()")
