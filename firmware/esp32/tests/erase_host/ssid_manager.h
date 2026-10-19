#pragma once

#include "fake_nvs.h"

class SsidManager {
public:
    static SsidManager& GetInstance() {
        static SsidManager instance;
        return instance;
    }
    void Clear() { fake::Trace("wifi_clear"); }
};
