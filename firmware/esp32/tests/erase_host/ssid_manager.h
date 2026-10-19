#pragma once

#include "fake_nvs.h"

class SsidManager {
public:
    static SsidManager& GetInstance() {
        static SsidManager instance;
        return instance;
    }
    bool Clear() {
        fake::Trace("wifi_clear");
        return !fake::State().wifi_clear_fails;
    }
};
