#pragma once

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

}
