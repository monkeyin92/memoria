#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace memoria {

// On-device stop keywords. Cloud ASR misses a short 「停」 under reply echo, so
// VoCat registers these phrases as extra MultiNet commands next to the wake
// word and lets them stop playback locally, the same way BOOT does. They only
// act in stop-only mode, which Application arms while a generation is playing
// and the signed allowed_barge_in contains "keyword"; outside it every stop hit
// is logged and dropped. Host-compilable on purpose (no ESP-IDF includes) so
// the thresholds and debounce can be tested without hardware.
//
// Tuning: every hit is logged on serial as
//   "Local stop keyword <verdict>: id=<id> prob=<score> min=<min_score>"
// Raise a phrase's min_score when echo or room speech crosses it, lower it
// when a clearly spoken stop reads "below_threshold", and delete a row to drop
// the phrase. MultiNet reports prob in [0, 1]; the wake word 「茉莉」 fires at
// the global detection threshold 0.20 and field hits land around 0.21-0.27, so
// every stop row starts stricter than that.
struct LocalStopPhrase {
    const char* id;       // keyword_id on the wire (device-media-v2 identifier)
    const char* command;  // MultiNet6 pinyin command
    const char* display;
    float min_score;      // MultiNet prob needed before playback stops
    std::uint32_t nominal_duration_ms;  // evidence.duration_ms estimate
};

inline constexpr LocalStopPhrase kLocalStopPhrases[] = {
    {"ting_yi_xia", "ting yi xia", "停一下", 0.25f, 700},
    {"bie_shuo_le", "bie shuo le", "别说了", 0.25f, 700},
    {"ting_ting", "ting ting", "停停", 0.25f, 500},
    // A single syllable false-triggers far more easily; keep it strictest.
    {"ting", "ting", "停", 0.35f, 350},
};
inline constexpr std::size_t kLocalStopPhraseCount =
    sizeof(kLocalStopPhrases) / sizeof(kLocalStopPhrases[0]);

// MultiNet command action for the rows above (the wake word uses "wake").
inline constexpr char kLocalStopAction[] = "stop";

// One utterance must produce one stop. MultiNet cleans its state after each
// hit, so the tail of 「停停」 or a repeated 「停」 can hit again right away.
inline constexpr std::int64_t kLocalStopDebounceMs = 1500;

// Edge and Voice Core only honour a hard-stop keyword at confidence >= 0.8
// (KWSHardStopMinConfidence). The device has already applied its own per
// phrase threshold and flushed playback, so the wire confidence is the raw
// score lifted to that floor; the raw score is kept in the serial log.
inline constexpr float kLocalStopHardStopConfidenceFloor = 0.8f;

inline const LocalStopPhrase* FindLocalStopPhrase(const char* id) {
    if (id == nullptr) {
        return nullptr;
    }
    for (const auto& phrase : kLocalStopPhrases) {
        if (std::strcmp(phrase.id, id) == 0) {
            return &phrase;
        }
    }
    return nullptr;
}

inline float LocalStopReportedConfidence(float score) {
    if (!(score >= kLocalStopHardStopConfidenceFloor)) {  // also catches NaN
        return kLocalStopHardStopConfidenceFloor;
    }
    return score > 1.0f ? 1.0f : score;
}

enum class LocalStopVerdict : std::uint8_t {
    kAccept = 0,
    kNotArmed = 1,        // not in stop-only mode (idle, listening, ...)
    kUnknownPhrase = 2,
    kBelowThreshold = 3,
    kDebounced = 4,
};

inline const char* LocalStopVerdictName(LocalStopVerdict verdict) {
    switch (verdict) {
        case LocalStopVerdict::kAccept:
            return "accepted";
        case LocalStopVerdict::kNotArmed:
            return "not_armed";
        case LocalStopVerdict::kUnknownPhrase:
            return "unknown_phrase";
        case LocalStopVerdict::kBelowThreshold:
            return "below_threshold";
        case LocalStopVerdict::kDebounced:
            return "debounced";
    }
    return "unknown";
}

// Decides whether one MultiNet stop hit may stop playback. Only accepted hits
// start the debounce window, so a weak or unarmed hit never masks a real stop.
class LocalStopKeywordGate {
public:
    LocalStopVerdict Evaluate(const char* phrase_id, float score, bool armed,
                              std::int64_t now_ms) {
        if (!armed) {
            return LocalStopVerdict::kNotArmed;
        }
        const LocalStopPhrase* phrase = FindLocalStopPhrase(phrase_id);
        if (phrase == nullptr) {
            return LocalStopVerdict::kUnknownPhrase;
        }
        if (!(score >= phrase->min_score)) {
            return LocalStopVerdict::kBelowThreshold;
        }
        if (has_last_accept_ && now_ms - last_accept_ms_ < kLocalStopDebounceMs) {
            return LocalStopVerdict::kDebounced;
        }
        has_last_accept_ = true;
        last_accept_ms_ = now_ms;
        return LocalStopVerdict::kAccept;
    }

    void Reset() {
        has_last_accept_ = false;
        last_accept_ms_ = 0;
    }

private:
    bool has_last_accept_ = false;
    std::int64_t last_accept_ms_ = 0;
};

}  // namespace memoria
