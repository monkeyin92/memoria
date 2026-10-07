#include "memoria_mascot_pack.h"

#include <cstring>

namespace memoria {

namespace {

constexpr std::size_t kHeaderBytes = 64;
constexpr std::size_t kEntryBytes = 24;
constexpr uint8_t kFullFrame = 0xFF;
constexpr uint8_t kKeepIndex = 255;
constexpr int kMaxCanvas = 512;

uint16_t Read16(const uint8_t* p) { return static_cast<uint16_t>(p[0] | (p[1] << 8)); }

uint32_t Read32(const uint8_t* p) {
    return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
           (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24);
}

bool IsFullPose(MascotFrame frame) { return frame <= MascotFrame::kGreeting; }

void ComputeSpans(MascotSprite* sprite) {
    for (int row = 0; row < sprite->h; ++row) {
        const uint8_t* alpha = sprite->alpha + static_cast<std::size_t>(row) * sprite->w;
        int first = 0;
        while (first < sprite->w && alpha[first] == 0) {
            ++first;
        }
        int last = sprite->w;
        while (last > first && alpha[last - 1] == 0) {
            --last;
        }
        sprite->span[row * 2] = static_cast<uint16_t>(first < last ? first : 0);
        sprite->span[row * 2 + 1] = static_cast<uint16_t>(first < last ? last : 0);
    }
}

}  // namespace

MascotFrame BlinkFrameFor(MascotFrame pose) {
    switch (pose) {
        case MascotFrame::kDefault:
            return MascotFrame::kDefaultBlink;
        case MascotFrame::kSad:
            return MascotFrame::kSadBlink;
        case MascotFrame::kSurprised:
            return MascotFrame::kSurprisedBlink;
        case MascotFrame::kThinking:
            return MascotFrame::kThinkingBlink;
        case MascotFrame::kListening:
            return MascotFrame::kListeningBlink;
        default:
            return MascotFrame::kCount;
    }
}

MascotFrame TalkFrameFor(MascotFrame pose) {
    switch (pose) {
        case MascotFrame::kDefault:
            return MascotFrame::kDefaultTalk;
        case MascotFrame::kHappy:
            return MascotFrame::kHappyTalk;
        case MascotFrame::kSad:
            return MascotFrame::kSadTalk;
        case MascotFrame::kSurprised:
            return MascotFrame::kSurprisedTalk;
        case MascotFrame::kThinking:
            return MascotFrame::kThinkingTalk;
        default:
            return MascotFrame::kCount;
    }
}

MascotPack::MascotPack(MascotAllocFn alloc, MascotFreeFn free, MascotInflateFn inflate)
    : alloc_(alloc), free_(free), inflate_(inflate) {}

MascotPack::~MascotPack() { Reset(); }

void MascotPack::Reset() {
    for (std::size_t i = 0; i < kMascotFrameCount; ++i) {
        if (full_[i].rgb != nullptr) {
            free_(full_[i].rgb);
        }
        if (full_[i].alpha != nullptr) {
            free_(full_[i].alpha);
        }
        if (full_[i].span != nullptr) {
            free_(full_[i].span);
        }
        full_[i] = MascotSprite{};
        full_present_[i] = false;
        if (patch_[i].indices != nullptr) {
            free_(patch_[i].indices);
        }
        patch_[i] = Patch{};
    }
    if (scratch_.rgb != nullptr) {
        free_(scratch_.rgb);
    }
    if (scratch_.alpha != nullptr) {
        free_(scratch_.alpha);
    }
    if (scratch_.span != nullptr) {
        free_(scratch_.span);
    }
    scratch_ = MascotSprite{};
    scratch_frame_ = MascotFrame::kCount;
    loaded_ = false;
    id_[0] = '\0';
}

bool MascotPack::Load(const uint8_t* data, std::size_t size) {
    Reset();
    if (data == nullptr || size < kHeaderBytes || std::memcmp(data, "MMP1", 4) != 0 ||
        Read16(data + 4) != 1) {
        return false;
    }
    const std::size_t frame_count = Read16(data + 6);
    canvas_w_ = Read16(data + 8);
    canvas_h_ = Read16(data + 10);
    foot_y_ = Read16(data + 12);
    foot_half_w_ = Read16(data + 14);
    theme_.bg_inner = Read32(data + 16);
    theme_.bg_outer = Read32(data + 20);
    theme_.ink = Read32(data + 24);
    theme_.primary = Read32(data + 28);
    theme_.accent = Read32(data + 32);
    std::memcpy(id_, data + 36, 16);
    id_[16] = '\0';
    if (canvas_w_ <= 0 || canvas_h_ <= 0 || canvas_w_ > kMaxCanvas || canvas_h_ > kMaxCanvas ||
        frame_count == 0 || frame_count > kMascotFrameCount ||
        size < kHeaderBytes + frame_count * kEntryBytes) {
        Reset();
        return false;
    }
    for (std::size_t i = 0; i < frame_count; ++i) {
        const uint8_t* entry = data + kHeaderBytes + i * kEntryBytes;
        if (entry[0] >= kMascotFrameCount) {
            continue;  // a newer pack may carry frames this firmware does not know
        }
        const auto frame = static_cast<MascotFrame>(entry[0]);
        const bool ok = entry[1] == kFullFrame ? DecodeFull(frame, entry, data, size)
                                               : DecodePatch(frame, entry, data, size);
        if (!ok) {
            Reset();
            return false;
        }
    }
    if (!full_present_[static_cast<std::size_t>(MascotFrame::kDefault)]) {
        Reset();
        return false;
    }
    // One scratch sprite as large as the largest pose serves every patch frame.
    int scratch_pixels = 0;
    int scratch_rows = 0;
    for (std::size_t i = 0; i < kMascotFrameCount; ++i) {
        if (full_present_[i] && full_[i].w * full_[i].h > scratch_pixels) {
            scratch_pixels = full_[i].w * full_[i].h;
        }
        if (full_present_[i] && full_[i].h > scratch_rows) {
            scratch_rows = full_[i].h;
        }
    }
    scratch_.rgb = static_cast<uint16_t*>(alloc_(static_cast<std::size_t>(scratch_pixels) * 2));
    scratch_.alpha = static_cast<uint8_t*>(alloc_(static_cast<std::size_t>(scratch_pixels)));
    scratch_.span = static_cast<uint16_t*>(alloc_(static_cast<std::size_t>(scratch_rows) * 4));
    if (scratch_.rgb == nullptr || scratch_.alpha == nullptr || scratch_.span == nullptr) {
        Reset();
        return false;
    }
    loaded_ = true;
    return true;
}

bool MascotPack::DecodeFull(MascotFrame frame, const uint8_t* entry, const uint8_t* data,
                            std::size_t size) {
    const std::size_t colours = Read16(entry + 2);
    const int x = Read16(entry + 4);
    const int y = Read16(entry + 6);
    const int w = Read16(entry + 8);
    const int h = Read16(entry + 10);
    const std::size_t offset = Read32(entry + 12);
    const std::size_t stream_size = Read32(entry + 16);
    if (!IsFullPose(frame) || colours == 0 || colours > 255 || w <= 0 || h <= 0 ||
        x + w > canvas_w_ || y + h > canvas_h_ || offset > size ||
        colours * 4 + stream_size > size - offset) {
        return false;
    }
    const std::size_t pixels = static_cast<std::size_t>(w) * h;
    auto* indices = static_cast<uint8_t*>(alloc_(pixels));
    MascotSprite sprite;
    sprite.x = x;
    sprite.y = y;
    sprite.w = w;
    sprite.h = h;
    sprite.rgb = static_cast<uint16_t*>(alloc_(pixels * 2));
    sprite.alpha = static_cast<uint8_t*>(alloc_(pixels));
    sprite.span = static_cast<uint16_t*>(alloc_(static_cast<std::size_t>(h) * 4));
    const uint8_t* palette = data + offset;
    const bool ok = indices != nullptr && sprite.rgb != nullptr && sprite.alpha != nullptr &&
                    sprite.span != nullptr &&
                    inflate_(palette + colours * 4, stream_size, indices, pixels);
    if (ok) {
        uint16_t rgb[256] = {};
        uint8_t alpha[256] = {};
        for (std::size_t c = 0; c < colours; ++c) {
            rgb[c] = Read16(palette + c * 4);
            alpha[c] = palette[c * 4 + 2];
        }
        for (std::size_t p = 0; p < pixels; ++p) {
            const uint8_t index = indices[p];
            // Out-of-palette indices (corrupt data) read as transparent.
            sprite.rgb[p] = index < colours ? rgb[index] : 0;
            sprite.alpha[p] = index < colours ? alpha[index] : 0;
        }
        ComputeSpans(&sprite);
    }
    if (indices != nullptr) {
        free_(indices);
    }
    if (!ok) {
        if (sprite.rgb != nullptr) {
            free_(sprite.rgb);
        }
        if (sprite.alpha != nullptr) {
            free_(sprite.alpha);
        }
        if (sprite.span != nullptr) {
            free_(sprite.span);
        }
        return false;
    }
    const auto slot = static_cast<std::size_t>(frame);
    full_[slot] = sprite;
    full_present_[slot] = true;
    return true;
}

bool MascotPack::DecodePatch(MascotFrame frame, const uint8_t* entry, const uint8_t* data,
                             std::size_t size) {
    const uint8_t base = entry[1];
    const std::size_t colours = Read16(entry + 2);
    const int x = Read16(entry + 4);
    const int y = Read16(entry + 6);
    const int w = Read16(entry + 8);
    const int h = Read16(entry + 10);
    const std::size_t offset = Read32(entry + 12);
    const std::size_t stream_size = Read32(entry + 16);
    if (IsFullPose(frame) || base >= kMascotFrameCount ||
        !IsFullPose(static_cast<MascotFrame>(base)) || colours > 255 || w <= 0 || h <= 0 ||
        x + w > canvas_w_ || y + h > canvas_h_ || offset > size ||
        colours * 4 + stream_size > size - offset) {
        return false;
    }
    Patch& patch = patch_[static_cast<std::size_t>(frame)];
    const std::size_t pixels = static_cast<std::size_t>(w) * h;
    patch.indices = static_cast<uint8_t*>(alloc_(pixels));
    const uint8_t* palette = data + offset;
    if (patch.indices == nullptr ||
        !inflate_(palette + colours * 4, stream_size, patch.indices, pixels)) {
        if (patch.indices != nullptr) {
            free_(patch.indices);
        }
        patch = Patch{};
        return false;
    }
    for (std::size_t c = 0; c < colours; ++c) {
        patch.rgb[c] = Read16(palette + c * 4);
        patch.alpha[c] = palette[c * 4 + 2];
    }
    for (std::size_t p = 0; p < pixels; ++p) {
        if (patch.indices[p] != kKeepIndex && patch.indices[p] >= colours) {
            patch.indices[p] = kKeepIndex;
        }
    }
    patch.base = static_cast<MascotFrame>(base);
    patch.x = x;
    patch.y = y;
    patch.w = w;
    patch.h = h;
    patch.present = true;
    return true;
}

const MascotSprite* MascotPack::Frame(MascotFrame frame) {
    if (!loaded_ || frame >= MascotFrame::kCount) {
        return nullptr;
    }
    const auto slot = static_cast<std::size_t>(frame);
    if (full_present_[slot]) {
        return &full_[slot];
    }
    const Patch& patch = patch_[slot];
    if (!patch.present || !full_present_[static_cast<std::size_t>(patch.base)]) {
        const MascotFrame fallback = patch.present ? patch.base : MascotFrame::kDefault;
        const auto fallback_slot = static_cast<std::size_t>(fallback);
        return full_present_[fallback_slot] ? &full_[fallback_slot]
                                            : &full_[static_cast<std::size_t>(MascotFrame::kDefault)];
    }
    if (scratch_frame_ == frame) {
        return &scratch_;
    }
    const MascotSprite& base = full_[static_cast<std::size_t>(patch.base)];
    const std::size_t pixels = static_cast<std::size_t>(base.w) * base.h;
    std::memcpy(scratch_.rgb, base.rgb, pixels * 2);
    std::memcpy(scratch_.alpha, base.alpha, pixels);
    scratch_.x = base.x;
    scratch_.y = base.y;
    scratch_.w = base.w;
    scratch_.h = base.h;
    for (int row = 0; row < patch.h; ++row) {
        const int cy = patch.y + row - base.y;
        if (cy < 0 || cy >= base.h) {
            continue;
        }
        const uint8_t* src = patch.indices + static_cast<std::size_t>(row) * patch.w;
        for (int col = 0; col < patch.w; ++col) {
            const uint8_t index = src[col];
            const int cx = patch.x + col - base.x;
            if (index == kKeepIndex || cx < 0 || cx >= base.w) {
                continue;
            }
            const std::size_t p = static_cast<std::size_t>(cy) * base.w + cx;
            scratch_.rgb[p] = patch.rgb[index];
            scratch_.alpha[p] = patch.alpha[index];
        }
    }
    ComputeSpans(&scratch_);
    scratch_frame_ = frame;
    return &scratch_;
}

bool LoadBrandMark(const uint8_t* data, std::size_t size, MascotAllocFn alloc, MascotFreeFn free,
                   MascotInflateFn inflate, BrandMark* out) {
    if (out == nullptr || data == nullptr || size < 12 || std::memcmp(data, "MMA1", 4) != 0) {
        return false;
    }
    const int w = Read16(data + 4);
    const int h = Read16(data + 6);
    const std::size_t stream_size = Read32(data + 8);
    if (w <= 0 || h <= 0 || w > kMaxCanvas || h > kMaxCanvas || stream_size > size - 12) {
        return false;
    }
    auto* alpha = static_cast<uint8_t*>(alloc(static_cast<std::size_t>(w) * h));
    if (alpha == nullptr) {
        return false;
    }
    if (!inflate(data + 12, stream_size, alpha, static_cast<std::size_t>(w) * h)) {
        free(alpha);
        return false;
    }
    out->w = w;
    out->h = h;
    out->alpha = alpha;
    return true;
}

}  // namespace memoria
