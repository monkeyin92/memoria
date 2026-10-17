from __future__ import annotations

import os
import pathlib
import subprocess


FIRMWARE = pathlib.Path(__file__).parents[1]
OVERLAY = FIRMWARE / "overlay"


def test_audio_input_shape_helper_is_safe_for_16k_and_24k_mono_and_stereo(
    tmp_path: pathlib.Path,
) -> None:
    source = tmp_path / "audio_buffer_test.cc"
    source.write_text(
        """
#include <cassert>
#include <vector>
#include "memoria_audio_buffer.h"

int main() {
    for (int requested_rate : {16000, 24000}) {
        for (int codec_rate : {16000, 24000}) {
            for (int channels : {1, 2}) {
                const auto frames = memoria::InputFramesAtCodecRate(
                    160, requested_rate, codec_rate);
                const auto samples = memoria::InterleavedInputSamples(frames, channels);
                std::vector<short> pcm(samples, 0);
                assert(memoria::InputReadFits(pcm.size(), frames, channels));
                assert(!memoria::InputReadFits(
                    pcm.size() == 0 ? 0 : pcm.size() - 1, frames, channels));
                const auto bounded_reader = [&pcm](short* destination, std::size_t capacity) {
                    assert(destination == pcm.data());
                    assert(capacity == pcm.size());
                    for (std::size_t i = 0; i < capacity; ++i) {
                        destination[i] = static_cast<short>(i);
                    }
                    return static_cast<int>(capacity);
                };
                const auto read_samples = bounded_reader(pcm.data(), pcm.size());
                assert(memoria::ReadSamplesFit(pcm.size(), read_samples));
                assert(!memoria::ReadSamplesFit(pcm.size(), read_samples + 1));
            }
        }
    }
    static_assert(memoria::InputFramesAtCodecRate(160, 16000, 24000) == 240);
    static_assert(memoria::InterleavedInputSamples(240, 2) == 480);
}
""",
        encoding="utf-8",
    )
    binary = tmp_path / "audio_buffer_test"
    subprocess.run(
        [
            "c++",
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fsanitize=address",
            "-fno-omit-frame-pointer",
            f"-I{OVERLAY / 'files' / 'main' / 'memoria'}",
            str(source),
            "-o",
            str(binary),
        ],
        check=True,
    )
    subprocess.run(
        [str(binary)],
        check=True,
        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0"},
    )


def test_audio_source_keeps_codec_tdm_shape_and_guards_audio_service() -> None:
    codec = (OVERLAY / "patches" / "0041-memoria-tap-heap-guard.patch").read_text(
        encoding="utf-8"
    )
    assert "diff --git a/main/audio/codecs/box_audio_codec.cc" not in codec
    assert "memoria_audio_buffer.h" in codec
    assert "const int codec_channels = codec_->input_channels();" in codec
    assert "data.resize(input_samples);" in codec
    assert "InterleavedInputSamples" in codec
    assert "InputReadFits" in codec
    assert "ReadSamplesFit" in codec
    assert "data.size() > input_samples" in codec
    assert "data.size() % static_cast<std::size_t>(codec_channels) != 0" in codec
    assert 'ESP_LOGW(TAG, "dropping input frame' in codec


def test_overlay_patch_is_numbered_after_encode_queue_budget() -> None:
    patches = sorted((OVERLAY / "patches").glob("*.patch"))
    names = [path.name for path in patches]
    assert names[-1] == "0041-memoria-tap-heap-guard.patch"
    assert names.index("0040-memoria-encode-queue-budget.patch") < names.index(
        "0041-memoria-tap-heap-guard.patch"
    )


def test_build_keeps_the_git_version_and_elf_artifact() -> None:
    patch = (OVERLAY / "patches" / "0041-memoria-tap-heap-guard.patch").read_text(
        encoding="utf-8"
    )
    build = (FIRMWARE / "scripts" / "build.sh").read_text(encoding="utf-8")
    check = (FIRMWARE / "scripts" / "check-overlay.sh").read_text(encoding="utf-8")
    assert 'set(PROJECT_VER "2.4.2+${MEMORIA_FIRMWARE_GIT_SHA}")' in patch
    assert 'export MEMORIA_FIRMWARE_GIT_SHA=' in build
    assert 'cp "$elf_image" "$MEMORIA_ARTIFACT_DIR/$artifact_name.elf"' in build
    assert 'find "$MEMORIA_ARTIFACT_DIR" -maxdepth 1 -type f -name "$MEMORIA_BOARD_NAME*.elf"' in check
    assert 'die "build artifact ELF is missing"' in check

