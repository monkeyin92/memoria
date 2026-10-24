#include "memoria_activation_retry_policy.h"

namespace memoria {

bool ActivationRetryPolicy::ShouldRetryOnBoot(bool unbound_awaiting_binding,
                                              ActivationAttemptOutcome outcome) {
    // A 409 always keeps the QR and retries, even when a failed local erase
    // left activation_v set. Other failures retry only while still unbound.
    return outcome == ActivationAttemptOutcome::kServerUnbound ||
           (unbound_awaiting_binding && outcome == ActivationAttemptOutcome::kFailed);
}

uint32_t ActivationRetryPolicy::DelayMsForFailureCount(uint32_t failures) {
    if (failures <= 1) {
        return kFirstDelayMs;
    }
    if (failures == 2) {
        return kSecondDelayMs;
    }
    return kMaxDelayMs;
}

bool ActivationRetryPolicy::ShouldShowErrorPage(uint32_t failures,
                                                ActivationAttemptOutcome outcome) {
    return outcome == ActivationAttemptOutcome::kFailed &&
           failures >= kErrorPageFailureCount;
}

bool ActivationRetryPolicy::ShouldRestoreQr(ActivationAttemptOutcome outcome) {
    return outcome == ActivationAttemptOutcome::kServerUnbound;
}

}  // namespace memoria
