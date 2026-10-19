#pragma once

#include <cstdint>

namespace memoria {

// How the last activation-manifest attempt ended.
enum class ActivationAttemptOutcome {
    kOk,
    // HTTP 409: the server still sees this board as unbound. The parent has to
    // scan the nearby QR before an activation manifest can exist.
    kServerUnbound,
    kFailed,
};

// Pure decisions for the activation retry loop. Deliberately free of ESP-IDF
// headers so the host tests can compile this file with a plain C++ compiler.
struct ActivationRetryPolicy {
    static constexpr uint32_t kFirstDelayMs = 5000;
    static constexpr uint32_t kSecondDelayMs = 10000;
    static constexpr uint32_t kMaxDelayMs = 20000;

    // The first failure happens in Start() before the retry task waits. Four
    // failures mean the 5 s, 10 s and 20 s waits have all elapsed: about 35 s
    // after the first failure. Only then may the error page cover the QR.
    static constexpr uint32_t kErrorPageFailureCount = 4;

    // True when the first failure should start the retry loop. A 409 always
    // does: the server says the parent still has to scan the QR. Any other
    // failure does only while the board is still unbound-awaiting-binding; a
    // bound board keeps the previous report-and-stop behaviour.
    static bool ShouldRetryOnBoot(bool unbound_awaiting_binding,
                                  ActivationAttemptOutcome outcome);

    // Wait before the next attempt after `failures` failed attempts.
    static uint32_t DelayMsForFailureCount(uint32_t failures);

    // 409 means the parent has not scanned yet, so the QR must stay visible.
    static bool ShouldShowErrorPage(uint32_t failures, ActivationAttemptOutcome outcome);

    // A later 409 must put the QR back over the error page.
    static bool ShouldRestoreQr(ActivationAttemptOutcome outcome);
};

}  // namespace memoria
