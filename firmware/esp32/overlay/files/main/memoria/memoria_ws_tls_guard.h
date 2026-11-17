#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <string>
#include <string_view>
#include <vector>

namespace memoria {

// Bounded, non-spinning TLS write helper used by Memoria's Wi-Fi WSS transport.
//
// Why this exists:
// 1. ESP-IDF's mbedtls port (`components/mbedtls/port/net_sockets.c:194-210`)
//    implements `net_would_block()` by returning `1` whenever `errno` is
//    `EAGAIN` / `EWOULDBLOCK` without checking `O_NONBLOCK` (contrary to its
//    comment), so `write()` maps a 10-second `SO_SNDTIMEO` expiry on a
//    blocking socket to `MBEDTLS_ERR_SSL_WANT_WRITE` (`ESP_TLS_ERR_SSL_WANT_WRITE`).
// 2. Upstream `EspSsl::Send` loops unconditionally on
//    `if (ret == ESP_TLS_ERR_SSL_WANT_WRITE) continue;`, turning a stalled
//    TCP send buffer into an unbounded loop on `Application`'s main task while
//    holding `WebSocket::send_mutex_`.
// 3. When the peer sends a WebSocket Ping (`0x9`), `WebSocket::OnTcpData` on
//    `ssl_receive` replies with `SendControlFrame(0xA)` which also acquires
//    `WebSocket::send_mutex_`, blocking `ssl_receive` behind `main_task`
//    so the device stops draining the downlink TCP window and stalls in
//    `speaking` indefinitely.
//
// `WriteBoundedTls` enforces `max_wait_us` across both `WANT_WRITE` retries and
// positive partial writes (`ret > 0` while `total_sent < size`), and caps
// consecutive `WANT_WRITE` returns at `max_want_write_retries`. Because each
// synchronous `write_fn` call can block up to the socket `SO_SNDTIMEO` (`T_call`),
// an immediate full stall fails closed on the first 10s `WANT_WRITE` return,
// and slow partial writes cannot start any new `write_fn` call at or after
// `start_us + max_wait_us` (bounding total wall time to `< max_wait_us + T_call`).
inline constexpr int kTlsErrWantWrite = -0x6880;  // ESP_TLS_ERR_SSL_WANT_WRITE
inline constexpr int64_t kDefaultTlsWriteTimeoutUs = 10LL * 1000LL * 1000LL;
inline constexpr uint32_t kDefaultMaxWantWriteRetries = 2;

template <typename WriteFn, typename NowUsFn>
int WriteBoundedTls(const char* data, size_t size, WriteFn&& write_fn, NowUsFn&& now_us_fn,
                    int want_write_code = kTlsErrWantWrite,
                    int64_t max_wait_us = kDefaultTlsWriteTimeoutUs,
                    uint32_t max_want_write_retries = kDefaultMaxWantWriteRetries) {
    if (size == 0) {
        return 0;
    }
    if (data == nullptr || max_wait_us < 0 ||
        size > static_cast<size_t>(std::numeric_limits<int>::max())) {
        return -1;
    }
    const int64_t start_us = now_us_fn();
    size_t total_sent = 0;
    uint32_t want_write_retries = 0;

    while (total_sent < size) {
        const int ret = write_fn(data + total_sent, size - total_sent);
        if (ret > 0) {
            if (static_cast<size_t>(ret) > size - total_sent) {
                return -1;
            }
            total_sent += static_cast<size_t>(ret);
            want_write_retries = 0;
            if (total_sent < size && (now_us_fn() - start_us) >= max_wait_us) {
                return -1;
            }
            continue;
        }
        if (ret == want_write_code) {
            ++want_write_retries;
            const int64_t elapsed_us = now_us_fn() - start_us;
            if (want_write_retries > max_want_write_retries || elapsed_us >= max_wait_us) {
                return -1;
            }
            continue;
        }
        return ret < 0 ? ret : -1;
    }
    return static_cast<int>(total_sent);
}

// Intercepts WebSocket Ping (opcode 0x9) frames in the raw TCP byte stream on
// the receive task before forwarding the stream to `WebSocket::OnTcpData`.
//
// By rewriting the opcode of a valid unfragmented Ping control frame (`0x89`,
// payload <= 125 bytes) in place to Pong (`0x8A`) and invoking `on_ping` to
// queue the outbound Pong frame onto `MemoriaProtocol`'s owner-task transport
// queue:
// - `WebSocket::OnTcpData` never enters `case 0x9: SendControlFrame(0xA, ...)`
//   on the `ssl_receive` task, so `ssl_receive` never contends on
//   `WebSocket::send_mutex_` or performs concurrent `mbedtls_ssl_write` while
//   `main_task` is sending uplink audio/control frames.
// - `WebSocket::OnTcpData` instead sees `case 0xA` (`OnPong`), which updates
//   transport liveness (`MarkTransportAlive`) as usual.
// - Per RFC 6455 section 5.4, control frames (such as outbound Pong `0x8A`)
//   may legally interleave with data frames, and each outbound frame is sent
//   atomically on the owner task.
class WebSocketPingInterceptor {
public:
    using StreamSink = std::function<void(const std::string&)>;
    using PingSink = std::function<void(const char*, size_t)>;

