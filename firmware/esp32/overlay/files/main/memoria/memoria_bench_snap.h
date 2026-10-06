#pragma once

// The serial screenshot of a bench build (TODOLIST M-2): how one frame travels as log lines, and how the
// bytes of a picture are cut, checked and encoded. Nothing here is compiled into a product image: only the
// bench code in memoria_mascot_bench.cc includes it, and that file is empty without
// CONFIG_MEMORIA_BENCH_SERIAL.
//
// The robot has no file system to put a picture in and the USB port is the only wire, so the frame goes out
// as ordinary log lines the resident serial logger already records:
//
//   SNAP <id> <w>x<h> <seq>/<total> <crc> <base64>      one chunk, seq counts from 1
//   SNAP <id> end <total> <bytes> <crc> fmt=rgb565le    after the last chunk
//   SNAP <id> failed reason=<why>                       nothing was sent
//
// <id> is 8 hex digits (uptime in ms when the snapshot was taken, so it differs between snapshots and
// between boots); <crc> is the 8-hex-digit CRC-32 that zlib.crc32 computes, of the chunk's own bytes on a
// chunk line and of the whole picture on the end line. The picture is rows of little-endian RGB565 pixels,
// row after row, padding between rows removed. Every chunk is base64 on its own, so a lost or damaged line
// costs one chunk and the script that rebuilds the PNG (scripts/snap_to_png.py) says which one.
//
// This header has no ESP-IDF dependency so the host tests compile the exact encoder and the PC script is
// checked against its real output.

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>

