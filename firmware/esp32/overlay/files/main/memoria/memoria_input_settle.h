#pragma once

#include <atomic>
#include <cstdint>

namespace memoria {

// The first part of a freshly opened microphone stream is not speech. The codec's ADC powers up, the
// local listening cue rings out of the speaker, and the echo canceller has not converged yet. In round 10
// (2026-10-02) the AFE's VAD fired within 0.2 s of every wake that had to power the input up, three times
// at a loud level (rms 0.06-0.48, sample 1920-2560); the server took each for the child starting to speak
// and held the wake greeting back for 3.3 s (TODOLIST N-13). Blocks read in this window are dropped
// before the audio engine sees them, so none of it can open a VAD epoch.
//
// 300 ms at 16 kHz: the artifacts ended by ~0.2 s, and a longer drop would only cost the start of a
// child's first word.
constexpr uint32_t kInputSettleSamples = 4800;

// The listening cue is played from the speaker beside the microphone. In four of the nine round-10 wakes
// the mic opened within 0.2 s of the cue being decoded (the cue-first seam does not always keep them
// apart), so while a local sound can still be ringing the input is muted too. Bounded on purpose: the
// window ends by itself, nothing can leave the robot deaf. The cue is ~0.5 s long.
constexpr uint32_t kLocalCueMuteMs = 800;

class InputSettle final {
public:
    // The input was just powered up: the next `samples` samples are start-up artifacts.
    void Arm(uint32_t samples = kInputSettleSamples) {
        remaining_.store(samples, std::memory_order_relaxed);
    }

    // A local sound (the listening cue, an alert) was just queued for the speaker.
    void NoteLocalSound(uint64_t now_ms) {
        cue_mute_until_ms_.store(now_ms + kLocalCueMuteMs, std::memory_order_relaxed);
    }

    // True for a block read inside the settling window or while a local sound can still ring: it must
    // not be fed. Both windows count down independently, so a drop never outlasts the longer of the two.
    bool ShouldDrop(uint32_t samples, uint64_t now_ms) {
        bool drop = false;
        const uint32_t remaining = remaining_.load(std::memory_order_relaxed);
        if (remaining > 0) {
            remaining_.store(samples >= remaining ? 0 : remaining - samples,
                             std::memory_order_relaxed);
            drop = true;
        }
        if (now_ms < cue_mute_until_ms_.load(std::memory_order_relaxed)) {
            drop = true;
        }
        return drop;
    }

    uint32_t remaining() const { return remaining_.load(std::memory_order_relaxed); }

private:
    std::atomic<uint32_t> remaining_{0};
    std::atomic<uint64_t> cue_mute_until_ms_{0};
};

}  // namespace memoria