    static constexpr size_t kMaxControlFramePayloadBytes = 125;
    static constexpr size_t kDefaultMaxHandshakeBytes = 4096;
    static constexpr size_t kDefaultMaxFramePayloadBytes = 65536;
    static constexpr size_t kMaxInputSliceBytes = 4096;

    explicit WebSocketPingInterceptor(
        size_t max_frame_payload_bytes = kDefaultMaxFramePayloadBytes,
        size_t max_handshake_bytes = kDefaultMaxHandshakeBytes)
        : max_frame_payload_bytes_(max_frame_payload_bytes),
          max_handshake_bytes_(max_handshake_bytes) {}

    void Reset() {
        handshake_completed_ = false;
        failed_ = false;
        pending_.clear();
    }

    bool has_error() const { return failed_; }
    bool failed() const { return failed_; }
    size_t pending_size() const { return pending_.size(); }

    // Builds a masked client-to-server WebSocket Pong control frame (RFC 6455
    // section 5.5.3: FIN=1, opcode=0xA, MASK=1, payload <= 125 bytes).
    static bool BuildMaskedPongFrame(const char* payload, size_t len, uint32_t mask_seed,
                                     std::string* out_frame) {
        if (out_frame == nullptr || len > kMaxControlFramePayloadBytes ||
            (len > 0 && payload == nullptr)) {
            return false;
        }
        out_frame->clear();
        out_frame->reserve(len + 6);
        out_frame->push_back(static_cast<char>(0x8A));
        out_frame->push_back(static_cast<char>(0x80 | static_cast<uint8_t>(len)));
        const uint8_t mask[4] = {
            static_cast<uint8_t>((mask_seed >> 24) & 0xFF),
            static_cast<uint8_t>((mask_seed >> 16) & 0xFF),
            static_cast<uint8_t>((mask_seed >> 8) & 0xFF),
            static_cast<uint8_t>(mask_seed & 0xFF),
        };
        out_frame->append(reinterpret_cast<const char*>(mask), 4);
        for (size_t i = 0; i < len; ++i) {
            out_frame->push_back(
                static_cast<char>(static_cast<uint8_t>(payload[i]) ^ mask[i % 4]));
        }
        return true;
    }

    bool OnStreamChunk(const std::string& chunk, const StreamSink& forward_stream,
                       const PingSink& on_ping) {
        if (failed_) {
            pending_.clear();
            return false;
        }
        if (chunk.empty()) {
            return true;
        }
        size_t pos = 0;
        while (pos < chunk.size()) {
            const size_t take = std::min(kMaxInputSliceBytes, chunk.size() - pos);
            if (!ProcessSlice(chunk.data() + pos, take, forward_stream, on_ping)) {
                return false;
            }
            pos += take;
        }
        return true;
    }

private:
    bool Fail() {
        failed_ = true;
        pending_.clear();
        return false;
    }