namespace memoria {
namespace bench {

// Bytes of picture per chunk line: a multiple of 3, so no chunk but the last carries base64 padding, and
// small enough that one log line (about 560 characters) never holds the console for long.
constexpr std::size_t kSnapChunkBytes = 384;
// Bytes of one RGB565 pixel.
constexpr std::size_t kSnapPixelBytes = 2;

// Characters Base64Encode writes for `length` bytes (the NUL not counted).
constexpr std::size_t Base64Length(std::size_t length) { return (length + 2) / 3 * 4; }

// Room for any line of this protocol, NUL included: the longest prefix the numbers can make (a 32-bit id,
// two 32-bit sizes and two 64-bit counters come to under 90 characters) plus a full chunk of base64.
constexpr std::size_t kSnapLineCapacity = 96 + Base64Length(kSnapChunkBytes) + 1;

// CRC-32 (IEEE 802.3, reflected, polynomial 0xEDB88320: the one zlib.crc32 computes), bit by bit. A screenshot
// is a few hundred kilobytes taken by hand now and then, so a table would only cost flash. Start from 0 and
// feed the result back in to continue over the next piece.
inline std::uint32_t Crc32(std::uint32_t crc, const std::uint8_t* data, std::size_t length) {
    crc = ~crc;
    for (std::size_t i = 0; i < length; ++i) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    return ~crc;
}

// Standard base64 with '=' padding; writes Base64Length(length) characters and a NUL, returns the count.
inline std::size_t Base64Encode(const std::uint8_t* data, std::size_t length, char* out) {
    static const char kAlphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::size_t written = 0;
    for (std::size_t i = 0; i < length; i += 3) {
        const std::uint32_t b0 = data[i];
        const std::uint32_t b1 = i + 1 < length ? data[i + 1] : 0u;
        const std::uint32_t b2 = i + 2 < length ? data[i + 2] : 0u;
        const std::uint32_t triple = (b0 << 16) | (b1 << 8) | b2;
        out[written++] = kAlphabet[(triple >> 18) & 63u];
        out[written++] = kAlphabet[(triple >> 12) & 63u];
        out[written++] = i + 1 < length ? kAlphabet[(triple >> 6) & 63u] : '=';
        out[written++] = i + 2 < length ? kAlphabet[triple & 63u] : '=';
    }
    out[written] = '\0';
    return written;
}

// Chunks a picture of `bytes` bytes is cut into.
constexpr std::size_t SnapChunkCount(std::size_t bytes) {
    return (bytes + kSnapChunkBytes - 1) / kSnapChunkBytes;
}

// Copies up to `want` bytes of the picture's byte stream, starting `offset` bytes in, into `out`. The picture
// is `rows` rows of `row_bytes` bytes that sit `stride` bytes apart in memory (LVGL pads rows); the padding is
// not part of the stream. Returns the bytes copied: fewer than `want` only at the end of the picture.
inline std::size_t GatherPicture(const std::uint8_t* base, std::size_t stride, std::size_t row_bytes,
                                 std::size_t rows, std::size_t offset, std::uint8_t* out,
                                 std::size_t want) {
    if (base == nullptr || out == nullptr || row_bytes == 0 || stride < row_bytes) {
        return 0;
    }
    const std::size_t total = row_bytes * rows;
    if (offset >= total) {
        return 0;
    }
    if (want > total - offset) {
        want = total - offset;
    }
    std::size_t copied = 0;
    while (copied < want) {
        const std::size_t position = offset + copied;
        const std::size_t row = position / row_bytes;
        const std::size_t column = position % row_bytes;
        std::size_t take = row_bytes - column;
        if (take > want - copied) {
            take = want - copied;
        }
        std::memcpy(out + copied, base + row * stride + column, take);
        copied += take;
    }
    return copied;
}

// One chunk line (no terminator) for chunk `seq` (1-based) of `total`, carrying `length` bytes. Writes at
// most `capacity` bytes including the NUL and returns the line's length, or 0 when it does not fit or the
// arguments make no sense.
inline std::size_t SnapChunkLine(char* out, std::size_t capacity, std::uint32_t id, unsigned width,
                                 unsigned height, std::size_t seq, std::size_t total,
                                 const std::uint8_t* bytes, std::size_t length) {
    if (out == nullptr || bytes == nullptr || length == 0 || length > kSnapChunkBytes || seq == 0 ||
        seq > total) {
        return 0;
    }
    char encoded[Base64Length(kSnapChunkBytes) + 1];
    Base64Encode(bytes, length, encoded);
    const int written = std::snprintf(out, capacity, "SNAP %08x %ux%u %zu/%zu %08x %s",
                                      static_cast<unsigned>(id), width, height, seq, total,
                                      static_cast<unsigned>(Crc32(0, bytes, length)), encoded);
    if (written <= 0 || static_cast<std::size_t>(written) >= capacity) {
        return 0;
    }
    return static_cast<std::size_t>(written);
}

// The line that closes a successful stream: how many chunks and bytes were sent and the CRC of all of them.
inline std::size_t SnapEndLine(char* out, std::size_t capacity, std::uint32_t id, std::size_t total,
                               std::size_t bytes, std::uint32_t crc) {
    const int written = std::snprintf(out, capacity, "SNAP %08x end %zu %zu %08x fmt=rgb565le",
                                      static_cast<unsigned>(id), total, bytes, static_cast<unsigned>(crc));
    return written > 0 && static_cast<std::size_t>(written) < capacity ? static_cast<std::size_t>(written)
                                                                       : 0;
}

// The line that says why nothing was sent: the PC side then knows the lines it may see are not a picture.
inline std::size_t SnapFailLine(char* out, std::size_t capacity, std::uint32_t id, const char* reason) {
    const int written = std::snprintf(out, capacity, "SNAP %08x failed reason=%s", static_cast<unsigned>(id),
                                      reason != nullptr ? reason : "unknown");
    return written > 0 && static_cast<std::size_t>(written) < capacity ? static_cast<std::size_t>(written)
                                                                       : 0;
}

// Streams a whole picture of `width` x `height` RGB565 pixels whose rows sit `stride` bytes apart: every
// chunk line and then the end line, one call of `emit(line, length)` each (the caller decides how a line
// leaves: a log call, a pause now and then so the console and the watchdog keep up). A picture that cannot be
// streamed emits one failure line instead and returns false.
template <typename Emit>
bool StreamPicture(const std::uint8_t* base, std::size_t stride, unsigned width, unsigned height,
                   std::uint32_t id, Emit&& emit) {
    char line[kSnapLineCapacity];
    const std::size_t row_bytes = static_cast<std::size_t>(width) * kSnapPixelBytes;
    const char* failure = nullptr;
    if (base == nullptr) {
        failure = "no_picture";
    } else if (width == 0 || height == 0 || stride < row_bytes) {
        failure = "bad_size";
    }
    if (failure != nullptr) {
        const std::size_t length = SnapFailLine(line, sizeof(line), id, failure);
        if (length > 0) {
            emit(static_cast<const char*>(line), length);
        }
        return false;
    }

    const std::size_t bytes = row_bytes * height;
    const std::size_t total = SnapChunkCount(bytes);
    std::uint8_t chunk[kSnapChunkBytes];
    std::uint32_t picture_crc = 0;
    for (std::size_t seq = 1; seq <= total; ++seq) {
        const std::size_t offset = (seq - 1) * kSnapChunkBytes;
        const std::size_t length = GatherPicture(base, stride, row_bytes, height, offset, chunk, kSnapChunkBytes);
        const std::size_t written = SnapChunkLine(line, sizeof(line), id, width, height, seq, total, chunk, length);
        if (length == 0 || written == 0) {
            const std::size_t fail = SnapFailLine(line, sizeof(line), id, "encode");
            if (fail > 0) {
                emit(static_cast<const char*>(line), fail);
            }
            return false;
        }
        picture_crc = Crc32(picture_crc, chunk, length);
        emit(static_cast<const char*>(line), written);
    }
    const std::size_t end = SnapEndLine(line, sizeof(line), id, total, bytes, picture_crc);
    if (end == 0) {
        return false;
    }
    emit(static_cast<const char*>(line), end);
    return true;
}

}  // namespace bench
}  // namespace memoria
