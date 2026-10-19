#pragma once

#include <string>
#include <vector>

#include "fake_nvs.h"

// Mirrors managed_components/78__esp-wifi-connect/include/ssid_manager.h.
// Clear() returns void, so the erase module cannot branch on its result.
// The component clears the in-memory list and saves it to NVS; when the save
// does not take effect the saved credentials survive a reboot and reload, so
// the fake keeps the list to represent that state. GetSsidList() is therefore
// the only observable outcome the module can check.
struct SsidItem {
    std::string ssid;
    std::string password;
};

class SsidManager {
public:
    static SsidManager& GetInstance() {
        static SsidManager instance;
        return instance;
    }

    void AddSsid(const std::string& ssid, const std::string& password) {
        ssid_list_.push_back({ssid, password});
    }

    void Clear() {
        fake::Trace("wifi_clear");
        if (fake::State().wifi_clear_fails) {
            return;
        }
        ssid_list_.clear();
    }

    const std::vector<SsidItem>& GetSsidList() const { return ssid_list_; }

private:
    std::vector<SsidItem> ssid_list_;
};