    bool ProcessSlice(const char* slice_data, size_t slice_len,
                      const StreamSink& forward_stream, const PingSink& on_ping) {
        pending_.append(slice_data, slice_len);

        std::string output;
        if (!handshake_completed_) {
            const size_t pos = pending_.find("\r\n\r\n");
            if (pos == std::string::npos) {
                if (pending_.size() > max_handshake_bytes_) {
                    return Fail();
                }
                return true;
            }
            const size_t header_end = pos + 4;
            if (header_end > max_handshake_bytes_) {
                return Fail();
            }
            output.append(pending_, 0, header_end);
            pending_.erase(0, header_end);
            const size_t line_end = output.find("\r\n");
            const std::string_view status_line(
                output.data(), line_end == std::string::npos ? output.size() : line_end);
            if (status_line.find("HTTP/1.1 101") != std::string_view::npos) {
                handshake_completed_ = true;
            } else {
                // Non-101 handshake response: forward header once so WebSocket
                // marks HANDSHAKE_FAILED_BIT, clear pending, and reject further data.
                failed_ = true;
                pending_.clear();
                if (forward_stream) {
                    forward_stream(output);
                }
                return false;
            }
        }

        size_t offset = 0;
        const size_t total = pending_.size();
        while (offset < total) {
            const uint8_t* buf = reinterpret_cast<const uint8_t*>(pending_.data() + offset);
            const size_t avail = total - offset;
            if (avail < 2) {
                break;
            }
            const uint8_t first_byte = buf[0];
            const bool fin = (first_byte & 0x80) != 0;
            const uint8_t rsv = first_byte & 0x70;
            const uint8_t opcode = first_byte & 0x0F;
            const bool is_control = (opcode & 0x08) != 0;
            const bool masked = (buf[1] & 0x80) != 0;
            uint64_t payload_len = buf[1] & 0x7F;
            size_t header_len = 2;

            if (rsv != 0 || (opcode > 0x2 && opcode < 0x8) || opcode > 0xA) {
                return Fail();
            }
            // RFC 6455 section 5.5: control frames must not be fragmented and
            // must not exceed 125 payload bytes.
            if (is_control && (!fin || payload_len > kMaxControlFramePayloadBytes)) {
                return Fail();
            }

            if (payload_len == 126) {
                if (avail < 4) {
                    break;
                }
                payload_len =
                    (static_cast<uint64_t>(buf[2]) << 8) | static_cast<uint64_t>(buf[3]);
                header_len += 2;
            } else if (payload_len == 127) {
                if (avail < 10) {
                    break;
                }
                if ((buf[2] & 0x80) != 0) {
                    return Fail();
                }
                payload_len = 0;
                for (int i = 0; i < 8; ++i) {
                    payload_len = (payload_len << 8) | static_cast<uint64_t>(buf[2 + i]);
                }
                header_len += 8;
            }

            const size_t full_header_len = header_len + (masked ? 4u : 0u);
            if (payload_len > std::numeric_limits<uint64_t>::max() - full_header_len ||
                payload_len > static_cast<uint64_t>(max_frame_payload_bytes_)) {
                return Fail();
            }

            uint8_t mask_key[4] = {0, 0, 0, 0};
            if (masked) {
                if (avail < header_len + 4) {
                    break;
                }
                for (int i = 0; i < 4; ++i) {
                    mask_key[i] = buf[header_len + i];
                }
                header_len += 4;
            }

            const size_t frame_len = header_len + static_cast<size_t>(payload_len);
            if (avail < frame_len) {
                break;
            }

            if (opcode == 0x9) {
                std::vector<char> unmasked(static_cast<size_t>(payload_len));
                for (size_t i = 0; i < payload_len; ++i) {
                    uint8_t b = buf[header_len + i];
                    if (masked) {
                        b ^= mask_key[i % 4];
                    }
                    unmasked[i] = static_cast<char>(b);
                }
                if (on_ping) {
                    on_ping(unmasked.data(), unmasked.size());
                }
                // Rewrite opcode 0x9 (Ping) to 0xA (Pong) so WebSocket::OnTcpData
                // marks liveness via OnPong without calling SendControlFrame on
                // the receive task.
                const size_t frame_start_in_output = output.size();
                output.append(pending_, offset, frame_len);
                output[frame_start_in_output] =
                    static_cast<char>((first_byte & 0xF0) | 0x0A);
            } else {
                output.append(pending_, offset, frame_len);
            }
            offset += frame_len;
        }

        if (offset > 0) {
            pending_.erase(0, offset);
        }
        if (pending_.size() > max_frame_payload_bytes_ + 14u) {
            return Fail();
        }
        if (!output.empty() && forward_stream) {
            forward_stream(output);
        }
        return true;
    }

    size_t max_frame_payload_bytes_ = kDefaultMaxFramePayloadBytes;
    size_t max_handshake_bytes_ = kDefaultMaxHandshakeBytes;
    bool handshake_completed_ = false;
    bool failed_ = false;
    std::string pending_;
};

}  // namespace memoria
