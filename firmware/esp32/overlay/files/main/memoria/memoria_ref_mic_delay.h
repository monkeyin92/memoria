#pragma once

#include <cstddef>
#include <cstdint>

namespace memoria {

// Lag between the software play reference (channel 1 after ApplyToFeed) and the
// mic (channel 0) at the codec rate, before the 24→16 kHz resampler.
// Positive lag_samples means the mic is later than the reference: the echo
// has not arrived yet when the reference is injected.
//
// One 10 ms block cannot see a delay of tens of ms, so this keeps 160 ms of
// decimated history and correlates once per second of audio. A pure tone is
// ambiguous by its period. The peak is meaningful for speech, which is what
// spoken 「停」 is. Single caller: AudioInputTask.

struct DelayEstimate {
    int lag_ms;
    int lag_samples;
    int corr_milli;
    int mic_rms;
    int ref_rms;
};

class RefMicDelayProbe {
public:
    bool Observe(const std::int16_t* interleaved, std::size_t frames, int channels, int sample_rate,
                 DelayEstimate* out) {
        if (out == nullptr || interleaved == nullptr || frames == 0 || channels < 2 || sample_rate != kRate) {
            return false;
        }
        const std::size_t stride = static_cast<std::size_t>(channels);
        for (std::size_t frame = 0; frame < frames; ++frame) {
            if (decim_ == 0) {
                mic_[write_] = interleaved[frame * stride];
                ref_[write_] = interleaved[frame * stride + 1];
                write_ = (write_ + 1) % kHistory;
                if (count_ < kHistory) {
                    ++count_;
                }
            }
            decim_ = (decim_ + 1) % kDecimate;
        }
        since_report_ += static_cast<int>(frames);
        if (since_report_ < kRate) {
            return false;
        }
        since_report_ = 0;
        if (count_ < kHistory) {
            return false;
        }
        const int mic_rms = Rms(mic_, 0);
        const int ref_rms = Rms(ref_, 0);
        if (mic_rms < kEnergyFloor || ref_rms < kEnergyFloor) {
            return false;
        }
        int best_lag = 0;
        int best_milli = -1001;
        for (int lag = kMinLag; lag <= kMaxLag; ++lag) {
            const int milli = Correlate(lag);
            if (milli > best_milli || (milli == best_milli && Abs(lag) < Abs(best_lag))) {
                best_milli = milli;
                best_lag = lag;
            }
        }
        if (best_milli < -1000) {
            return false;
        }
        const int lag_samples = best_lag * kDecimate;
        out->lag_samples = lag_samples;
        out->lag_ms = (lag_samples * 1000 + kRate / 2) / kRate;
        out->corr_milli = best_milli;
        out->mic_rms = mic_rms;
        out->ref_rms = ref_rms;
        return true;
    }

private:
    static constexpr int kRate = 24000;
    static constexpr int kDecimate = 8;
    static constexpr int kHistory = 480;
    static constexpr int kCorrLen = 240;
    static constexpr int kMinLag = -24;
    static constexpr int kMaxLag = 240;
    static constexpr int kEnergyFloor = 200;

    static int Abs(int value) { return value < 0 ? -value : value; }

    static std::uint64_t Isqrt(std::uint64_t value) {
        std::uint64_t op = value;
        std::uint64_t res = 0;
        std::uint64_t one = 1ull << 62;
        while (one > op) {
            one >>= 2;
        }
        while (one != 0) {
            if (op >= res + one) {
                op -= res + one;
                res = (res >> 1) + one;
            } else {
                res >>= 1;
            }
            one >>= 2;
        }
        return res;
    }

    std::int16_t At(const std::int16_t* buf, int age) const {
        int idx = write_ - 1 - age;
        while (idx < 0) {
            idx += kHistory;
        }
        return buf[idx];
    }

    int Rms(const std::int16_t* buf, int lag) const {
        std::int64_t energy = 0;
        int used = 0;
        for (int i = 0; i < kCorrLen; ++i) {
            const int age = i + lag;
            if (age < 0 || age >= kHistory) {
                continue;
            }
            const std::int32_t sample = At(buf, age);
            energy += static_cast<std::int64_t>(sample) * sample;
            ++used;
        }
        if (used == 0) {
            return 0;
        }
        return static_cast<int>(Isqrt(static_cast<std::uint64_t>(energy / used)));
    }

    int Correlate(int lag) const {
        std::int64_t dot = 0;
        std::int64_t mic_energy = 0;
        std::int64_t ref_energy = 0;
        int used = 0;
        for (int i = 0; i < kCorrLen; ++i) {
            const int ref_age = i + lag;
            if (ref_age < 0 || ref_age >= kHistory) {
                continue;
            }
            const std::int32_t mic = At(mic_, i);
            const std::int32_t ref = At(ref_, ref_age);
            dot += static_cast<std::int64_t>(mic) * ref;
            mic_energy += static_cast<std::int64_t>(mic) * mic;
            ref_energy += static_cast<std::int64_t>(ref) * ref;
            ++used;
        }
        if (used < kCorrLen / 2 || mic_energy == 0 || ref_energy == 0) {
            return -1001;
        }
        const std::uint64_t denom = Isqrt(static_cast<std::uint64_t>(mic_energy)) *
                                    Isqrt(static_cast<std::uint64_t>(ref_energy));
        if (denom == 0) {
            return -1001;
        }
        int milli = static_cast<int>(dot * 1000 / static_cast<std::int64_t>(denom));
        if (milli > 1000) {
            milli = 1000;
        }
        if (milli < -1000) {
            milli = -1000;
        }
        return milli;
    }

    std::int16_t mic_[kHistory]{};
    std::int16_t ref_[kHistory]{};
    int write_ = 0;
    int count_ = 0;
    int decim_ = 0;
    int since_report_ = 0;
};

inline RefMicDelayProbe ref_mic_delay;

inline bool ObserveRefMicDelay(const std::int16_t* interleaved, std::size_t frames, int channels,
                               int sample_rate, DelayEstimate* out) {
    return ref_mic_delay.Observe(interleaved, frames, channels, sample_rate, out);
}

}  // namespace memoria
