#pragma once

#include <string>

#include "fake_nvs.h"

class Settings {
public:
    Settings(const std::string& ns, bool read_write = false) : ns_(ns) {
        (void)read_write;
        fake::Trace("settings_open " + ns_);
    }
    int32_t GetInt(const std::string& key, int32_t default_value = 0) {
        auto& keys = fake::State().store[ns_];
        auto found = keys.find(key);
        return found == keys.end() ? default_value : found->second;
    }
    void SetInt(const std::string& key, int32_t value) {
        fake::State().store[ns_][key] = value;
        fake::Trace("set_int " + ns_ + " " + key + " " + std::to_string(value));
    }

private:
    std::string ns_;
};
