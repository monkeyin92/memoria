#ifndef MEMORIA_MASCOT_PACK_H
#define MEMORIA_MASCOT_PACK_H

#include <cstddef>
#include <cstdint>

namespace memoria {

// Companion mascot frames for the 360x360 round LCD, packed on the host by
// firmware/esp32/scripts/build_mascot_pack.py into mascot_<id>.mmp in the
// assets partition.
//
// File layout (little endian):
//   header, 64 bytes:
//     char     magic[4]      "MMP1"
//     uint16   version       1
//     uint16   frame_count
//     uint16   canvas_w, canvas_h
//     uint16   foot_y        canvas row of the default pose's feet
//     uint16   foot_half_w   half width of the default pose near the feet
//     uint32   bg_inner, bg_outer, ink, primary, accent   (0xRRGGBB)
//     char     id[16]        companion id, NUL padded
//     uint8    reserved[12]
//   frame_count entries, 24 bytes each:
//     uint8    frame         MascotFrame
//     uint8    base          0xFF for a full frame, else the frame it patches
//     uint16   colours       palette entries (<= 255)
//     uint16   x, y, w, h    rectangle in canvas coordinates
//     uint32   offset        from file start: palette, then the zlib stream
//     uint32   stream_size   zlib bytes after the palette
//     uint32   reserved
//   palette: colours x {uint16 rgb565, uint8 alpha, uint8 pad}
//   zlib stream: w*h palette indices; in a patch, index 255 keeps the base pixel.
//
// This header and memoria_mascot_pack.cc have no ESP-IDF or LVGL dependency:
// the zlib decoder and allocator are injected so the same code runs on the
// device (ROM tinfl, PSRAM) and in the host tests.

enum class MascotFrame : uint8_t {
    kDefault = 0,
    kHappy,
    kSad,
    kSurprised,
    kThinking,
    kListening,
    kSleepy,
    kDizzy,
    kGreeting,
    kDefaultBlink,
    kSadBlink,
    kSurprisedBlink,
    kThinkingBlink,
    kListeningBlink,
    kDefaultTalk,
    kHappyTalk,
    kSadTalk,
    kSurprisedTalk,
    kThinkingTalk,
    kCount,
};

constexpr std::size_t kMascotFrameCount = static_cast<std::size_t>(MascotFrame::kCount);

// The blink / talk variant of a pose, or kCount when the pose has none.
MascotFrame BlinkFrameFor(MascotFrame pose);
MascotFrame TalkFrameFor(MascotFrame pose);

struct MascotTheme {
    uint32_t bg_inner = 0xF4F6FB;
    uint32_t bg_outer = 0xDDE6F5;
    uint32_t ink = 0x22344D;
    uint32_t primary = 0x3D5A80;
    uint32_t accent = 0xF2C14E;
};

// A decoded frame: premultiplication-free RGB565 plus 8-bit alpha, stored as
// two planes of w*h, positioned at (x, y) in canvas coordinates.
struct MascotSprite {
    int x = 0;
    int y = 0;
    int w = 0;
    int h = 0;
    uint16_t* rgb = nullptr;
    uint8_t* alpha = nullptr;
    // Per row: [first, last + 1) columns with alpha > 0 (empty rows: 0, 0).
    uint16_t* span = nullptr;
};

// Inflate a complete zlib stream into exactly out_size bytes.
using MascotInflateFn = bool (*)(const uint8_t* in, std::size_t in_size, uint8_t* out,
                                 std::size_t out_size);
using MascotAllocFn = void* (*)(std::size_t bytes);
using MascotFreeFn = void (*)(void* ptr);

class MascotPack {
public:
    MascotPack(MascotAllocFn alloc, MascotFreeFn free, MascotInflateFn inflate);
    ~MascotPack();
    MascotPack(const MascotPack&) = delete;
    MascotPack& operator=(const MascotPack&) = delete;

    // Parse and decode every frame. The source bytes are not referenced
    // afterwards, so they may live in memory-mapped flash that is unmapped
    // later. Returns false (and holds nothing) on any malformed input.
    bool Load(const uint8_t* data, std::size_t size);
    bool loaded() const { return loaded_; }

    const char* id() const { return id_; }
    const MascotTheme& theme() const { return theme_; }
    int canvas_w() const { return canvas_w_; }
    int canvas_h() const { return canvas_h_; }
    int foot_y() const { return foot_y_; }
    int foot_half_w() const { return foot_half_w_; }

    // The frame ready to draw. Patch frames are composed over their base into
    // an internal scratch sprite, so the pointer stays valid only until the
    // next Frame() call for a different patch frame. Missing frames fall back
    // to their base, and then to kDefault.
    const MascotSprite* Frame(MascotFrame frame);

private:
    struct Patch {
        MascotFrame base = MascotFrame::kCount;
        int x = 0;
        int y = 0;
        int w = 0;
        int h = 0;
        uint8_t* indices = nullptr;
        uint16_t rgb[255] = {};
        uint8_t alpha[255] = {};
        bool present = false;
    };

    void Reset();
    bool DecodeFull(MascotFrame frame, const uint8_t* entry, const uint8_t* data, std::size_t size);
    bool DecodePatch(MascotFrame frame, const uint8_t* entry, const uint8_t* data,
                     std::size_t size);

    MascotAllocFn alloc_;
    MascotFreeFn free_;
    MascotInflateFn inflate_;
    bool loaded_ = false;
    char id_[17] = {};
    MascotTheme theme_;
    int canvas_w_ = 0;
    int canvas_h_ = 0;
    int foot_y_ = 0;
    int foot_half_w_ = 0;
    MascotSprite full_[kMascotFrameCount];
    bool full_present_[kMascotFrameCount] = {};
    Patch patch_[kMascotFrameCount];
    MascotSprite scratch_;
    MascotFrame scratch_frame_ = MascotFrame::kCount;
};

// The boot wordmark: an 8-bit alpha mask from brand_mark.mma
// ("MMA1", uint16 w, uint16 h, uint32 zlib size, zlib stream of w*h bytes).
struct BrandMark {
    int w = 0;
    int h = 0;
    uint8_t* alpha = nullptr;
};

bool LoadBrandMark(const uint8_t* data, std::size_t size, MascotAllocFn alloc, MascotFreeFn free,
                   MascotInflateFn inflate, BrandMark* out);

}  // namespace memoria

#endif  // MEMORIA_MASCOT_PACK_H
