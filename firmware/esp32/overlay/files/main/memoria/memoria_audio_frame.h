#pragma once

#include <cstddef>
#include <cstdint>

namespace memoria {

enum class MemoriaAudioDirection : uint8_t {
    kUplink = 1,
    kDownlink = 2,
};

enum class MemoriaAudioFrameError : uint8_t {
    kOk = 0,
    kInvalidArgument,
    kBufferTooSmall,
    kHeaderIncomplete,
    kUnsupportedVersion,
    kInvalidDirection,
    kInvalidFlags,
    kInvalidEpoch,
    kInvalidSequence,
    kInvalidSampleStart,
    kInvalidFrameSamples,
    kInvalidPayload,
    kPayloadLengthMismatch,
};

struct MemoriaAudioFrameMetadata {
    MemoriaAudioDirection direction = MemoriaAudioDirection::kUplink;
    uint16_t flags = 0;
    uint32_t stream_epoch = 0;
    uint64_t sequence = 0;
    uint64_t sample_start = 0;
    uint32_t frame_samples = 0;
    uint32_t generation_id = 0;
};

#pragma pack(push, 1)
struct MemoriaAudioFrameHeaderV1 {
    uint8_t version;
    uint8_t direction;
    uint16_t flags;
    uint32_t stream_epoch;
    uint32_t sequence;
    uint64_t sample_start;
    uint32_t frame_samples;
    uint32_t generation_id;
    uint16_t payload_size;
};
#pragma pack(pop)

static_assert(sizeof(MemoriaAudioFrameHeaderV1) == 30,
              "MemoriaAudioFrameV1 header must be exactly 30 bytes");

class MemoriaAudioFrame final {
public:
    static constexpr uint8_t kVersion = 1;
    static constexpr size_t kHeaderSize = sizeof(MemoriaAudioFrameHeaderV1);
    static constexpr size_t kMaxPayloadBytes = 4096;
    static constexpr uint32_t kUplinkFrameSamples = 320;
    static constexpr uint32_t kDownlinkFrameSamples = 480;

    static MemoriaAudioFrameError Encode(const MemoriaAudioFrameMetadata& metadata,
                                         const uint8_t* payload,
                                         size_t payload_size,
                                         uint8_t* output,
                                         size_t output_capacity,
                                         size_t* output_size);

    static MemoriaAudioFrameError Decode(const uint8_t* input,
                                         size_t input_size,
                                         MemoriaAudioFrameMetadata* metadata,
                                         const uint8_t** payload,
                                         size_t* payload_size);

    static MemoriaAudioFrameError Validate(const MemoriaAudioFrameMetadata& metadata,
                                           size_t payload_size);
};

const char* MemoriaAudioFrameErrorName(MemoriaAudioFrameError error);

}  // namespace memoria
