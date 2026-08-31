#pragma once

#include <optional>
#include <string>

namespace memoria {

struct WakeWordSelection {
    std::string id;
    std::string command;
    std::string display;
};

class WakeWordRegistry {
public:
    static WakeWordRegistry& GetInstance();

    void Configure(const WakeWordSelection& selection);
    std::optional<WakeWordSelection> ActiveSelection() const;
    bool ApplyActiveCommand(std::string* command, std::string* display) const;

    void PersistToNvs() const;
    void LoadFromNvs();

private:
    WakeWordRegistry() = default;

    std::optional<WakeWordSelection> active_;
};

WakeWordSelection ResolveWakeWordSelection(
    const std::string& wake_word_id,
    const std::string& wake_word_pinyin,
    const std::string& wake_word_display);

}  // namespace memoria
