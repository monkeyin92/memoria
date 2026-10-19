#include <iostream>
#include <string>

#include "memoria_activation_error_text.h"
#include "memoria_activation_retry_policy.h"

namespace {

memoria::ActivationAttemptOutcome ParseOutcome(const std::string& value) {
    if (value == "ok") {
        return memoria::ActivationAttemptOutcome::kOk;
    }
    if (value == "unbound") {
        return memoria::ActivationAttemptOutcome::kServerUnbound;
    }
    return memoria::ActivationAttemptOutcome::kFailed;
}

}  // namespace

int main() {
    std::string command;
    while (std::cin >> command) {
        if (command == "delay") {
            uint32_t failures = 0;
            std::cin >> failures;
            std::cout << "delay "
                      << memoria::ActivationRetryPolicy::DelayMsForFailureCount(failures)
                      << "\n";
        } else if (command == "boot") {
            int unbound = 0;
            std::string outcome;
            std::cin >> unbound >> outcome;
            std::cout << "boot "
                      << (memoria::ActivationRetryPolicy::ShouldRetryOnBoot(
                              unbound != 0, ParseOutcome(outcome))
                              ? "true"
                              : "false")
                      << "\n";
        } else if (command == "page") {
            uint32_t failures = 0;
            std::string outcome;
            std::cin >> failures >> outcome;
            std::cout << "page "
                      << (memoria::ActivationRetryPolicy::ShouldShowErrorPage(
                              failures, ParseOutcome(outcome))
                              ? "true"
                              : "false")
                      << "\n";
        } else if (command == "restore") {
            std::string outcome;
            std::cin >> outcome;
            std::cout << "restore "
                      << (memoria::ActivationRetryPolicy::ShouldRestoreQr(
                              ParseOutcome(outcome))
                              ? "true"
                              : "false")
                      << "\n";
        } else if (command == "copy") {
            std::cout << "title " << memoria::kActivationNetworkErrorTitle << "\n";
            std::cout << "body " << memoria::kActivationNetworkErrorBody << "\n";
            std::cout << "detail " << memoria::kActivationNetworkErrorDetailFormat << "\n";
        }
    }
    return 0;
}
