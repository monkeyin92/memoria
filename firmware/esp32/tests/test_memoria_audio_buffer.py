from __future__ import annotations

import pathlib
import subprocess


ROOT = pathlib.Path(__file__).parents[3]
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
    subprocess.run([str(binary)], check=True)


def test_audio_source_uses_matching_codec_channel_shape() -> None:
    codec = (OVERLAY / "patches" / "0041-memoria-tap-heap-guard.patch").read_text(
        encoding="utf-8"
    )
    assert "channel = input_channels_" in codec
    assert "memoria_audio_buffer.h" in codec
    assert "InterleavedInputSamples" in codec
    assert "InputReadFits" in codec
    assert "ReadSamplesFit" in codec


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
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'set(PROJECT_VER "2.4.2+${MEMORIA_FIRMWARE_GIT_SHA}")' in patch
    assert 'export MEMORIA_FIRMWARE_GIT_SHA=' in build
    assert 'cp "$elf_image" "$MEMORIA_ARTIFACT_DIR/$artifact_name.elf"' in build
    assert "path: firmware/esp32/artifacts/*" in ci

