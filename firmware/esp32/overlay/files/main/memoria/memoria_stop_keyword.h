#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace memoria {

// Master switch for the on-device stop keywords below. Off since build 15:
// playback runs echo cancellation, MultiNet6 and the uplink Opus encoder on
// one core, and on 2026-09-30 that starved the idle task (task watchdog 10-13 s
// into a long reply), delayed the server's playback.flush and produced no
// local stop hit in three long replies. The cloud path already stops playback
// by meaning ("停一下", "好的我知道了", "退下吧", "再见", ...), so with the
// switch off the device behaves as build 10 did during playback: the wake word
// detector is disabled, no stop phrase joins the MultiNet graph, and hello
// declares local_stop_keyword=false.
inline constexpr bool kLocalStopKeywordEnabled = false;

// On-device stop keywords. Cloud ASR misses a short 「停」 under reply echo, so
// VoCat registers these phrases as extra MultiNet commands next to the wake
// word and lets them stop playback locally, the same way BOOT does. They only
// act in stop-only mode, which Application arms while a generation is playing
// and the signed allowed_barge_in contains "keyword"; outside it every stop hit
// is logged and dropped. Host-compilable on purpose (no ESP-IDF includes) so
// the thresholds and debounce can be tested without hardware.
//
// Detection is split in two so weak hits stay visible for tuning. MultiNet
// itself reports every command at or above kMultiNetDetectThreshold; the code
// then applies each command's own acceptance: kWakeWordMinScore for the wake
// word (equal to that floor since build 14) and min_score per stop row.
//
// Tuning: in stop-only mode every stop hit >= kMultiNetDetectThreshold is
// logged on serial as
//   "Local stop keyword <verdict>: id=<id> prob=<score> min=<min_score>"
// and every detection window's candidates as "MultiNet stop-mode ...". Set a
// row's min_score just under the scores clearly spoken stops reach, above the
// scores reply echo reaches, or delete the row to drop the phrase. First device
// run (build 11, TTS at 0.5-1 m): 「停停」 hit as ting at 0.447, 「别说了」 at
// 0.243, four stops stayed under the old 0.20 floor; two long stories without
// a stop word produced no stop hit.
struct LocalStopPhrase {
    const char* id;       // keyword_id on the wire (device-media-v2 identifier)
    const char* command;  // MultiNet6 pinyin command
    const char* display;
    float min_score;      // MultiNet prob needed before playback stops
    std::uint32_t nominal_duration_ms;  // evidence.duration_ms estimate
};

inline constexpr LocalStopPhrase kLocalStopPhrases[] = {
    {"ting_yi_xia", "ting yi xia", "停一下", 0.20f, 700},
    {"bie_shuo_le", "bie shuo le", "别说了", 0.20f, 700},
    {"ting_ting", "ting ting", "停停", 0.20f, 500},
    // A single syllable false-triggers far more easily; keep it strictest.
    {"ting", "ting", "停", 0.30f, 350},
};
inline constexpr std::size_t kLocalStopPhraseCount =
    sizeof(kLocalStopPhrases) / sizeof(kLocalStopPhrases[0]);

// The phrases CustomWakeWord registers as MultiNet commands: all of them while
// the switch is on, none while it is off.
struct LocalStopPhraseRange {
    const LocalStopPhrase* first;
    const LocalStopPhrase* last;
    constexpr const LocalStopPhrase* begin() const { return first; }
    constexpr const LocalStopPhrase* end() const { return last; }
};

inline constexpr LocalStopPhraseRange LocalStopPhrasesToRegister() {
    return kLocalStopKeywordEnabled
               ? LocalStopPhraseRange{kLocalStopPhrases, kLocalStopPhrases + kLocalStopPhraseCount}
               : LocalStopPhraseRange{kLocalStopPhrases, kLocalStopPhrases};
}

// MultiNet's model-wide detection threshold on this board. It must match
// CONFIG_CUSTOM_WAKE_WORD_THRESHOLD (percent) in the board config.json, which
// feeds the assets index.json; CustomWakeWord applies this value on Memoria.
inline constexpr float kMultiNetDetectThreshold = 0.10f;

// The wake word accepts everything MultiNet reports. Build 12/13 kept build 10's
// 0.20, but on the bench 「茉莉」 scored 0.11-0.20 in ten of twelve wake-ups
// (build 13 rejected all of them; build 10 with the same floor missed them
// too), so acceptance was lowered to the detection floor (build 14).
inline constexpr float kWakeWordMinScore = 0.10f;

inline bool WakeWordAccepted(float score) {
    return score >= kWakeWordMinScore;  // NaN is rejected
}

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
