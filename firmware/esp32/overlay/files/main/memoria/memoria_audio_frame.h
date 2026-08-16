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
    // Header flags bit 0 (0x0001): downlink-only discontinuity marker the
    // edge sets at socket-write time on the first frame after a
    // same-generation forward gap caused by queue drops. Uplink frames must
    // carry flags 0; any other bit is forged.
    static constexpr uint16_t kDiscontinuityFlag = 0x0001;
    static constexpr uint32_t kUplinkFrameSamples = 320;
    // 20 ms frames at the negotiated downlink rate. The session layer picks
    // one of these after session.accepted v2; v1 sessions always use 24 kHz.
    static constexpr uint32_t kDownlinkFrameSamples16k = 320;
    static constexpr uint32_t kDownlinkFrameSamples24k = 480;

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
