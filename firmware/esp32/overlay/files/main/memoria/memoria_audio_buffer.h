#pragma once

#include <cstddef>
#include <limits>

namespace memoria {

constexpr std::size_t InputFramesAtCodecRate(std::size_t requested_frames,
                                             int requested_rate,
                                             int codec_rate) {
    if (requested_rate <= 0 || codec_rate <= 0) {
        return 0;
    }
    if (requested_frames >
        std::numeric_limits<std::size_t>::max() /
            static_cast<std::size_t>(codec_rate)) {
        return 0;
    }
    return requested_frames * static_cast<std::size_t>(codec_rate) /
           static_cast<std::size_t>(requested_rate);
}

constexpr std::size_t InterleavedInputSamples(std::size_t frames, int channels) {
    if (channels <= 0 || frames >
        std::numeric_limits<std::size_t>::max() /
            static_cast<std::size_t>(channels)) {
        return 0;
    }
    return frames * static_cast<std::size_t>(channels);
}

constexpr bool InputReadFits(std::size_t allocated_samples,
                             std::size_t frames,
                             int codec_channels) {
    if (codec_channels <= 0) {
        return false;
    }
    const auto channels = static_cast<std::size_t>(codec_channels);
    if (frames > std::numeric_limits<std::size_t>::max() / channels) {
        return false;
    }
    return allocated_samples >= frames * channels;
}

constexpr bool ReadSamplesFit(std::size_t allocated_samples, int read_samples) {
    return read_samples > 0 &&
           static_cast<std::size_t>(read_samples) <= allocated_samples;
}

}  // namespace memoria
