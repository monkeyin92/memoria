#include "memoria_audio_frame.h"

#include <cstring>
#include <limits>

namespace memoria {
namespace {

uint16_t ReadU16(const uint8_t* input) {
    return static_cast<uint16_t>((static_cast<uint16_t>(input[0]) << 8) | input[1]);
}

uint32_t ReadU32(const uint8_t* input) {
    return (static_cast<uint32_t>(input[0]) << 24) |
           (static_cast<uint32_t>(input[1]) << 16) |
           (static_cast<uint32_t>(input[2]) << 8) |
           static_cast<uint32_t>(input[3]);
}

uint64_t ReadU64(const uint8_t* input) {
    uint64_t value = 0;
    for (size_t index = 0; index < sizeof(value); ++index) {
        value = (value << 8) | input[index];
    }
    return value;
}

void WriteU16(uint8_t* output, uint16_t value) {
    output[0] = static_cast<uint8_t>(value >> 8);
    output[1] = static_cast<uint8_t>(value);
}

void WriteU32(uint8_t* output, uint32_t value) {
    output[0] = static_cast<uint8_t>(value >> 24);
    output[1] = static_cast<uint8_t>(value >> 16);
    output[2] = static_cast<uint8_t>(value >> 8);
    output[3] = static_cast<uint8_t>(value);
}

void WriteU64(uint8_t* output, uint64_t value) {
    for (size_t index = 0; index < sizeof(value); ++index) {
        output[sizeof(value) - index - 1] = static_cast<uint8_t>(value >> (index * 8));
    }
}

bool IsDirectionValid(MemoriaAudioDirection direction) {
    return direction == MemoriaAudioDirection::kUplink ||
           direction == MemoriaAudioDirection::kDownlink;
}

bool IsValidDownlinkFrameSamples(uint32_t frame_samples) {
    return frame_samples == MemoriaAudioFrame::kDownlinkFrameSamples16k ||
           frame_samples == MemoriaAudioFrame::kDownlinkFrameSamples24k;
}

}  // namespace

MemoriaAudioFrameError MemoriaAudioFrame::Validate(
    const MemoriaAudioFrameMetadata& metadata,
    size_t payload_size) {
    if (!IsDirectionValid(metadata.direction)) {
        return MemoriaAudioFrameError::kInvalidDirection;
    }
    if (metadata.direction == MemoriaAudioDirection::kUplink) {
        // The discontinuity flag is a downlink-only edge marking; an uplink
        // frame carrying any flag is forged.
        if (metadata.flags != 0) {
            return MemoriaAudioFrameError::kInvalidFlags;
        }
    } else if ((metadata.flags & ~MemoriaAudioFrame::kDiscontinuityFlag) != 0) {
        // Only the discontinuity bit is defined for downlink; any other bit
        // would be a forged marker and is rejected at the wire boundary.
        return MemoriaAudioFrameError::kInvalidFlags;
    }
    if (metadata.stream_epoch == 0) {
        return MemoriaAudioFrameError::kInvalidEpoch;
    }
    if (metadata.sequence > std::numeric_limits<uint32_t>::max()) {
        return MemoriaAudioFrameError::kInvalidSequence;
    }
    if (metadata.sample_start >
        std::numeric_limits<uint64_t>::max() - metadata.frame_samples) {
        return MemoriaAudioFrameError::kInvalidSampleStart;
    }
    if (metadata.direction == MemoriaAudioDirection::kUplink &&
        metadata.frame_samples != MemoriaAudioFrame::kUplinkFrameSamples) {
        return MemoriaAudioFrameError::kInvalidFrameSamples;
    }
    if (metadata.direction == MemoriaAudioDirection::kDownlink &&
        !IsValidDownlinkFrameSamples(metadata.frame_samples)) {
        return MemoriaAudioFrameError::kInvalidFrameSamples;
    }
    if (payload_size == 0 || payload_size > kMaxPayloadBytes || payload_size > UINT16_MAX) {
        return MemoriaAudioFrameError::kInvalidPayload;
    }
    if (metadata.direction == MemoriaAudioDirection::kUplink && metadata.generation_id != 0) {
        return MemoriaAudioFrameError::kInvalidPayload;
    }
    return MemoriaAudioFrameError::kOk;
}

MemoriaAudioFrameError MemoriaAudioFrame::Encode(
    const MemoriaAudioFrameMetadata& metadata,
    const uint8_t* payload,
    size_t payload_size,
    uint8_t* output,
    size_t output_capacity,
    size_t* output_size) {
    if (output == nullptr || output_size == nullptr ||
        (payload == nullptr && payload_size != 0)) {
        return MemoriaAudioFrameError::kInvalidArgument;
    }
    const MemoriaAudioFrameError validation = Validate(metadata, payload_size);
    if (validation != MemoriaAudioFrameError::kOk) {
        return validation;
    }
    if (output_capacity < kHeaderSize + payload_size) {
        return MemoriaAudioFrameError::kBufferTooSmall;
    }

    output[0] = kVersion;
    output[1] = static_cast<uint8_t>(metadata.direction);
    WriteU16(output + 2, metadata.flags);
    WriteU32(output + 4, metadata.stream_epoch);
    WriteU32(output + 8, static_cast<uint32_t>(metadata.sequence));
    WriteU64(output + 12, metadata.sample_start);
    WriteU32(output + 20, metadata.frame_samples);
    WriteU32(output + 24, metadata.generation_id);
    WriteU16(output + 28, static_cast<uint16_t>(payload_size));
    std::memcpy(output + kHeaderSize, payload, payload_size);
    *output_size = kHeaderSize + payload_size;
    return MemoriaAudioFrameError::kOk;
}

MemoriaAudioFrameError MemoriaAudioFrame::Decode(
    const uint8_t* input,
    size_t input_size,
    MemoriaAudioFrameMetadata* metadata,
    const uint8_t** payload,
    size_t* payload_size) {
    if (input == nullptr || metadata == nullptr || payload == nullptr || payload_size == nullptr) {
        return MemoriaAudioFrameError::kInvalidArgument;
    }
    if (input_size < kHeaderSize) {
        return MemoriaAudioFrameError::kHeaderIncomplete;
    }
    if (input[0] != kVersion) {
        return MemoriaAudioFrameError::kUnsupportedVersion;
    }
    const uint8_t direction = input[1];
    if (direction != static_cast<uint8_t>(MemoriaAudioDirection::kUplink) &&
        direction != static_cast<uint8_t>(MemoriaAudioDirection::kDownlink)) {
        return MemoriaAudioFrameError::kInvalidDirection;
    }

    MemoriaAudioFrameMetadata decoded{};
    decoded.direction = static_cast<MemoriaAudioDirection>(direction);
    decoded.flags = ReadU16(input + 2);
    decoded.stream_epoch = ReadU32(input + 4);
    decoded.sequence = static_cast<uint64_t>(ReadU32(input + 8));
    decoded.sample_start = ReadU64(input + 12);
    decoded.frame_samples = ReadU32(input + 20);
    decoded.generation_id = ReadU32(input + 24);
    const size_t declared_payload_size = ReadU16(input + 28);
    if (declared_payload_size != input_size - kHeaderSize) {
        return MemoriaAudioFrameError::kPayloadLengthMismatch;
    }

    const MemoriaAudioFrameError validation = Validate(decoded, declared_payload_size);
    if (validation != MemoriaAudioFrameError::kOk) {
        return validation;
    }
    *metadata = decoded;
    *payload = input + kHeaderSize;
    *payload_size = declared_payload_size;
    return MemoriaAudioFrameError::kOk;
}

const char* MemoriaAudioFrameErrorName(MemoriaAudioFrameError error) {
    switch (error) {
        case MemoriaAudioFrameError::kOk:
            return "ok";
        case MemoriaAudioFrameError::kInvalidArgument:
            return "invalid_argument";
        case MemoriaAudioFrameError::kBufferTooSmall:
            return "buffer_too_small";
        case MemoriaAudioFrameError::kHeaderIncomplete:
            return "header_incomplete";
        case MemoriaAudioFrameError::kUnsupportedVersion:
            return "unsupported_version";
        case MemoriaAudioFrameError::kInvalidDirection:
            return "invalid_direction";
        case MemoriaAudioFrameError::kInvalidFlags:
            return "invalid_flags";
        case MemoriaAudioFrameError::kInvalidEpoch:
            return "invalid_epoch";
        case MemoriaAudioFrameError::kInvalidSequence:
            return "invalid_sequence";
        case MemoriaAudioFrameError::kInvalidSampleStart:
            return "invalid_sample_start";
        case MemoriaAudioFrameError::kInvalidFrameSamples:
            return "invalid_frame_samples";
        case MemoriaAudioFrameError::kInvalidPayload:
            return "invalid_payload";
        case MemoriaAudioFrameError::kPayloadLengthMismatch:
            return "payload_length_mismatch";
    }
    return "unknown";
}

}  // namespace memoria
