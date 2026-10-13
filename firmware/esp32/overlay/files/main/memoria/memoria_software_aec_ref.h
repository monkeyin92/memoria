#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>

namespace memoria {

// This board has no hardware AEC reference: ES7210 channel 1 is empty noise. Play PCM that is about
// to leave via I2S is copied into the AFE R channel at the codec input rate (24 kHz) inside
// ReadAudioData, before the 24→16 kHz resampler, so WritePlay and ApplyToFeed share one clock.
// Single writer (AudioOutputTask) and single reader (AudioInputTask / ReadAudioData).

constexpr std::size_t kSoftwareAecRefCapacity = 8192;  // ~341 ms of mono at 24 kHz

class SoftwareAecReference final {
public:
    void WritePlay(const int16_t* mono, std::size_t samples) {
        if (mono == nullptr || samples == 0) {
            return;
        }
        std::size_t w = write_.load(std::memory_order_relaxed);
        const std::size_t r = read_.load(std::memory_order_acquire);
        for (std::size_t i = 0; i < samples; ++i) {
            const std::size_t next = (w + 1) & kMask;
            if (next == r) {
                break;  // drop the newest samples; capacity is sized so this is rare
            }
            buf_[w] = mono[i];
            w = next;
        }
        write_.store(w, std::memory_order_release);
    }

    // Overwrites channel 1 (R) with drained play samples. Underrun writes 0. Channel 0 (mic) is
    // untouched. No-op when channels < 2.
    void ApplyToFeed(int16_t* interleaved, std::size_t frames, int channels) {
        if (interleaved == nullptr || frames == 0 || channels < 2) {
            return;
        }
        std::size_t r = read_.load(std::memory_order_relaxed);
        const std::size_t w = write_.load(std::memory_order_acquire);
        const std::size_t stride = static_cast<std::size_t>(channels);
        for (std::size_t f = 0; f < frames; ++f) {
            int16_t sample = 0;
            if (r != w) {
                sample = buf_[r];
                r = (r + 1) & kMask;
            }
            interleaved[f * stride + 1] = sample;
        }
        read_.store(r, std::memory_order_release);
    }

    std::size_t available() const {
        const std::size_t w = write_.load(std::memory_order_acquire);
        const std::size_t r = read_.load(std::memory_order_acquire);
        return (w - r) & kMask;
    }

private:
    static constexpr std::size_t kMask = kSoftwareAecRefCapacity - 1;
    static_assert((kSoftwareAecRefCapacity & kMask) == 0, "capacity must be a power of two");

    int16_t buf_[kSoftwareAecRefCapacity]{};
    std::atomic<std::size_t> write_{0};
    std::atomic<std::size_t> read_{0};
};

inline SoftwareAecReference software_aec_ref;

inline void WritePlay(const int16_t* mono, std::size_t samples) {
    software_aec_ref.WritePlay(mono, samples);
}

inline void ApplyToFeed(int16_t* interleaved, std::size_t frames, int channels) {
    software_aec_ref.ApplyToFeed(interleaved, frames, channels);
}

}  // namespace memoria
