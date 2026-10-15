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

// Linear chirp on the same clock as FillBenchTone. index is the next sample
// inside a sweep of sweep_samples, and frequency moves from hz_start at
// index 0 to hz_end across that span. Not a spoken line and not an asset.

inline std::size_t FillBenchSweep(std::int16_t* dst, std::size_t samples, int sample_rate, int hz_start,
                                  int hz_end, std::uint32_t* index, std::uint32_t sweep_samples, int amplitude) {
    if (dst == nullptr || index == nullptr || samples == 0 || sample_rate <= 0 || hz_start <= 0 ||
        hz_end <= 0 || sweep_samples == 0) {
        return 0;
    }
    const double span = static_cast<double>(sweep_samples);
    const double rate = static_cast<double>(sample_rate);
    const double f0 = static_cast<double>(hz_start);
    const double df = static_cast<double>(hz_end - hz_start);
    std::size_t wrote = 0;
    for (; wrote < samples; ++wrote) {
        const auto n = *index + static_cast<std::uint32_t>(wrote);
        if (n >= sweep_samples) {
            break;
        }
        const double nd = static_cast<double>(n);
        const double turns = f0 * nd / rate + df * nd * nd / (2.0 * span * rate);
        const double wave = std::sin(turns * 6.283185307179586);
        dst[wrote] = static_cast<std::int16_t>(std::lround(wave * static_cast<double>(amplitude)));
    }
    *index += static_cast<std::uint32_t>(wrote);
    return wrote;
}

}  // namespace memoria
