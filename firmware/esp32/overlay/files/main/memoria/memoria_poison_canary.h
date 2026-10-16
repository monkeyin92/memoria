#pragma once

#if CONFIG_HEAP_POISONING_COMPREHENSIVE
#include "esp_idf_version.h"
#if ESP_IDF_VERSION != ESP_IDF_VERSION_VAL(6, 0, 2)
#error "单块 canary 校验依赖 IDF v6.0.2 multi_heap_poisoning 私有布局，升级 IDF 前必须重核"
#endif
#endif

#include <cstdint>
#include <cstring>

namespace memoria {

inline constexpr uint32_t kPoisonHeadCanary = 0xABBA1234u;
inline constexpr uint32_t kPoisonTailCanary = 0xBAAD5678u;

struct PoisonHead {
    uint32_t head_canary;
    uint32_t alloc_size;
};

static_assert(sizeof(PoisonHead) == 8, "ESP32 poison head is 8 bytes");

inline constexpr uint32_t kPoisonCanaryBytes = sizeof(PoisonHead) + sizeof(uint32_t);

inline bool PoisonCanaryOk(const void* user, uint32_t user_bytes) noexcept {
    if (user == nullptr || user_bytes == 0) {
        return false;
    }
    const auto* bytes = static_cast<const unsigned char*>(user);
    PoisonHead head{};
    std::memcpy(&head, bytes - sizeof(head), sizeof(head));
    if (head.head_canary != kPoisonHeadCanary || head.alloc_size != user_bytes) {
        return false;
    }
    uint32_t tail = 0;
    std::memcpy(&tail, bytes + user_bytes, sizeof(tail));
    return tail == kPoisonTailCanary;
}

inline uint32_t CanaryElapsedUs(int64_t started, int64_t finished) noexcept {
    if (finished < started) {
        return 0;
    }
    const uint64_t delta = static_cast<uint64_t>(finished - started);
    if (delta > static_cast<uint64_t>(UINT32_MAX)) {
        return UINT32_MAX;
    }
    return static_cast<uint32_t>(delta);
}

struct CanaryUsWindow {
    uint32_t count = 0;
    uint32_t min_us = 0;
    uint32_t max_us = 0;
    uint64_t sum_us = 0;

    void Note(uint32_t us) noexcept {
        if (count == 0 || us < min_us) {
            min_us = us;
        }
        if (us > max_us) {
            max_us = us;
        }
        sum_us += us;
        count += 1;
    }

    void Take(uint32_t& n, uint32_t& min_out, uint32_t& max_out, uint32_t& avg_out) noexcept {
        n = count;
        min_out = count == 0 ? 0 : min_us;
        max_out = count == 0 ? 0 : max_us;
        avg_out = count == 0 ? 0 : static_cast<uint32_t>(sum_us / count);
        count = 0;
        min_us = 0;
        max_us = 0;
        sum_us = 0;
    }
};

}
