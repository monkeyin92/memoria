"""The AFE feed rate is 16 kHz once the codec resampler is open."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

HEADER = (
    pathlib.Path(__file__).parents[1]
    / "overlay"
    / "files"
    / "main"
    / "memoria"
    / "memoria_afe_input_rate.h"
)
PATCH = (
    pathlib.Path(__file__).parents[1] / "overlay" / "patches" / "0045-memoria-afe-input-rate.patch"
)

HARNESS = r"""
#include "memoria_afe_input_rate.h"

#include <cstdio>

int main() {
    printf("open=%d\n", memoria::AfeInputHz(24000, true));
    printf("closed=%d\n", memoria::AfeInputHz(24000, false));
    printf("native=%d\n", memoria::AfeInputHz(16000, false));
    printf("other=%d\n", memoria::AfeInputHz(48000, true));
    return 0;
}
"""


@pytest.fixture(scope="module")
def lines() -> list[str]:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "rate"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O2",
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
        completed = subprocess.run([str(binary)], text=True, capture_output=True, check=True)
        return completed.stdout.splitlines()


def test_a_24_khz_codec_feeds_the_afe_at_16_khz_when_the_resampler_opens(lines: list[str]) -> None:
    assert lines == ["open=16000", "closed=24000", "native=16000", "other=16000"]


def test_boot_logs_that_rate_and_the_afe_library_reports_its_own() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    assert "afe input hz=%d codec_hz=%d resampler=%d" in patch
    assert "memoria::AfeInputHz(codec->input_sample_rate(), input_resampler_ != nullptr)" in patch
    assert "afe_iface_->get_samp_rate(afe_data_)" in patch
    assert "hz: %d" in patch
