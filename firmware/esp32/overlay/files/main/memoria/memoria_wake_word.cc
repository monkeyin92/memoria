#include "memoria_wake_word.h"

#include <esp_log.h>
#include <nvs.h>

#include <algorithm>
#include <cctype>

namespace memoria {
namespace {

constexpr char kTag[] = "MemoriaWakeWord";
constexpr char kNvsNamespace[] = "memoria";
constexpr char kNvsWakeWordId[] = "wake_word_id";
constexpr char kNvsWakeWordCommand[] = "wake_word_cmd";
constexpr char kNvsWakeWordDisplay[] = "wake_word_disp";

bool IsValidWakeWordId(const std::string& wake_word_id) {
    return wake_word_id == "mo_li" || wake_word_id == "mei_mo_li_ya" || wake_word_id == "custom";
}

std::string NormalizePinyin(const std::string& value) {
    std::string normalized;
    normalized.reserve(value.size());
    bool pending_space = false;
    for (unsigned char ch : value) {
        if (std::isspace(ch)) {
            pending_space = !normalized.empty();
            continue;
        }
        if (pending_space) {
            normalized.push_back(' ');
            pending_space = false;
        }
        normalized.push_back(static_cast<char>(std::tolower(ch)));
    }
    return normalized;
}

}  // namespace

WakeWordRegistry& WakeWordRegistry::GetInstance() {
    static WakeWordRegistry instance;
    return instance;
}

WakeWordSelection ResolveWakeWordSelection(
    const std::string& wake_word_id,
    const std::string& wake_word_pinyin,
    const std::string& wake_word_display) {
    if (!IsValidWakeWordId(wake_word_id)) {
        return {"mo_li", "mo li", "茉莉"};
    }
    if (wake_word_id == "mo_li") {
        return {"mo_li", "mo li", "茉莉"};
    }
    if (wake_word_id == "mei_mo_li_ya") {
        return {"mei_mo_li_ya", "mei mo li ya", "梅莫里亚"};
    }
    const std::string command = NormalizePinyin(wake_word_pinyin);
    const std::string display = wake_word_display.empty() ? wake_word_id : wake_word_display;
    if (command.empty()) {
        return {"mo_li", "mo li", "茉莉"};
    }
    return {"custom", command, display};
}

void WakeWordRegistry::Configure(const WakeWordSelection& selection) {
    active_ = selection;
    ESP_LOGI(kTag, "configured wake word id=%s command=%s display=%s",
             selection.id.c_str(), selection.command.c_str(), selection.display.c_str());
}

std::optional<WakeWordSelection> WakeWordRegistry::ActiveSelection() const {
    return active_;
}

bool WakeWordRegistry::ApplyActiveCommand(std::string* command, std::string* display) const {
    if (!active_.has_value() || command == nullptr || display == nullptr) {
        return false;
    }
    *command = active_->command;
    *display = active_->display;
    return true;
}

void WakeWordRegistry::PersistToNvs() const {
    if (!active_.has_value()) {
        return;
    }
    nvs_handle_t handle = 0;
    if (nvs_open(kNvsNamespace, NVS_READWRITE, &handle) != ESP_OK) {
        ESP_LOGW(kTag, "failed to open NVS for wake word persistence");
        return;
    }
    nvs_set_str(handle, kNvsWakeWordId, active_->id.c_str());
    nvs_set_str(handle, kNvsWakeWordCommand, active_->command.c_str());
    nvs_set_str(handle, kNvsWakeWordDisplay, active_->display.c_str());
    nvs_commit(handle);
    nvs_close(handle);
}

void WakeWordRegistry::LoadFromNvs() {
    nvs_handle_t handle = 0;
    if (nvs_open(kNvsNamespace, NVS_READONLY, &handle) != ESP_OK) {
        return;
    }
    char id[32] = {};
    char command[64] = {};
    char display[32] = {};
    size_t id_len = sizeof(id);
    size_t command_len = sizeof(command);
    size_t display_len = sizeof(display);
    if (nvs_get_str(handle, kNvsWakeWordId, id, &id_len) != ESP_OK ||
        nvs_get_str(handle, kNvsWakeWordCommand, command, &command_len) != ESP_OK ||
        nvs_get_str(handle, kNvsWakeWordDisplay, display, &display_len) != ESP_OK) {
        nvs_close(handle);
        return;
    }
    nvs_close(handle);
    Configure(ResolveWakeWordSelection(id, command, display));
}

}  // namespace memoria
