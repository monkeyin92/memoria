#pragma once

#include <atomic>
#include <cmath>
#include <cstddef>
#include <cstdint>

namespace memoria {

// How loud the audio is, as the 0..255 number the mascot's mouth, glow and body follow (TODOLIST M-1).
// Until now the mouth flapped on a random timer whenever the device state said "speaking", so a stalled
// or lost reply still looked like talking (round 18, p03: 30.8 s in speaking). The audio tasks measure
// the PCM they actually move and publish it here; the display task polls it once per frame.
//
// The scale is the block's RMS in dBFS, linear between a floor and a ceiling. Speech from the TTS sits
// around -26..-16 dBFS in 20 ms blocks (about 170..215 on this scale), the gaps between words and the
// pauses between sentences fall to -45 dBFS or less (55 or less), and the AFE's output while nobody speaks
// is quieter still.
constexpr float kLevelFloorDb = -54.0f;  // this quiet or quieter reads as silence (0)
constexpr float kLevelCeilDb = -12.0f;   // this loud or louder reads as full (255)

inline uint8_t RmsToLevel(float rms) {
    if (!(rms > 0.0f)) {
        return 0;
    }
    const float db = 20.0f * std::log10(rms);
    const float t = (db - kLevelFloorDb) / (kLevelCeilDb - kLevelFloorDb);
    if (t <= 0.0f) {
        return 0;
    }
    if (t >= 1.0f) {
        return 255;
    }
    return static_cast<uint8_t>(t * 255.0f + 0.5f);
}

// RMS of 16-bit PCM, 0..1 of full scale. Integer accumulation: the audio tasks must not do software
// double-precision arithmetic per sample.
inline float PcmRms(const int16_t* pcm, std::size_t count) {
    if (pcm == nullptr || count == 0) {
        return 0.0f;
    }
    uint64_t sum = 0;
    for (std::size_t i = 0; i < count; ++i) {
        const int32_t s = pcm[i];
        sum += static_cast<uint64_t>(static_cast<int64_t>(s) * s);
    }
    return std::sqrt(static_cast<float>(sum) / static_cast<float>(count)) / 32768.0f;
}

inline uint8_t PcmLevel(const int16_t* pcm, std::size_t count) {
    return RmsToLevel(PcmRms(pcm, count));
}

// Where an audio task leaves its block levels for the display task: the loudest and the quietest level
// and the number of blocks since the reader last looked. One writer and one reader, lock-free, one 32-bit
// word. The count is what tells "silence" from "no audio at all": a stalled playback queue publishes
// nothing. The valley is what lets a display polling at 25 fps see the syllables inside 20 ms blocks that
// arrive two or three at a time: the mouth opens on a crest and shuts in the dip before the next one.
//
// Word layout: bits 0..7 the peak, bits 8..15 255 minus the valley (so both are a plain maximum and an empty
// word is all zeros), bits 16..31 the block count, saturating.
class AudioLevelTap final {
public:
    struct Reading {
        uint8_t peak = 0;
        uint8_t valley = 0;  // 0 when no block arrived
        uint32_t blocks = 0;
    };

    // The audio task: one block of PCM was just played (or captured), this loud.
    void Publish(uint8_t level) {
        const uint32_t inverted = 255u - level;
        uint32_t current = word_.load(std::memory_order_relaxed);
        for (;;) {
            const uint32_t peak = current & 0xFFu;
            const uint32_t deepest = (current >> 8) & 0xFFu;
            const uint32_t blocks = current >> 16;
            const uint32_t next = ((blocks < 0xFFFFu ? blocks + 1 : blocks) << 16) |
                                  ((inverted > deepest ? inverted : deepest) << 8) | (level > peak ? level : peak);
            if (word_.compare_exchange_weak(current, next, std::memory_order_relaxed)) {
                return;
            }
        }
    }

    // The display task: everything published since the previous call.
    Reading Take() {
        const uint32_t word = word_.exchange(0, std::memory_order_relaxed);
        Reading reading;
        reading.blocks = word >> 16;
        if (reading.blocks != 0) {
            reading.peak = static_cast<uint8_t>(word & 0xFFu);
            reading.valley = static_cast<uint8_t>(255u - ((word >> 8) & 0xFFu));
        }
        return reading;
    }

private:
    std::atomic<uint32_t> word_{0};
};

// Publishes `pcm` (mono, `sample_rate` Hz) to `tap` in blocks of 20 ms. A playback frame is exactly one block on
// this board (20 ms Opus frames), but a local cue decodes into longer ones, and the mouth wants syllable-sized
// steps either way.
inline void PublishPcm(AudioLevelTap& tap, const int16_t* pcm, std::size_t count, int sample_rate) {
    const std::size_t block = sample_rate > 0 ? static_cast<std::size_t>(sample_rate) / 50 : 0;
    if (pcm == nullptr || count == 0 || block == 0) {
        return;
    }
    for (std::size_t at = 0; at < count; at += block) {
        tap.Publish(PcmLevel(pcm + at, count - at < block ? count - at : block));
    }
}

// The speaker side (what AudioOutputTask hands the codec) and the microphone side (what the audio
// engine sends upstream, after echo cancellation and noise suppression). Inline variables: constant
// initialisation, no guard on the audio task's path.
inline AudioLevelTap output_level_tap;
inline AudioLevelTap input_level_tap;

}  // namespace memoria
