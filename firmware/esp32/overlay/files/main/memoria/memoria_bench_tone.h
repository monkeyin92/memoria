#pragma once

#include <cstddef>
#include <cstdint>
#include <cmath>

namespace memoria {

// One known sine for a bench slot probe. Not a spoken line and not an asset.
// phase is the next sample index at sample_rate and is advanced by samples.

inline std::size_t FillBenchTone(std::int16_t* dst, std::size_t samples, int sample_rate, int hz,
                                 std::uint32_t* phase, int amplitude) {
    if (dst == nullptr || phase == nullptr || samples == 0 || sample_rate <= 0 || hz <= 0) {
        return 0;
    }
    for (std::size_t i = 0; i < samples; ++i) {
        const double turns =
            static_cast<double>(*phase + static_cast<std::uint32_t>(i)) * static_cast<double>(hz) /
            static_cast<double>(sample_rate);
        const double wave = std::sin(turns * 6.283185307179586);
        dst[i] = static_cast<std::int16_t>(std::lround(wave * static_cast<double>(amplitude)));
    }
    *phase += static_cast<std::uint32_t>(samples);
    return samples;
}

}  // namespace memoria
