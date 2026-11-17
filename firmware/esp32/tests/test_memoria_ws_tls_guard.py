"""Compile and run host tests for Memoria's bounded TLS write and WebSocket Ping interceptor."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).parents[1]
GUARD_HEADER = ROOT / "overlay/files/main/memoria/memoria_ws_tls_guard.h"
PROTOCOL_SOURCE = ROOT / "overlay/files/main/memoria/memoria_protocol.cc"
PROTOCOL_HEADER = ROOT / "overlay/files/main/memoria/memoria_protocol.h"


def function(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def class_block(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    assert source[end] == ";"
    return source[start : end + 1]


def run_cpp(tmp_path: Path, source: str) -> str:
    compiler = shutil.which("clang++") or shutil.which("g++")
    assert compiler, "host compiler required"
    cpp, binary = tmp_path / "probe.cc", tmp_path / "probe"
    cpp.write_text(source, encoding="utf-8")
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-pthread",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{GUARD_HEADER.parent}",
            str(cpp),
            "-o",
            str(binary),
        ],
        check=True,
    )
    result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_write_bounded_tls_bounds_want_write_and_handles_partial_writes(tmp_path: Path) -> None:
    output = run_cpp(
        tmp_path,
        r"""
#include "memoria_ws_tls_guard.h"
#include <cassert>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

using memoria::WriteBoundedTls;
using memoria::kTlsErrWantWrite;

int main() {
    // 1. Empty and null checks.
    int64_t now_us = 1000;
    assert(WriteBoundedTls(nullptr, 0, [](const char*, size_t) { return 0; }, [&]() { return now_us; }) == 0);
    assert(WriteBoundedTls(nullptr, 4, [](const char*, size_t) { return 4; }, [&]() { return now_us; }) == -1);

    // 2. Partial writes with a transient WANT_WRITE in between reset retry counter and succeed.
    std::string sent;
    int call = 0;
    const std::string payload = "hello-world-tls-frame";
    int res = WriteBoundedTls(
        payload.data(), payload.size(),
        [&](const char* ptr, size_t len) -> int {
            ++call;
            if (call == 1) {
                sent.append(ptr, 5);
                return 5;
            }
            if (call == 2) {
                now_us += 1000 * 1000; // 1s
                return kTlsErrWantWrite;
            }
            if (call == 3) {
                sent.append(ptr, 6);
                return 6;
            }
            if (call == 4) {
                now_us += 1000 * 1000; // 1s
                return kTlsErrWantWrite;
            }
            sent.append(ptr, len);
            return static_cast<int>(len);
        },
        [&]() { return now_us; },
        kTlsErrWantWrite,
        10LL * 1000LL * 1000LL,
        1);
    assert(res == static_cast<int>(payload.size()));
    assert(sent == payload);

    // 3. Reproduce ESP-IDF net_would_block + SO_SNDTIMEO 10s expiry returning WANT_WRITE:
    //    First WANT_WRITE at +10s immediately trips max_wait_us (10s) and fails closed (-1)
    //    instead of spinning forever like upstream EspSsl::Send.
    int spin_calls = 0;
    now_us = 5000000;
    int stall_res = WriteBoundedTls(
        payload.data(), payload.size(),
        [&](const char*, size_t) -> int {
            ++spin_calls;
            now_us += 10LL * 1000LL * 1000LL; // 10s SO_SNDTIMEO expired
            return kTlsErrWantWrite;
        },
        [&]() { return now_us; });
    assert(stall_res == -1);
    assert(spin_calls == 1);

    // 4. Rapid consecutive WANT_WRITE without advancing clock trips max_want_write_retries (2).
    spin_calls = 0;
    now_us = 1000;
    int retry_res = WriteBoundedTls(
        payload.data(), payload.size(),
        [&](const char*, size_t) -> int {
            ++spin_calls;
            now_us += 100; // 100 us
            return kTlsErrWantWrite;
        },
        [&]() { return now_us; });
    assert(retry_res == -1);
    assert(spin_calls == 3); // initial + 2 retries, 3rd WANT_WRITE fails closed

    // 5. Slow partial writes (1 byte per 4s call on a 10-byte payload) cannot bypass
    //    the cumulative 10s max_wait_us deadline when bytes remain unsent.
    int partial_calls = 0;
    now_us = 1000;
    int partial_deadline_res = WriteBoundedTls(
        "0123456789", 10,
        [&](const char*, size_t) -> int {
            ++partial_calls;
            now_us += 4LL * 1000LL * 1000LL; // +4s per 1-byte partial write
            return 1;
        },
        [&]() { return now_us; },
        kTlsErrWantWrite,
        10LL * 1000LL * 1000LL);
    assert(partial_deadline_res == -1);
    assert(partial_calls == 3); // 3 * 4s = 12s >= 10s with 7 bytes still remaining

    // 6. Bogus write_fn return value (> remaining) fails closed.
    now_us = 1000;
    assert(WriteBoundedTls(
        "abcd", 4,
        [&](const char*, size_t) -> int { return 5; },
        [&]() { return now_us; }) == -1);

    std::puts("WriteBoundedTls checks passed");
    return 0;
}
""",
    )
    print(output.strip())


def test_websocket_ping_interceptor_rewrites_ping_and_preserves_frames(tmp_path: Path) -> None:
    output = run_cpp(
        tmp_path,
        r"""
#include "memoria_ws_tls_guard.h"
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

using memoria::WebSocketPingInterceptor;

static std::string MakeWsFrame(uint8_t opcode, const std::string& payload, bool masked = false, uint32_t mask_seed = 0x12345678u, bool fin = true) {
    std::string out;
    out.push_back(static_cast<char>((fin ? 0x80 : 0x00) | (opcode & 0x0F)));
    uint8_t second = masked ? 0x80 : 0x00;
    if (payload.size() < 126) {
        out.push_back(static_cast<char>(second | static_cast<uint8_t>(payload.size())));
    } else {
        out.push_back(static_cast<char>(second | 126));
        out.push_back(static_cast<char>((payload.size() >> 8) & 0xFF));
        out.push_back(static_cast<char>(payload.size() & 0xFF));
    }
    uint8_t mask[4] = {
        static_cast<uint8_t>((mask_seed >> 24) & 0xFF),
        static_cast<uint8_t>((mask_seed >> 16) & 0xFF),
        static_cast<uint8_t>((mask_seed >> 8) & 0xFF),
        static_cast<uint8_t>(mask_seed & 0xFF),
    };
    if (masked) {
        out.append(reinterpret_cast<const char*>(mask), 4);
    }
    for (size_t i = 0; i < payload.size(); ++i) {
        uint8_t b = static_cast<uint8_t>(payload[i]);
        if (masked) {
            b ^= mask[i % 4];
        }
        out.push_back(static_cast<char>(b));
    }
    return out;
}

static std::string MakeWs64Header(uint8_t opcode, uint64_t payload_len) {
    std::string out;
    out.push_back(static_cast<char>(0x80 | (opcode & 0x0F)));
    out.push_back(static_cast<char>(127));
    for (int shift = 56; shift >= 0; shift -= 8) {
        out.push_back(static_cast<char>((payload_len >> shift) & 0xFF));
    }
    return out;
}

int main() {
    WebSocketPingInterceptor interceptor;
    std::string forwarded;
    std::vector<std::string> pings;

    auto on_stream = [&](const std::string& chunk) { forwarded.append(chunk); };
    auto on_ping = [&](const char* data, size_t len) { pings.emplace_back(data, len); };

    const std::string handshake =
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n\r\n";
    const std::string audio_frame_1 = MakeWsFrame(0x2, std::string(160, '\x42'));
    const std::string ping_empty = MakeWsFrame(0x9, "");
    const std::string ping_payload = MakeWsFrame(0x9, "edge-hb-25", true, 0xCAFEBABEu);
    const std::string text_completed = MakeWsFrame(0x1, "{\"type\":\"generation.completed\"}");

    // Feed the stream in 3-byte fragments across handshake, binary audio, empty ping,
    // masked payload ping, and generation.completed JSON frame.
    const std::string wire = handshake + audio_frame_1 + ping_empty + ping_payload + text_completed;
    for (size_t i = 0; i < wire.size(); i += 3) {
        assert(interceptor.OnStreamChunk(wire.substr(i, 3), on_stream, on_ping));
    }
    assert(interceptor.pending_size() == 0);

    assert(pings.size() == 2);
    assert(pings[0].empty());
    assert(pings[1] == "edge-hb-25");

    // Verify that the forwarded stream is byte-for-byte identical except the two Ping
    // opcodes (0x89) were rewritten in place to Pong (0x8A).
    std::string expected_forwarded =
        handshake +
        audio_frame_1 +
        MakeWsFrame(0xA, "") +
        MakeWsFrame(0xA, "edge-hb-25", true, 0xCAFEBABEu) +
        text_completed;
    assert(forwarded == expected_forwarded);

    // Verify BuildMaskedPongFrame RFC 6455 encoding and unmasking.
    std::string pong_frame;
    assert(WebSocketPingInterceptor::BuildMaskedPongFrame("edge-hb-25", 10, 0xA1B2C3D4u, &pong_frame));
    assert(pong_frame.size() == 16);
    assert(static_cast<uint8_t>(pong_frame[0]) == 0x8A);
    assert(static_cast<uint8_t>(pong_frame[1]) == (0x80 | 10));
    const uint8_t mask[4] = {0xA1, 0xB2, 0xC3, 0xD4};
    std::string unmasked;
    for (size_t i = 0; i < 10; ++i) {
        unmasked.push_back(static_cast<char>(static_cast<uint8_t>(pong_frame[6 + i]) ^ mask[i % 4]));
    }
    assert(unmasked == "edge-hb-25");

    // Payload > 125 bytes is rejected for control frames.
    std::string overlong(126, 'x');
    assert(!WebSocketPingInterceptor::BuildMaskedPongFrame(overlong.data(), overlong.size(), 1, &pong_frame));

    // Non-101 HTTP response forwards the header once (so WebSocket::OnTcpData sets
    // HANDSHAKE_FAILED_BIT), clears pending_, latches failed_, and rejects trailing data.
    {
        WebSocketPingInterceptor bad_hs;
        std::string hs_forwarded;
        const std::string resp = "HTTP/1.1 403 Forbidden\r\nContent-Length: 4\r\n\r\nbody";
        assert(!bad_hs.OnStreamChunk(resp, [&](const std::string& s) { hs_forwarded.append(s); }, on_ping));
        assert(hs_forwarded == "HTTP/1.1 403 Forbidden\r\nContent-Length: 4\r\n\r\n");
        assert(bad_hs.failed());
        assert(bad_hs.pending_size() == 0);
        assert(!bad_hs.OnStreamChunk("more", [&](const std::string& s) { hs_forwarded.append(s); }, on_ping));
    }

    // Oversized HTTP handshake (> 4096 bytes without \r\n\r\n) fails closed and clears pending_.
    {
        WebSocketPingInterceptor huge_hs;
        std::string hs_forwarded;
        assert(!huge_hs.OnStreamChunk(std::string(5000, 'A'), [&](const std::string& s) { hs_forwarded.append(s); }, on_ping));
        assert(hs_forwarded.empty());
        assert(huge_hs.failed());
        assert(huge_hs.pending_size() == 0);
    }

    // Fragmented Ping (FIN=0, opcode=0x9) is rejected immediately at the 2-byte header.
    {
        WebSocketPingInterceptor frag_ping;
        assert(frag_ping.OnStreamChunk(handshake, [](const std::string&) {}, on_ping));
        const std::string bad_frame = MakeWsFrame(0x9, "ping", false, 0, false);
        assert(!frag_ping.OnStreamChunk(bad_frame, [](const std::string&) {}, on_ping));
        assert(frag_ping.failed());
        assert(frag_ping.pending_size() == 0);
    }

    // Control frame with 16-bit extended length (len_code == 126) is rejected immediately at 2 bytes.
    {
        WebSocketPingInterceptor big_ping;
        assert(big_ping.OnStreamChunk(handshake, [](const std::string&) {}, on_ping));
        assert(!big_ping.OnStreamChunk(std::string("\x89\x7e\x00\x7e", 4), [](const std::string&) {}, on_ping));
        assert(big_ping.failed());
        assert(big_ping.pending_size() == 0);
    }

    // Reserved RSV bits and reserved opcodes are rejected immediately at 2 bytes.
    {
        WebSocketPingInterceptor bad_rsv;
        assert(bad_rsv.OnStreamChunk(handshake, [](const std::string&) {}, on_ping));
        assert(!bad_rsv.OnStreamChunk(std::string("\x91\x01x", 3), [](const std::string&) {}, on_ping));
        assert(bad_rsv.failed());

        WebSocketPingInterceptor bad_opcode;
        assert(bad_opcode.OnStreamChunk(handshake, [](const std::string&) {}, on_ping));
        assert(!bad_opcode.OnStreamChunk(std::string("\x83\x00", 2), [](const std::string&) {}, on_ping));
        assert(bad_opcode.failed());
    }

    // Oversized 64-bit payload lengths (65537, 0x100000000 - 10, UINT64_MAX - 9, MSB-set)
    // are rejected immediately upon parsing the 10-byte header, without buffering payload
    // or wrapping 64-bit arithmetic.
    for (uint64_t bad_len : {
             uint64_t{65537},
             uint64_t{0x100000000ULL - 10},
             uint64_t{UINT64_MAX - 9},
             uint64_t{1ULL << 63},
         }) {
        WebSocketPingInterceptor huge_frame;
        assert(huge_frame.OnStreamChunk(handshake, [](const std::string&) {}, on_ping));
        const std::string hdr = MakeWs64Header(0x2, bad_len);
        assert(!huge_frame.OnStreamChunk(hdr, [](const std::string&) {}, on_ping));
        assert(huge_frame.failed());
        assert(huge_frame.pending_size() == 0);
    }

    std::puts("WebSocketPingInterceptor checks passed");
    return 0;
}
""",
    )
    print(output.strip())


def test_real_memoria_ssl_transport_lifecycle_and_uaf_safety(tmp_path: Path) -> None:
    """Compile and exercise the real MemoriaSslTransport class from memoria_protocol.cc.

    Verifies:
    1. Connect sets SO_RCVTIMEO (200ms) on sockfd and spawns ssl_receive;
    2. Disconnect uses shutdown(sockfd, SHUT_RDWR) (never close(sockfd) before esp_tls_conn_destroy)
       and suppresses disconnect_callback on explicit Disconnect();
    3. When ssl_receive is blocked inside esp_tls_conn_read past Disconnect()'s wait timeout
       and MemoriaSslTransport (`this`) is destroyed, there is zero UAF on `this`, `tls_client`,
       or `event_group`, and `esp_tls_conn_destroy` + `vEventGroupDelete` run deterministically
       when ssl_receive finally exits;
    4. Self-disconnect from inside a stream callback on ssl_receive does not deadlock;
    5. xTaskCreate failure cleans up tls_client and event_group without leaking;
    6. Concurrent Send() failure and ReceiveLoop() failure fire disconnect_callback at most once.
    """
    protocol_src = PROTOCOL_SOURCE.read_text(encoding="utf-8")
    transport_cls = class_block(protocol_src, "class MemoriaSslTransport final : public Tcp")

    output = run_cpp(
        tmp_path,
        r"""
#include "memoria_ws_tls_guard.h"
#include <atomic>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <future>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <sys/socket.h>
#include <sys/time.h>
#include <thread>
#include <vector>

using esp_err_t = int;
constexpr esp_err_t ESP_OK = 0;
constexpr esp_err_t ESP_ERR_NO_MEM = 0x101;
constexpr int ESP_TLS_ERR_SSL_WANT_READ = -0x6900;
constexpr int ESP_TLS_ERR_SSL_WANT_WRITE = -0x6880;
using EventBits_t = uint32_t;
using TickType_t = uint32_t;
using BaseType_t = int;
using UBaseType_t = unsigned int;
using TaskHandle_t = void*;
constexpr EventBits_t BIT0 = 1u << 0;
constexpr BaseType_t pdFALSE = 0;
constexpr BaseType_t pdPASS = 1;
constexpr BaseType_t pdFAIL = 0;
#define pdMS_TO_TICKS(ms) (static_cast<TickType_t>(ms))
#define ESP_LOGE(tag, fmt, ...) ((void)tag)

struct FakeEventGroup {
    std::mutex mu;
    std::condition_variable cv;
    EventBits_t bits = 0;
    bool deleted = false;
};
using EventGroupHandle_t = FakeEventGroup*;

static std::atomic<int> g_event_group_created{0};
static std::atomic<int> g_event_group_deleted{0};
static std::atomic<bool> g_uaf_detected{false};

EventGroupHandle_t xEventGroupCreate() {
    ++g_event_group_created;
    return new FakeEventGroup();
}

void vEventGroupDelete(EventGroupHandle_t eg) {
    assert(eg != nullptr);
    {
        std::lock_guard<std::mutex> lock(eg->mu);
        if (eg->deleted) {
            g_uaf_detected.store(true);
        }
        eg->deleted = true;
    }
    ++g_event_group_deleted;
    delete eg;
}

void xEventGroupClearBits(EventGroupHandle_t eg, EventBits_t bits) {
    std::lock_guard<std::mutex> lock(eg->mu);
    if (eg->deleted) g_uaf_detected.store(true);
    eg->bits &= ~bits;
}

void xEventGroupSetBits(EventGroupHandle_t eg, EventBits_t bits) {
    std::lock_guard<std::mutex> lock(eg->mu);
    if (eg->deleted) g_uaf_detected.store(true);
    eg->bits |= bits;
    eg->cv.notify_all();
}

EventBits_t xEventGroupWaitBits(EventGroupHandle_t eg, EventBits_t bits_to_wait, BaseType_t, BaseType_t, TickType_t ticks) {
    std::unique_lock<std::mutex> lock(eg->mu);
    if (eg->deleted) g_uaf_detected.store(true);
    eg->cv.wait_for(lock, std::chrono::milliseconds(ticks), [&]() {
        return (eg->bits & bits_to_wait) != 0;
    });
    return eg->bits;
}

static thread_local void* g_current_task_token = nullptr;
TaskHandle_t xTaskGetCurrentTaskHandle() {
    if (g_current_task_token == nullptr) {
        static thread_local int main_token = 1;
        g_current_task_token = &main_token;
    }
    return g_current_task_token;
}

static std::atomic<bool> g_fail_task_create{false};
static std::mutex g_threads_mu;
static std::vector<std::thread> g_spawned_threads;

struct TaskExitThrow {};
void vTaskDelete(TaskHandle_t) {
    throw TaskExitThrow{};
}

BaseType_t xTaskCreate(void (*fn)(void*), const char*, uint32_t, void* arg, UBaseType_t, TaskHandle_t* out_handle) {
    if (g_fail_task_create.load()) {
        return pdFAIL;
    }
    auto ready = std::make_shared<std::promise<TaskHandle_t>>();
    auto fut = ready->get_future();
    std::thread th([fn, arg, ready]() {
        int task_token = 2;
        g_current_task_token = &task_token;
        ready->set_value(g_current_task_token);
        try {
            fn(arg);
        } catch (const TaskExitThrow&) {
        }
    });
    TaskHandle_t h = fut.get();
    if (out_handle != nullptr) {
        *out_handle = h;
    }
    std::lock_guard<std::mutex> lock(g_threads_mu);
    g_spawned_threads.push_back(std::move(th));
    return pdPASS;
}

static void JoinAllSpawnedThreads() {
    std::vector<std::thread> threads;
    {
        std::lock_guard<std::mutex> lock(g_threads_mu);
        threads.swap(g_spawned_threads);
    }
    for (auto& t : threads) {
        if (t.joinable()) t.join();
    }
}

struct esp_tls {
    int sockfd = 42;
    std::atomic<bool> destroyed{false};
};
using esp_tls_t = esp_tls;
using esp_tls_error_handle_t = void*;

struct esp_tls_cfg_t {
    esp_err_t (*crt_bundle_attach)(void*) = nullptr;
};
esp_err_t esp_crt_bundle_attach(void*) { return ESP_OK; }

static std::atomic<int> g_tls_created{0};
static std::atomic<int> g_tls_destroyed{0};
static std::atomic<int> g_shutdown_calls{0};
static std::atomic<int> g_setsockopt_rcvtimeo_us{0};
static std::function<int(esp_tls_t*, char*, size_t)> g_read_fn;
static std::function<int(esp_tls_t*, const char*, size_t)> g_write_fn;
static std::atomic<int64_t> g_now_us{1000};

int64_t esp_timer_get_time() { return g_now_us.load(); }

esp_tls_t* esp_tls_init() {
    ++g_tls_created;
    return new esp_tls_t();
}

int esp_tls_conn_new_sync(const char*, int, int, const esp_tls_cfg_t*, esp_tls_t*) {
    return 1;
}

esp_err_t esp_tls_get_error_handle(esp_tls_t*, esp_tls_error_handle_t* out) {
    *out = nullptr;
    return ESP_OK;
}

esp_err_t esp_tls_get_and_clear_last_error(esp_tls_error_handle_t, int*, int*) {
    return -1;
}

void esp_tls_conn_destroy(esp_tls_t* tls) {
    assert(tls != nullptr);
    if (tls->destroyed.exchange(true)) {
        g_uaf_detected.store(true);
    }
    ++g_tls_destroyed;
    delete tls;
}

esp_err_t esp_tls_get_conn_sockfd(esp_tls_t* tls, int* sockfd) {
    if (tls->destroyed.load()) {
        g_uaf_detected.store(true);
    }
    *sockfd = tls->sockfd;
    return ESP_OK;
}

int esp_tls_conn_read(esp_tls_t* tls, void* buf, size_t len) {
    if (tls == nullptr || tls->destroyed.load()) {
        g_uaf_detected.store(true);
        return -1;
    }
    int ret = g_read_fn ? g_read_fn(tls, static_cast<char*>(buf), len) : 0;
    if (tls->destroyed.load()) {
        g_uaf_detected.store(true);
    }
    return ret;
}

int esp_tls_conn_write(esp_tls_t* tls, const void* buf, size_t len) {
    if (tls == nullptr || tls->destroyed.load()) {
        g_uaf_detected.store(true);
        return -1;
    }
    return g_write_fn ? g_write_fn(tls, static_cast<const char*>(buf), len) : static_cast<int>(len);
}

#define setsockopt test_setsockopt
#define shutdown test_shutdown
#define close test_close_forbidden

int test_setsockopt(int, int, int optname, const void* optval, socklen_t optlen) {
    if (optname == SO_RCVTIMEO && optlen == sizeof(struct timeval)) {
        const auto* tv = static_cast<const struct timeval*>(optval);
        g_setsockopt_rcvtimeo_us.store(static_cast<int>(tv->tv_sec * 1000000 + tv->tv_usec));
    }
    return 0;
}

int test_shutdown(int, int) {
    ++g_shutdown_calls;
    return 0;
}

int test_close_forbidden(int) {
    // MemoriaSslTransport must NEVER call close(sockfd) directly before esp_tls_conn_destroy!
    g_uaf_detected.store(true);
    assert(false && "Direct close(sockfd) is forbidden in MemoriaSslTransport");
    return -1;
}

class Tcp {
public:
    virtual ~Tcp() = default;
    virtual bool Connect(const std::string& host, int port) = 0;
    virtual void Disconnect() = 0;
    virtual int Send(const std::string& data) = 0;
    virtual void OnStream(std::function<void(const std::string& data)> callback) {
        stream_callback_ = callback;
    }
    virtual void OnDisconnected(std::function<void()> callback) {
        disconnect_callback_ = callback;
    }
    bool connected() const { return connected_; }
    virtual int GetLastError() = 0;
protected:
    std::function<void(const std::string& data)> stream_callback_;
    std::function<void()> disconnect_callback_;
    bool connected_ = false;
};

namespace memoria {
constexpr const char* kTag = "TestMemoriaSsl";
"""
        + transport_cls
        + r"""
} // namespace memoria

int main() {
    using memoria::MemoriaSslTransport;

    // Case 1: Normal Connect, Ping interception, Send, and explicit Disconnect.
    {
        std::vector<std::string> pings;
        std::string forwarded;
        std::atomic<int> disconnects{0};
        std::vector<std::string> chunks = {
            "HTTP/1.1 101 Switching Protocols\r\n\r\n",
            std::string("\x89\x04ping", 6),
        };
        std::atomic<size_t> idx{0};
        g_read_fn = [&](esp_tls_t*, char* dst, size_t) -> int {
            size_t i = idx.fetch_add(1);
            if (i < chunks.size()) {
                std::memcpy(dst, chunks[i].data(), chunks[i].size());
                return static_cast<int>(chunks[i].size());
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
            return ESP_TLS_ERR_SSL_WANT_READ;
        };
        g_write_fn = [&](esp_tls_t*, const char*, size_t len) -> int {
            return static_cast<int>(len);
        };

        MemoriaSslTransport transport([&](const char* data, size_t len) {
            pings.emplace_back(data, len);
        });
        assert(transport.Connect("edge.example.com", 443));
        // Register callbacks after Connect() just like WebSocket::Connect() does.
        transport.OnStream([&](const std::string& s) { forwarded.append(s); });
        transport.OnDisconnected([&]() { ++disconnects; });

        while (pings.empty()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        assert(g_setsockopt_rcvtimeo_us.load() == 200 * 1000);
        assert(pings.size() == 1 && pings[0] == "ping");
        assert(transport.Send("hello") == 5);

        transport.Disconnect();
        JoinAllSpawnedThreads();
        // Explicit Disconnect() must NOT fire disconnect_callback.
        assert(disconnects.load() == 0);
        assert(!g_uaf_detected.load());
        assert(g_tls_created.load() == g_tls_destroyed.load());
        assert(g_event_group_created.load() == g_event_group_deleted.load());
    }

    // Case 2: Disconnect wait timeout + owner destruction while esp_tls_conn_read is blocked.
    // Proves zero UAF on `this`, `tls_client`, and `event_group`, and deferred cleanup on task exit.
    {
        std::promise<void> read_entered;
        std::promise<void> allow_read_return;
        auto allow_read_fut = allow_read_return.get_future().share();

        g_read_fn = [&](esp_tls_t* tls, char*, size_t) -> int {
            read_entered.set_value();
            allow_read_fut.wait();
            if (tls->destroyed.load()) {
                g_uaf_detected.store(true);
            }
            return 0;
        };

        const int tls_before = g_tls_destroyed.load();
        {
            auto transport = std::make_unique<MemoriaSslTransport>([](const char*, size_t) {});
            transport->SetDisconnectWaitTimeoutMs(20);
            assert(transport->Connect("edge.example.com", 443));
            read_entered.get_future().wait();
            // Destroy transport while ssl_receive is blocked inside esp_tls_conn_read!
            transport.reset();
        }
        // While ssl_receive is still blocked in esp_tls_conn_read, tls_client must NOT be destroyed yet!
        assert(g_tls_destroyed.load() == tls_before);
        assert(!g_uaf_detected.load());

        // Now unblock esp_tls_conn_read; ssl_receive exits and SharedState destructor cleans up.
        allow_read_return.set_value();
        JoinAllSpawnedThreads();
        assert(!g_uaf_detected.load());
        assert(g_tls_created.load() == g_tls_destroyed.load());
        assert(g_event_group_created.load() == g_event_group_deleted.load());
    }

    // Case 3: Self-disconnect from inside OnStream callback on ssl_receive does not deadlock.
    {
        const std::string hs = "HTTP/1.1 101 Switching Protocols\r\n\r\n";
        std::atomic<bool> sent_hs{false};
        g_read_fn = [&](esp_tls_t*, char* dst, size_t) -> int {
            if (!sent_hs.exchange(true)) {
                std::memcpy(dst, hs.data(), hs.size());
                return static_cast<int>(hs.size());
            }
            return 0;
        };

        auto transport = std::make_unique<MemoriaSslTransport>([](const char*, size_t) {});
        transport->OnStream([&](const std::string&) {
            transport->Disconnect();
        });
        assert(transport->Connect("edge.example.com", 443));
        JoinAllSpawnedThreads();
        transport.reset();
        assert(!g_uaf_detected.load());
        assert(g_tls_created.load() == g_tls_destroyed.load());
        assert(g_event_group_created.load() == g_event_group_deleted.load());
    }

    // Case 4: xTaskCreate failure cleans up tls_client and event_group immediately.
    {
        g_fail_task_create.store(true);
        MemoriaSslTransport transport([](const char*, size_t) {});
        assert(!transport.Connect("edge.example.com", 443));
        assert(transport.GetLastError() == ESP_ERR_NO_MEM);
        g_fail_task_create.store(false);
        assert(g_tls_created.load() == g_tls_destroyed.load());
        assert(g_event_group_created.load() == g_event_group_deleted.load());
    }

    // Case 5: Concurrent Send() failure and ReceiveLoop() failure invoke OnDisconnected at most once.
    {
        std::promise<void> rx_wait;
        auto rx_wait_fut = rx_wait.get_future().share();
        g_read_fn = [&](esp_tls_t*, char*, size_t) -> int {
            rx_wait_fut.wait();
            return -100;
        };
        g_write_fn = [&](esp_tls_t*, const char*, size_t) -> int {
            return -200;
        };

        std::atomic<int> disconnect_calls{0};
        MemoriaSslTransport transport([](const char*, size_t) {});
        transport.OnDisconnected([&]() { ++disconnect_calls; });
        assert(transport.Connect("edge.example.com", 443));
        assert(transport.Send("fail") == -200);
        assert(transport.GetLastError() == -200);
        rx_wait.set_value();
        JoinAllSpawnedThreads();
        assert(disconnect_calls.load() == 1);
        transport.Disconnect();
        assert(disconnect_calls.load() == 1);
        assert(!g_uaf_detected.load());
        assert(g_tls_created.load() == g_tls_destroyed.load());
        assert(g_event_group_created.load() == g_event_group_deleted.load());
    }

    // Case 6: Malformed WebSocket frame on ReceiveLoop shuts down socket and fires OnDisconnected once.
    {
        std::vector<std::string> bad_stream = {
            "HTTP/1.1 101 Switching Protocols\r\n\r\n",
            std::string("\x09\x00", 2), // fragmented Ping (illegal per RFC 6455)
        };
        std::atomic<size_t> idx{0};
        g_read_fn = [&](esp_tls_t*, char* dst, size_t) -> int {
            size_t i = idx.fetch_add(1);
            if (i < bad_stream.size()) {
                std::memcpy(dst, bad_stream[i].data(), bad_stream[i].size());
                return static_cast<int>(bad_stream[i].size());
            }
            return 0;
        };
        std::atomic<int> disconnect_calls{0};
        const int shutdowns_before = g_shutdown_calls.load();
        MemoriaSslTransport transport([](const char*, size_t) {});
        transport.OnDisconnected([&]() { ++disconnect_calls; });
        assert(transport.Connect("edge.example.com", 443));
        JoinAllSpawnedThreads();
        assert(disconnect_calls.load() == 1);
        assert(transport.GetLastError() == -1);
        assert(g_shutdown_calls.load() > shutdowns_before);
        transport.Disconnect();
        assert(disconnect_calls.load() == 1);
        assert(!g_uaf_detected.load());
        assert(g_tls_created.load() == g_tls_destroyed.load());
        assert(g_event_group_created.load() == g_event_group_deleted.load());
    }

    std::puts("Real MemoriaSslTransport lifecycle & UAF safety checks passed");
    return 0;
}
""",
    )
    print(output.strip())


def test_real_websocket_and_protocol_retirement_lifecycle_across_all_disconnect_paths(
    tmp_path: Path,
) -> None:
    """Compile and exercise the real upstream WebSocket member/destructor semantics together
    with the real MemoriaSslTransport class and MemoriaProtocol::RetireOrDestroyWebSocket /
    ReapRetiredWebSockets / CloseAudioChannel / OpenAudioChannel (kSessionClosedBit) paths.

    Specifically proves zero UAF on WebSocket (`this` and its members such as `receive_buffer_`,
    `current_message_`, `on_data_`, `on_disconnected_`) when:
    1. Positive control: Normal WebSocket session open, frame delivery, and explicit CloseAudioChannel(true);
    2. Peer Close control frame (opcode 0x8): WebSocket::OnTcpData sets WebSocket::connected_ = false
       WHILE MemoriaSslTransport::connected_ is still true, then main_task runs CloseAudioChannel(false).
       Without explicit retiring_ssl->Disconnect() before WebSocket destruction, WebSocket::~WebSocket
       skips tcp_->Disconnect() because WebSocket::connected_ is false, destroying WebSocket members
       before tcp_ while ssl_receive can still invoke OnTcpData/OnDisconnected on raw WebSocket `this`!
    3. Write failure (Send() < 0) followed by CloseAudioChannel(false) while ssl_receive is unwinding;
    4. EOF / read error (esp_tls_conn_read <= 0) firing OnDisconnected (setting WebSocket::connected_ = false)
       concurrently with CloseAudioChannel(false) on main_task;
    5. Post-handshake session rejection (server sends session.error / invalid message after HTTP 101,
       setting kSessionClosedBit during OpenAudioChannel): OpenAudioChannel wakes on kSessionClosedBit,
       immediately stops the SSL transport and defers WebSocket owner destruction if the receive callback
       is still unwinding after OnData;
    6. Disconnect() wait timeout while a stream/disconnect callback is still in flight on ssl_receive:
       CanDestroyCallbackOwner() is false on timeout, so RetireOrDestroyWebSocket moves the WebSocket
       owner into retired_websockets_ instead of destroying it; the in-flight callback continues accessing
       WebSocket members (`receive_buffer_`, `current_message_`) after Disconnect() returns with zero UAF,
       and ReapRetiredWebSockets() deterministically reclaims the retired WebSocket once ssl_receive exits.
    """
    protocol_src = PROTOCOL_SOURCE.read_text(encoding="utf-8")
    transport_cls = class_block(protocol_src, "class MemoriaSslTransport final : public Tcp")
    reap_fn = function(protocol_src, "void MemoriaProtocol::ReapRetiredWebSockets(")
    retire_or_destroy_fn = function(protocol_src, "void MemoriaProtocol::RetireOrDestroyWebSocket(")
    shutdown_fn = function(protocol_src, "bool MemoriaProtocol::PrepareForDestruction(")
    destructor_fn = function(protocol_src, "MemoriaProtocol::~MemoriaProtocol()")
    reset_patch = (ROOT / "overlay/patches/0050-defer-protocol-reset-until-callbacks-exit.patch").read_text()
    added = "\n".join(line[1:] for line in reset_patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
    reset_fn = function(added, "void Application::ContinueProtocolReset()")

    output = run_cpp(
        tmp_path,
        r"""
#include "memoria_ws_tls_guard.h"
#include <atomic>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <functional>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <sys/socket.h>
#include <sys/time.h>
#include <thread>
#include <utility>
#include <vector>

using esp_err_t = int;
constexpr esp_err_t ESP_OK = 0;
constexpr esp_err_t ESP_ERR_NO_MEM = 0x101;
constexpr int ESP_TLS_ERR_SSL_WANT_READ = -0x6900;
constexpr int ESP_TLS_ERR_SSL_WANT_WRITE = -0x6880;
using EventBits_t = uint32_t;
using TickType_t = uint32_t;
using BaseType_t = int;
using UBaseType_t = unsigned int;
using TaskHandle_t = void*;
constexpr EventBits_t BIT0 = 1u << 0;
constexpr EventBits_t BIT1 = 1u << 1;
constexpr BaseType_t pdFALSE = 0;
constexpr BaseType_t pdTRUE = 1;
constexpr BaseType_t pdPASS = 1;
#define pdMS_TO_TICKS(ms) (static_cast<TickType_t>(ms))
#define ESP_LOGE(tag, fmt, ...) ((void)tag)
#define ESP_LOGW(tag, fmt, ...) ((void)tag)
#define ESP_LOGI(tag, fmt, ...) ((void)tag)
#define ESP_LOGD(tag, fmt, ...) ((void)tag)

struct FakeEventGroup {
    std::mutex mu;
    std::condition_variable cv;
    EventBits_t bits = 0;
    bool deleted = false;
};
using EventGroupHandle_t = FakeEventGroup*;

static std::atomic<int> g_event_group_created{0};
static std::atomic<int> g_event_group_deleted{0};
static std::atomic<bool> g_uaf_detected{false};

EventGroupHandle_t xEventGroupCreate() {
    ++g_event_group_created;
    return new FakeEventGroup();
}

void vEventGroupDelete(EventGroupHandle_t eg) {
    assert(eg != nullptr);
    {
        std::lock_guard<std::mutex> lock(eg->mu);
        if (eg->deleted) {
            g_uaf_detected.store(true);
        }
        eg->deleted = true;
    }
    ++g_event_group_deleted;
    delete eg;
}

void xEventGroupClearBits(EventGroupHandle_t eg, EventBits_t bits) {
    std::lock_guard<std::mutex> lock(eg->mu);
    if (eg->deleted) g_uaf_detected.store(true);
    eg->bits &= ~bits;
}

void xEventGroupSetBits(EventGroupHandle_t eg, EventBits_t bits) {
    std::lock_guard<std::mutex> lock(eg->mu);
    if (eg->deleted) g_uaf_detected.store(true);
    eg->bits |= bits;
    eg->cv.notify_all();
}

EventBits_t xEventGroupWaitBits(EventGroupHandle_t eg, EventBits_t bits_to_wait, BaseType_t clear_on_exit, BaseType_t, TickType_t ticks) {
    std::unique_lock<std::mutex> lock(eg->mu);
    if (eg->deleted) g_uaf_detected.store(true);
    eg->cv.wait_for(lock, std::chrono::milliseconds(ticks), [&]() {
        return (eg->bits & bits_to_wait) != 0;
    });
    EventBits_t out = eg->bits;
    if (clear_on_exit && (out & bits_to_wait) != 0) {
        eg->bits &= ~bits_to_wait;
    }
    return out;
}

static thread_local void* g_current_task_token = nullptr;
TaskHandle_t xTaskGetCurrentTaskHandle() {
    if (g_current_task_token == nullptr) {
        static thread_local int main_token = 1;
        g_current_task_token = &main_token;
    }
    return g_current_task_token;
}

static std::mutex g_threads_mu;
static std::vector<std::thread> g_spawned_threads;

struct TaskExitThrow {};
void vTaskDelete(TaskHandle_t) {
    throw TaskExitThrow{};
}

BaseType_t xTaskCreate(void (*fn)(void*), const char*, uint32_t, void* arg, UBaseType_t, TaskHandle_t* out_handle) {
    auto ready = std::make_shared<std::promise<TaskHandle_t>>();
    auto fut = ready->get_future();
    std::thread th([fn, arg, ready]() {
        int task_token = 2;
        g_current_task_token = &task_token;
        ready->set_value(g_current_task_token);
        try {
            fn(arg);
        } catch (const TaskExitThrow&) {
        }
    });
    TaskHandle_t h = fut.get();
    if (out_handle != nullptr) {
        *out_handle = h;
    }
    std::lock_guard<std::mutex> lock(g_threads_mu);
    g_spawned_threads.push_back(std::move(th));
    return pdPASS;
}

static void JoinAllSpawnedThreads() {
    std::vector<std::thread> threads;
    {
        std::lock_guard<std::mutex> lock(g_threads_mu);
        threads.swap(g_spawned_threads);
    }
    for (auto& t : threads) {
        if (t.joinable()) t.join();
    }
}

struct esp_tls {
    int sockfd = 42;
    std::atomic<bool> destroyed{false};
};
using esp_tls_t = esp_tls;
using esp_tls_error_handle_t = void*;

struct esp_tls_cfg_t {
    esp_err_t (*crt_bundle_attach)(void*) = nullptr;
};
esp_err_t esp_crt_bundle_attach(void*) { return ESP_OK; }

static std::atomic<int> g_tls_created{0};
static std::atomic<int> g_tls_destroyed{0};
static std::atomic<int> g_shutdown_calls{0};
static std::function<int(esp_tls_t*, char*, size_t)> g_read_fn;
static std::function<int(esp_tls_t*, const char*, size_t)> g_write_fn;
static std::atomic<int64_t> g_now_us{1000};

int64_t esp_timer_get_time() { return g_now_us.load(); }

esp_tls_t* esp_tls_init() {
    ++g_tls_created;
    return new esp_tls_t();
}

int esp_tls_conn_new_sync(const char*, int, int, const esp_tls_cfg_t*, esp_tls_t*) {
    return 1;
}

esp_err_t esp_tls_get_error_handle(esp_tls_t*, esp_tls_error_handle_t* out) {
    *out = nullptr;
    return ESP_OK;
}

esp_err_t esp_tls_get_and_clear_last_error(esp_tls_error_handle_t, int*, int*) {
    return -1;
}

void esp_tls_conn_destroy(esp_tls_t* tls) {
    assert(tls != nullptr);
    if (tls->destroyed.exchange(true)) {
        g_uaf_detected.store(true);
    }
    ++g_tls_destroyed;
    delete tls;
}

esp_err_t esp_tls_get_conn_sockfd(esp_tls_t* tls, int* sockfd) {
    if (tls->destroyed.load()) {
        g_uaf_detected.store(true);
    }
    *sockfd = tls->sockfd;
    return ESP_OK;
}

int esp_tls_conn_read(esp_tls_t* tls, void* buf, size_t len) {
    if (tls == nullptr || tls->destroyed.load()) {
        g_uaf_detected.store(true);
        return -1;
    }
    int ret = g_read_fn ? g_read_fn(tls, static_cast<char*>(buf), len) : 0;
    if (tls->destroyed.load()) {
        g_uaf_detected.store(true);
    }
    return ret;
}

int esp_tls_conn_write(esp_tls_t* tls, const void* buf, size_t len) {
    if (tls == nullptr || tls->destroyed.load()) {
        g_uaf_detected.store(true);
        return -1;
    }
    return g_write_fn ? g_write_fn(tls, static_cast<const char*>(buf), len) : static_cast<int>(len);
}

#define setsockopt test_setsockopt
#define shutdown test_shutdown
#define close test_close_forbidden

int test_setsockopt(int, int, int, const void*, socklen_t) { return 0; }
int test_shutdown(int, int) {
    ++g_shutdown_calls;
    return 0;
}
int test_close_forbidden(int) {
    g_uaf_detected.store(true);
    assert(false && "Direct close(sockfd) is forbidden");
    return -1;
}

class Tcp {
public:
    virtual ~Tcp() = default;
    virtual bool Connect(const std::string& host, int port) = 0;
    virtual void Disconnect() = 0;
    virtual int Send(const std::string& data) = 0;
    virtual void OnStream(std::function<void(const std::string& data)> callback) {
        stream_callback_ = callback;
    }
    virtual void OnDisconnected(std::function<void()> callback) {
        disconnect_callback_ = callback;
    }
    bool connected() const { return connected_; }
    virtual int GetLastError() = 0;
protected:
    std::function<void(const std::string& data)> stream_callback_;
    std::function<void()> disconnect_callback_;
    bool connected_ = false;
};

static std::atomic<int> g_ws_alive{0};

// Exact reproduction of upstream 78__esp-ml307 WebSocket member declaration order
// and destructor / OnTcpData / OnDisconnected behavior, instrumented with a member
// canary that detects any use-after-destruction of WebSocket members while tcp_ or
// a receive callback is still running.
class WebSocket {
public:
    using SslFactory = std::function<std::unique_ptr<Tcp>()>;

    explicit WebSocket(SslFactory ssl_factory) : ssl_factory_(std::move(ssl_factory)) {
        ++g_ws_alive;
        handshake_event_group_ = xEventGroupCreate();
    }

    ~WebSocket() {
        // Exact upstream WebSocket::~WebSocket logic:
        // Only calls tcp_->Disconnect() when connected_ is true!
        if (connected_) {
            tcp_->Disconnect();
        }
        if (handshake_event_group_) {
            vEventGroupDelete(handshake_event_group_);
            handshake_event_group_ = nullptr;
        }
        // Notice: in C++, members declared AFTER `tcp_` are destroyed BEFORE `tcp_`!
        // `member_canary_` simulates WebSocket's own members (`receive_buffer_`,
        // `current_message_`, `on_data_`, `on_disconnected_`, etc.) being destroyed
        // before `tcp_` is destroyed.
    }

    bool IsConnected() const {
        CheckAlive();
        return connected_;
    }

    bool Connect(const char*) {
        CheckAlive();
        tcp_ = ssl_factory_();
        connected_ = false;
        if (!tcp_->Connect("edge.example.com", 443)) {
            return false;
        }
        xEventGroupClearBits(handshake_event_group_, HANDSHAKE_SUCCESS_BIT | HANDSHAKE_FAILED_BIT);
        tcp_->OnStream([this](const std::string& data) {
            this->OnTcpData(data);
        });
        tcp_->OnDisconnected([this]() {
            this->CheckAlive();
            if (connected_) {
                connected_ = false;
                if (on_disconnected_) {
                    on_disconnected_();
                }
            }
        });
        if (tcp_->Send("GET /ws HTTP/1.1\r\n\r\n") < 0) {
            return false;
        }
        EventBits_t bits = xEventGroupWaitBits(
            handshake_event_group_,
            HANDSHAKE_SUCCESS_BIT | HANDSHAKE_FAILED_BIT,
            pdFALSE, pdFALSE, pdMS_TO_TICKS(2000));
        if (bits & HANDSHAKE_SUCCESS_BIT) {
            connected_ = true;
            return true;
        }
        if (bits & HANDSHAKE_FAILED_BIT) {
            if (on_error_) on_error_(-1);
        }
        return false;
    }

    bool Send(const std::string& data) {
        CheckAlive();
        std::lock_guard<std::mutex> lock(send_mutex_);
        return tcp_->Send(data) >= 0;
    }

    void Close() {
        CheckAlive();
        if (connected_) {
            SendControlFrame(0x8, nullptr, 0);
        }
    }

    void OnDisconnected(std::function<void()> callback) { on_disconnected_ = std::move(callback); }
    void OnData(std::function<void(const char*, size_t, bool)> callback) { on_data_ = std::move(callback); }
    void OnError(std::function<void(int)> callback) { on_error_ = std::move(callback); }
    void OnPong(std::function<void(const char*, size_t)> callback) { on_pong_ = std::move(callback); }
    int GetLastError() {
        CheckAlive();
        return tcp_ ? tcp_->GetLastError() : 0;
    }

private:
    struct MemberCanary {
        std::atomic<bool> alive{true};
        ~MemberCanary() {
            alive.store(false, std::memory_order_release);
            --g_ws_alive;
        }
    };

    void CheckAlive() const {
        if (!member_canary_.alive.load(std::memory_order_acquire)) {
            g_uaf_detected.store(true);
        }
    }

    void OnTcpData(const std::string& data) {
        CheckAlive();
        receive_buffer_.append(data);
        if (!handshake_completed_) {
            size_t pos = receive_buffer_.find("\r\n\r\n");
            if (pos != std::string::npos) {
                std::string hs = receive_buffer_.substr(0, pos + 4);
                receive_buffer_ = receive_buffer_.substr(pos + 4);
                if (hs.find("HTTP/1.1 101") != std::string::npos) {
                    handshake_completed_ = true;
                    xEventGroupSetBits(handshake_event_group_, HANDSHAKE_SUCCESS_BIT);
                } else {
                    xEventGroupSetBits(handshake_event_group_, HANDSHAKE_FAILED_BIT);
                    return;
                }
            } else {
                return;
            }
        }
        size_t buffer_offset = 0;
        const uint8_t* buffer = reinterpret_cast<const uint8_t*>(receive_buffer_.data());
        size_t buffer_size = receive_buffer_.size();
        while (buffer_offset < buffer_size) {
            CheckAlive();
            if (buffer_size - buffer_offset < 2) break;
            uint8_t opcode = buffer[buffer_offset] & 0x0F;
            bool fin = (buffer[buffer_offset] & 0x80) != 0;
            uint64_t payload_length = buffer[buffer_offset + 1] & 0x7F;
            size_t header_length = 2;
            if (buffer_size - buffer_offset < header_length + payload_length) break;
            std::vector<char> payload(payload_length);
            if (payload_length > 0) {
                std::memcpy(payload.data(), buffer + buffer_offset + header_length, payload_length);
            }
            switch (opcode) {
                case 0x1:
                case 0x2:
                    current_message_.assign(payload.begin(), payload.end());
                    if (fin && on_data_) {
                        on_data_(current_message_.data(), current_message_.size(), opcode == 0x2);
                    }
                    // Upstream WebSocket::OnTcpData touches members AFTER on_data_ returns!
                    CheckAlive();
                    current_message_.clear();
                    break;
                case 0x8: // Close control frame: sets connected_ = false while tcp_ is still open!
                    connected_ = false;
                    if (on_disconnected_) {
                        on_disconnected_();
                    }
                    CheckAlive();
                    break;
                case 0xA:
                    if (on_pong_) {
                        on_pong_(payload.data(), payload.size());
                    }
                    CheckAlive();
                    break;
                default:
                    break;
            }
            buffer_offset += header_length + payload_length;
        }
        CheckAlive();
        if (buffer_offset > 0) {
            receive_buffer_ = receive_buffer_.substr(buffer_offset);
        }
    }

    bool SendControlFrame(uint8_t opcode, const void*, size_t len) {
        CheckAlive();
        std::string frame(2, '\0');
        frame[0] = static_cast<char>(0x80 | opcode);
        frame[1] = static_cast<char>(0x80 | len);
        std::lock_guard<std::mutex> lock(send_mutex_);
        return tcp_->Send(frame) >= 0;
    }

    // Notice exact declaration order: tcp_ is declared BEFORE WebSocket's other members,
    // so in ~WebSocket(), member_canary_ and all other fields are destroyed BEFORE tcp_!
    SslFactory ssl_factory_;
    std::unique_ptr<Tcp> tcp_;
    std::string receive_buffer_;
    bool handshake_completed_ = false;
    std::atomic<bool> connected_{false};
    std::mutex send_mutex_;
    EventGroupHandle_t handshake_event_group_ = nullptr;
    static constexpr EventBits_t HANDSHAKE_SUCCESS_BIT = BIT0;
    static constexpr EventBits_t HANDSHAKE_FAILED_BIT = BIT1;
    std::function<void(const char*, size_t, bool)> on_data_;
    std::function<void(int)> on_error_;
    std::function<void()> on_disconnected_;
    std::function<void(const char*, size_t)> on_pong_;
    std::vector<char> current_message_;
    MemberCanary member_canary_;
};

namespace memoria {
constexpr const char* kTag = "TestMemoriaWsLifecycle";
"""
        + transport_cls
        + r"""

struct MemoriaProtocol;
MemoriaProtocol* g_active_protocol = nullptr;
#define configASSERT(value) assert(value)
struct MemoriaProtocol {
    TaskHandle_t activation_retry_task_ = nullptr;
    TaskHandle_t display_profile_task_ = nullptr;
    std::function<void()> on_local_flush_requested_, on_audio_channel_closed_,
                          on_disconnected_, on_transport_action_ready_;
    static constexpr EventBits_t kSessionReadyBit = BIT0;
    static constexpr EventBits_t kSessionClosedBit = BIT1;

    std::recursive_mutex playback_state_mutex_;
    std::atomic<bool> shutting_down_{false};
    std::atomic<uint32_t> websocket_attempt_id_{0};
    bool transport_close_notified_ = true;
    bool error_occurred_ = false;
    uint32_t stream_epoch_ = 0;
    EventGroupHandle_t event_group_ = nullptr;
    std::unique_ptr<WebSocket> websocket_;
    MemoriaSslTransport* media_ssl_transport_ = nullptr;
    std::deque<std::pair<std::unique_ptr<WebSocket>, MemoriaSslTransport*>> retired_websockets_;
    uint32_t disconnect_wait_timeout_ms_ = 10000;
    std::function<void(const char*, size_t, bool)> custom_on_data_hook_;
    std::function<void()> custom_on_disconnected_hook_;
    int closed_notifications_ = 0;

    MemoriaProtocol() {
        event_group_ = xEventGroupCreate();
    }
    ~MemoriaProtocol();

    bool PrepareForDestruction();
    void ReapRetiredWebSockets();
    void RetireOrDestroyWebSocket(std::unique_ptr<WebSocket> retiring_websocket,
                                  MemoriaSslTransport* retiring_ssl,
                                  bool send_ws_close);

    bool RetireTransportAttempt(uint32_t websocket_attempt) {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        if (websocket_attempt != websocket_attempt_id_.load()) {
            return false;
        }
        error_occurred_ = true;
        ++websocket_attempt_id_;
        xEventGroupSetBits(event_group_, kSessionClosedBit);
        if (!transport_close_notified_) {
            transport_close_notified_ = true;
            ++closed_notifications_;
        }
        return true;
    }

    void ResetSessionState() {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        stream_epoch_ = 0;
    }

    bool OpenAudioChannelSimulated() {
        CloseAudioChannel(false);
        ResetSessionState();
        uint32_t websocket_attempt = 0;
        {
            std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
            stream_epoch_ = 1;
            error_occurred_ = false;
            websocket_attempt = ++websocket_attempt_id_;
            transport_close_notified_ = false;
        }
        xEventGroupClearBits(event_group_, kSessionReadyBit | kSessionClosedBit);
        media_ssl_transport_ = nullptr;
        websocket_ = std::make_unique<WebSocket>([this]() -> std::unique_ptr<Tcp> {
            auto ssl = std::make_unique<MemoriaSslTransport>([](const char*, size_t) {});
            ssl->SetDisconnectWaitTimeoutMs(disconnect_wait_timeout_ms_);
            media_ssl_transport_ = ssl.get();
            return ssl;
        });
        WebSocket* const websocket = websocket_.get();
        websocket->OnData([this, websocket_attempt](const char* data, size_t size, bool binary) {
            {
                std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
                if (websocket_attempt != websocket_attempt_id_.load()) {
                    return;
                }
                const std::string msg(data, size);
                if (msg == "session.accepted") {
                    xEventGroupSetBits(event_group_, kSessionReadyBit);
                } else if (msg == "session.error") {
                    error_occurred_ = true;
                    RetireTransportAttempt(websocket_attempt);
                }
            }
            if (custom_on_data_hook_) {
                custom_on_data_hook_(data, size, binary);
            }
        });
        websocket->OnDisconnected([this, websocket, websocket_attempt]() {
            if (custom_on_disconnected_hook_) {
                custom_on_disconnected_hook_();
            }
            (void)websocket->GetLastError();
            RetireTransportAttempt(websocket_attempt);
        });
        websocket->OnError([this, websocket_attempt](int) {
            std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
            if (websocket_attempt != websocket_attempt_id_.load()) return;
            error_occurred_ = true;
            RetireTransportAttempt(websocket_attempt);
        });
        if (!websocket->Connect("wss://edge.example.com/ws")) {
            std::unique_ptr<WebSocket> failed_websocket;
            MemoriaSslTransport* failed_ssl = nullptr;
            {
                std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
                failed_ssl = media_ssl_transport_;
                media_ssl_transport_ = nullptr;
                failed_websocket = std::move(websocket_);
            }
            RetireOrDestroyWebSocket(std::move(failed_websocket), failed_ssl, false);
            return false;
        }
        if (!websocket->Send("{\"type\":\"device.hello\"}")) {
            CloseAudioChannel(false);
            return false;
        }
        const EventBits_t bits = xEventGroupWaitBits(
            event_group_, kSessionReadyBit | kSessionClosedBit,
            pdTRUE, pdFALSE, pdMS_TO_TICKS(1000));
        if ((bits & kSessionClosedBit) != 0) {
            std::unique_ptr<WebSocket> closed_websocket;
            MemoriaSslTransport* closed_ssl = nullptr;
            {
                std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
                closed_ssl = media_ssl_transport_;
                media_ssl_transport_ = nullptr;
                if (closed_ssl != nullptr) {
                    closed_websocket = std::move(websocket_);
                }
                ResetSessionState();
            }
            if (closed_ssl != nullptr) {
                RetireOrDestroyWebSocket(std::move(closed_websocket), closed_ssl, false);
            }
            return false;
        }
        if ((bits & kSessionReadyBit) == 0) {
            CloseAudioChannel(false);
            return false;
        }
        return true;
    }

    void CloseAudioChannel(bool send_goodbye) {
        std::unique_ptr<WebSocket> retiring_websocket;
        MemoriaSslTransport* retiring_ssl = nullptr;
        bool notify_closed = false;
        {
            std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
            const bool had_channel_state = websocket_ != nullptr || stream_epoch_ != 0;
            if (!transport_close_notified_) {
                transport_close_notified_ = true;
                notify_closed = had_channel_state;
                ++websocket_attempt_id_;
            }
            retiring_ssl = media_ssl_transport_;
            media_ssl_transport_ = nullptr;
            retiring_websocket = std::move(websocket_);
            ResetSessionState();
        }
        RetireOrDestroyWebSocket(std::move(retiring_websocket), retiring_ssl, send_goodbye);
        if (notify_closed) {
            ++closed_notifications_;
        }
    }
};

"""
        + reap_fn
        + "\n"
        + retire_or_destroy_fn
        + "\n" + shutdown_fn
        + "\n" + destructor_fn
        + r"""
} // namespace memoria

struct Application {
    std::unique_ptr<memoria::MemoriaProtocol> protocol_;
    bool protocol_reset_pending_ = true;
    void ContinueProtocolReset();
};
""" + reset_fn + r"""

static std::string MakeWsFrame(uint8_t opcode, const std::string& payload) {
    std::string out;
    out.push_back(static_cast<char>(0x80 | (opcode & 0x0F)));
    out.push_back(static_cast<char>(payload.size()));
    out.append(payload);
    return out;
}

int main() {
    using memoria::MemoriaProtocol;
    const std::string kHandshake101 = "HTTP/1.1 101 Switching Protocols\r\n\r\n";
    std::atomic<int> client_writes{0};

    // 1. Positive control: Normal open (HTTP 101 + session.accepted) and explicit CloseAudioChannel(true).
    {
        client_writes.store(0);
        MemoriaProtocol proto;
        std::atomic<int> step{0};
        g_read_fn = [&](esp_tls_t*, char* dst, size_t) -> int {
            int w = client_writes.load(std::memory_order_acquire);
            int s = step.load(std::memory_order_relaxed);
            if (s == 0 && w >= 1) {
                step.store(1);
                std::memcpy(dst, kHandshake101.data(), kHandshake101.size());
                return static_cast<int>(kHandshake101.size());
            }
            if (s == 1 && w >= 2) {
                step.store(2);
                std::string f = MakeWsFrame(0x1, "session.accepted");
                std::memcpy(dst, f.data(), f.size());
                return static_cast<int>(f.size());
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            return ESP_TLS_ERR_SSL_WANT_READ;
        };
        g_write_fn = [&](esp_tls_t*, const char*, size_t len) -> int {
            client_writes.fetch_add(1, std::memory_order_release);
            return static_cast<int>(len);
        };

        assert(proto.OpenAudioChannelSimulated());
        assert(g_ws_alive.load() == 1);
        proto.CloseAudioChannel(true);
        JoinAllSpawnedThreads();
        assert(g_ws_alive.load() == 0);
        assert(proto.retired_websockets_.empty());
        assert(!g_uaf_detected.load());
    }

    // 2. Peer Close control frame (opcode 0x8):
    //    WebSocket::OnTcpData sets WebSocket::connected_ = false while MemoriaSslTransport::connected_
    //    is still true! Then CloseAudioChannel(false) runs while ssl_receive continues delivering
    //    trailing bytes or unwinding OnTcpData. Proves retiring_ssl->Disconnect() stops ssl_receive
    //    before WebSocket is destroyed, preventing UAF on WebSocket `this`.
    {
        client_writes.store(0);
        MemoriaProtocol proto;
        std::atomic<bool> allow_close_frame{false};
        std::atomic<int> step{0};
        g_read_fn = [&](esp_tls_t*, char* dst, size_t) -> int {
            int w = client_writes.load(std::memory_order_acquire);
            int s = step.load(std::memory_order_relaxed);
            if (s == 0 && w >= 1) {
                step.store(1);
                std::memcpy(dst, kHandshake101.data(), kHandshake101.size());
                return static_cast<int>(kHandshake101.size());
            }
            if (s == 1 && w >= 2) {
                step.store(2);
                std::string f = MakeWsFrame(0x1, "session.accepted");
                std::memcpy(dst, f.data(), f.size());
                return static_cast<int>(f.size());
            }
            if (s == 2) {
                if (!allow_close_frame.load(std::memory_order_acquire)) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                    return ESP_TLS_ERR_SSL_WANT_READ;
                }
                step.store(3);
                std::string f = MakeWsFrame(0x8, "");
                std::memcpy(dst, f.data(), f.size());
                return static_cast<int>(f.size());
            }
            // Keep sending trailing text frames until Disconnect() stops ssl_receive!
            // If CloseAudioChannel didn't call retiring_ssl->Disconnect() when
            // WebSocket::connected_ == false, ssl_receive would call OnTcpData on freed WebSocket!
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            std::string extra = MakeWsFrame(0x1, "trailing");
            std::memcpy(dst, extra.data(), extra.size());
            return static_cast<int>(extra.size());
        };
        g_write_fn = [&](esp_tls_t*, const char*, size_t len) -> int {
            client_writes.fetch_add(1, std::memory_order_release);
            return static_cast<int>(len);
        };

        assert(proto.OpenAudioChannelSimulated());
        allow_close_frame.store(true, std::memory_order_release);
        // Wait for OnTcpData(0x8) to mark WebSocket::connected_ = false while ssl_receive continues!
        while (proto.websocket_->IsConnected()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        proto.CloseAudioChannel(false);
        JoinAllSpawnedThreads();
        assert(g_ws_alive.load() == 0);
        assert(!g_uaf_detected.load());
    }

    // 3. Write failure (Send() < 0) during hello after HTTP 101:
    //    Send() fails and calls NotifyDisconnectOnce (setting WebSocket::connected_ = false),
    //    then OpenAudioChannel calls CloseAudioChannel(false).
    {
        client_writes.store(0);
        MemoriaProtocol proto;
        std::atomic<int> step{0};
        g_read_fn = [&](esp_tls_t*, char* dst, size_t) -> int {
            if (step.load(std::memory_order_acquire) == 0 &&
                client_writes.load(std::memory_order_acquire) >= 1) {
                step.store(1, std::memory_order_release);
                std::memcpy(dst, kHandshake101.data(), kHandshake101.size());
                return static_cast<int>(kHandshake101.size());
            }
            if (client_writes.load(std::memory_order_acquire) >= 2) {
                return 0; // socket shut down after write failure
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            return ESP_TLS_ERR_SSL_WANT_READ;
        };
        g_write_fn = [&](esp_tls_t*, const char*, size_t len) -> int {
            if (client_writes.fetch_add(1, std::memory_order_acq_rel) == 0) {
                return static_cast<int>(len); // HTTP GET upgrade succeeds
            }
            return -1; // device.hello write fails!
        };

        assert(!proto.OpenAudioChannelSimulated());
        JoinAllSpawnedThreads();
        assert(g_ws_alive.load() == 0);
        assert(!g_uaf_detected.load());
    }

    // 4. EOF / read error (esp_tls_conn_read <= 0) firing OnDisconnected concurrently with CloseAudioChannel
    //    AND remaining in-flight past Disconnect() timeout before touching websocket->GetLastError():
    {
        client_writes.store(0);
        MemoriaProtocol proto;
        proto.disconnect_wait_timeout_ms_ = 10;
        std::promise<void> in_disconnect_cb;
        std::promise<void> release_disconnect_cb;
        auto release_disconnect_fut = release_disconnect_cb.get_future().share();
        std::atomic<bool> allow_eof{false};
        std::atomic<int> step{0};
        g_read_fn = [&](esp_tls_t*, char* dst, size_t) -> int {
            int w = client_writes.load(std::memory_order_acquire);
            int s = step.load(std::memory_order_relaxed);
            if (s == 0 && w >= 1) {
                step.store(1);
                std::memcpy(dst, kHandshake101.data(), kHandshake101.size());
                return static_cast<int>(kHandshake101.size());
            }
            if (s == 1 && w >= 2) {
                step.store(2);
                std::string f = MakeWsFrame(0x1, "session.accepted");
                std::memcpy(dst, f.data(), f.size());
                return static_cast<int>(f.size());
            }
            if (!allow_eof.load(std::memory_order_acquire)) {
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
                return ESP_TLS_ERR_SSL_WANT_READ;
            }
            step.store(3);
            return 0; // EOF -> NotifyDisconnectOnce -> WebSocket::OnDisconnected
        };
        g_write_fn = [&](esp_tls_t*, const char*, size_t len) -> int {
            client_writes.fetch_add(1, std::memory_order_release);
            return static_cast<int>(len);
        };

        proto.custom_on_disconnected_hook_ = [&]() {
            in_disconnect_cb.set_value();
            release_disconnect_fut.wait();
        };

        assert(proto.OpenAudioChannelSimulated());
        allow_eof.store(true, std::memory_order_release);
        in_disconnect_cb.get_future().wait();

        // CloseAudioChannel(false) runs while OnDisconnected is still in flight on ssl_receive
        // (and WebSocket::connected_ is ALREADY false!). Disconnect() times out after 10ms,
        // and WebSocket is held in retired_websockets_ until OnDisconnected finishes calling
        // websocket->GetLastError()!
        proto.CloseAudioChannel(false);
        assert(proto.websocket_ == nullptr);
        assert(proto.retired_websockets_.size() == 1);
        assert(g_ws_alive.load() == 1);
        assert(!g_uaf_detected.load());

        release_disconnect_cb.set_value();
        JoinAllSpawnedThreads();
        assert(!g_uaf_detected.load());

        proto.ReapRetiredWebSockets();
        assert(proto.retired_websockets_.empty());
        assert(g_ws_alive.load() == 0);
        assert(!g_uaf_detected.load());
    }

    // 5. Post-handshake session rejection (kSessionClosedBit during OpenAudioChannel)
    //    AND Disconnect() timeout while OnData callback is still unwinding on ssl_receive!
    //    When server sends session.error, OnData sets kSessionClosedBit which wakes OpenAudioChannel
    //    on main_task BEFORE WebSocket::OnTcpData finishes touching `current_message_.clear()` and
    //    `receive_buffer_.substr(...)`. Even if Disconnect() times out (e.g. 10ms) while ssl_receive
    //    is still unwinding inside OnTcpData, RetireOrDestroyWebSocket keeps WebSocket alive in
    //    retired_websockets_ until CanDestroyCallbackOwner() is true!
    {
        client_writes.store(0);
        MemoriaProtocol proto;
        proto.disconnect_wait_timeout_ms_ = 10; // short timeout to force timeout while callback is in flight
        std::promise<void> allow_callback_unwind;
        auto allow_unwind_fut = allow_callback_unwind.get_future().share();
        std::atomic<int> step{0};
        g_read_fn = [&](esp_tls_t*, char* dst, size_t) -> int {
            int w = client_writes.load(std::memory_order_acquire);
            int s = step.load(std::memory_order_relaxed);
            if (s == 0 && w >= 1) {
                step.store(1);
                std::memcpy(dst, kHandshake101.data(), kHandshake101.size());
                return static_cast<int>(kHandshake101.size());
            }
            if (s == 1 && w >= 2) {
                step.store(2);
                std::string f = MakeWsFrame(0x1, "session.error");
                std::memcpy(dst, f.data(), f.size());
                return static_cast<int>(f.size());
            }
            if (s >= 2) {
                return 0;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            return ESP_TLS_ERR_SSL_WANT_READ;
        };
        g_write_fn = [&](esp_tls_t*, const char*, size_t len) -> int {
            client_writes.fetch_add(1, std::memory_order_release);
            return static_cast<int>(len);
        };

        // Hold ssl_receive inside OnData("session.error") AFTER RetireTransportAttempt sets
        // kSessionClosedBit and wakes OpenAudioChannel on main_task!
        std::promise<void> error_callback_entered;
        proto.custom_on_data_hook_ = [&](const char* data, size_t size, bool) {
            std::string msg(data, size);
            if (msg == "session.error") {
                error_callback_entered.set_value();
                allow_unwind_fut.wait();
            }
        };

        assert(!proto.OpenAudioChannelSimulated());
        error_callback_entered.get_future().wait();

        // At this point:
        // - OpenAudioChannelSimulated() took the kSessionClosedBit branch and returned false;
        // - Disconnect() timed out after 10ms because ssl_receive is still inside OnTcpData("post-error-tail");
        // - Because CanDestroyCallbackOwner() is false, WebSocket MUST still be alive in retired_websockets_!
        assert(proto.websocket_ == nullptr);
        assert(proto.retired_websockets_.size() == 1);
        assert(g_ws_alive.load() == 1);
        assert(!g_uaf_detected.load());

        // Attempting to reap while callback is still in flight keeps it alive.
        proto.ReapRetiredWebSockets();
        assert(proto.retired_websockets_.size() == 1);
        assert(g_ws_alive.load() == 1);

        // Now let OnTcpData finish unwinding and ssl_receive exit.
        allow_callback_unwind.set_value();
        JoinAllSpawnedThreads();
        assert(!g_uaf_detected.load());

        // Now CanDestroyCallbackOwner() is true, and ReapRetiredWebSockets() destroys the retired WebSocket cleanly!
        proto.ReapRetiredWebSockets();
        assert(proto.retired_websockets_.empty());
        assert(g_ws_alive.load() == 0);
        assert(!g_uaf_detected.load());
        assert(g_tls_created.load() == g_tls_destroyed.load());
    }
    assert(g_event_group_created.load() == g_event_group_deleted.load());

    // 6. Explicit CloseAudioChannel on main_task while an OnData callback is in flight on ssl_receive
    //    past Disconnect()'s wait timeout: proves WebSocket is retained in retired_websockets_ until
    //    the in-flight callback and ssl_receive finish, then reaped on next Close/Open/Reap.
    {
        client_writes.store(0);
        MemoriaProtocol proto;
        proto.disconnect_wait_timeout_ms_ = 10;
        std::atomic<bool> allow_audio_frame{false};
        std::promise<void> in_callback;
        std::promise<void> release_callback;
        auto release_fut = release_callback.get_future().share();
        std::atomic<int> step{0};
        g_read_fn = [&](esp_tls_t*, char* dst, size_t) -> int {
            int w = client_writes.load(std::memory_order_acquire);
            int s = step.load(std::memory_order_relaxed);
            if (s == 0 && w >= 1) {
                step.store(1);
                std::memcpy(dst, kHandshake101.data(), kHandshake101.size());
                return static_cast<int>(kHandshake101.size());
            }
            if (s == 1 && w >= 2) {
                step.store(2);
                std::string f = MakeWsFrame(0x1, "session.accepted");
                std::memcpy(dst, f.data(), f.size());
                return static_cast<int>(f.size());
            }
            if (s == 2 && allow_audio_frame.load(std::memory_order_acquire)) {
                step.store(3);
                std::string f = MakeWsFrame(0x2, "inflight-audio");
                std::memcpy(dst, f.data(), f.size());
                return static_cast<int>(f.size());
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            return ESP_TLS_ERR_SSL_WANT_READ;
        };
        g_write_fn = [&](esp_tls_t*, const char*, size_t len) -> int {
            client_writes.fetch_add(1, std::memory_order_release);
            return static_cast<int>(len);
        };

        proto.custom_on_data_hook_ = [&](const char* data, size_t size, bool binary) {
            if (binary && std::string(data, size) == "inflight-audio") {
                in_callback.set_value();
                release_fut.wait();
            }
        };

        assert(proto.OpenAudioChannelSimulated());
        allow_audio_frame.store(true, std::memory_order_release);
        in_callback.get_future().wait();

        // Call CloseAudioChannel(false) on main_task while ssl_receive is blocked inside OnTcpData -> OnData!
        proto.CloseAudioChannel(false);
        // Disconnect() timed out after 10ms, so WebSocket must be held in retired_websockets_, NOT destroyed!
        assert(proto.websocket_ == nullptr);
        assert(proto.retired_websockets_.size() == 1);
        assert(g_ws_alive.load() == 1);
        assert(!g_uaf_detected.load());

        // Release the in-flight callback; OnTcpData touches current_message_ and receive_buffer_ after OnData returns!
        release_callback.set_value();
        JoinAllSpawnedThreads();
        assert(!g_uaf_detected.load());

        // ReapRetiredWebSockets collects it cleanly.
        proto.ReapRetiredWebSockets();
        assert(proto.retired_websockets_.empty());
        assert(g_ws_alive.load() == 0);
        assert(!g_uaf_detected.load());
        assert(g_tls_created.load() == g_tls_destroyed.load());
    }
    assert(g_event_group_created.load() == g_event_group_deleted.load());

    // 8. Whole protocol owner reset on main_task while an OnData callback is in flight on ssl_receive
    //    past Disconnect()'s wait timeout: proves WebSocket is retained in retired_websockets_ until
    //    the in-flight callback and ssl_receive finish, then reaped on next Close/Open/Reap.
    for (bool hold_state_lock : {false, true}) {
        client_writes.store(0);
        Application app;
        app.protocol_ = std::make_unique<MemoriaProtocol>();
        auto& proto = *app.protocol_;
        proto.disconnect_wait_timeout_ms_ = 10;
        std::atomic<bool> allow_audio_frame{false};
        std::promise<void> in_callback;
        std::promise<void> release_callback;
        auto release_fut = release_callback.get_future().share();
        std::atomic<int> step{0};
        g_read_fn = [&](esp_tls_t*, char* dst, size_t) -> int {
            int w = client_writes.load(std::memory_order_acquire);
            int s = step.load(std::memory_order_relaxed);
            if (s == 0 && w >= 1) {
                step.store(1);
                std::memcpy(dst, kHandshake101.data(), kHandshake101.size());
                return static_cast<int>(kHandshake101.size());
            }
            if (s == 1 && w >= 2) {
                step.store(2);
                std::string f = MakeWsFrame(0x1, "session.accepted");
                std::memcpy(dst, f.data(), f.size());
                return static_cast<int>(f.size());
            }
            if (s == 2 && allow_audio_frame.load(std::memory_order_acquire)) {
                step.store(3);
                std::string f = MakeWsFrame(0x2, "inflight-audio");
                std::memcpy(dst, f.data(), f.size());
                return static_cast<int>(f.size());
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            return ESP_TLS_ERR_SSL_WANT_READ;
        };
        g_write_fn = [&](esp_tls_t*, const char*, size_t len) -> int {
            client_writes.fetch_add(1, std::memory_order_release);
            return static_cast<int>(len);
        };

        proto.custom_on_data_hook_ = [&](const char* data, size_t size, bool binary) {
            if (binary && std::string(data, size) == "inflight-audio") {
                std::unique_lock<std::recursive_mutex> lock(proto.playback_state_mutex_, std::defer_lock);
                if (hold_state_lock) { lock.lock(); }
                in_callback.set_value();
                release_fut.wait();
                assert(app.protocol_.get() == &proto);
                xEventGroupSetBits(proto.event_group_, MemoriaProtocol::kSessionReadyBit);
            }
        };

        assert(proto.OpenAudioChannelSimulated());
        allow_audio_frame.store(true, std::memory_order_release);
        in_callback.get_future().wait();

        // The actual application reset must retain Protocol as well as WebSocket
        // beyond the old 500ms destruction grace period, and keep main runnable.
        auto before = std::chrono::steady_clock::now();
        app.ContinueProtocolReset();
        assert(std::chrono::steady_clock::now() - before < std::chrono::milliseconds(100));
        assert(app.protocol_ != nullptr && app.protocol_reset_pending_);
        std::this_thread::sleep_for(std::chrono::milliseconds(550));
        app.ContinueProtocolReset();
        assert(app.protocol_ != nullptr && app.protocol_reset_pending_);
        assert(g_ws_alive.load() == 1);
        assert(!g_uaf_detected.load());

        // Release the in-flight callback; OnTcpData touches current_message_ and receive_buffer_ after OnData returns!
        release_callback.set_value();
        for (int retry = 0; app.protocol_ != nullptr && retry < 1000; ++retry) {
            app.ContinueProtocolReset();
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        JoinAllSpawnedThreads();
        assert(!g_uaf_detected.load());
        assert(app.protocol_ == nullptr && !app.protocol_reset_pending_);
        assert(g_ws_alive.load() == 0);
        assert(!g_uaf_detected.load());
        assert(g_tls_created.load() == g_tls_destroyed.load());
    }
    assert(g_event_group_created.load() == g_event_group_deleted.load());

    // 7. Receive callback self-disconnect: OnData running on ssl_receive calls CloseAudioChannel(false)
    //    directly while WebSocket::OnTcpData is still on the call stack (and will touch
    //    current_message_.clear() and receive_buffer_.substr(pos) after OnData returns).
    //    Proves Disconnect() does not self-deadlock AND RetireOrDestroyWebSocket defers WebSocket
    //    destruction until OnTcpData and ssl_receive finish unwinding.
    {
        client_writes.store(0);
        MemoriaProtocol proto;
        std::atomic<bool> allow_self_close_frame{false};
        std::promise<void> self_close_returned;
        std::atomic<int> step{0};
        g_read_fn = [&](esp_tls_t*, char* dst, size_t) -> int {
            int w = client_writes.load(std::memory_order_acquire);
            int s = step.load(std::memory_order_relaxed);
            if (s == 0 && w >= 1) {
                step.store(1);
                std::memcpy(dst, kHandshake101.data(), kHandshake101.size());
                return static_cast<int>(kHandshake101.size());
            }
            if (s == 1 && w >= 2) {
                step.store(2);
                std::string f = MakeWsFrame(0x1, "session.accepted");
                std::memcpy(dst, f.data(), f.size());
                return static_cast<int>(f.size());
            }
            if (s == 2 && allow_self_close_frame.load(std::memory_order_acquire)) {
                step.store(3);
                std::string f = MakeWsFrame(0x1, "self-close-from-receive-callback");
                std::memcpy(dst, f.data(), f.size());
                return static_cast<int>(f.size());
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            return ESP_TLS_ERR_SSL_WANT_READ;
        };
        g_write_fn = [&](esp_tls_t*, const char*, size_t len) -> int {
            client_writes.fetch_add(1, std::memory_order_release);
            return static_cast<int>(len);
        };

        proto.custom_on_data_hook_ = [&](const char* data, size_t size, bool) {
            if (std::string(data, size) == "self-close-from-receive-callback") {
                proto.CloseAudioChannel(false);
                // Inside the callback on ssl_receive, WebSocket MUST still be alive in retired_websockets_!
                assert(proto.websocket_ == nullptr);
                assert(proto.retired_websockets_.size() == 1);
                assert(g_ws_alive.load() == 1);
                self_close_returned.set_value();
            }
        };

        assert(proto.OpenAudioChannelSimulated());
        allow_self_close_frame.store(true, std::memory_order_release);
        self_close_returned.get_future().wait();
        JoinAllSpawnedThreads();
        assert(!g_uaf_detected.load());

        // Now that ssl_receive has exited, ReapRetiredWebSockets destroys the retired WebSocket cleanly.
        proto.ReapRetiredWebSockets();
        assert(proto.retired_websockets_.empty());
        assert(g_ws_alive.load() == 0);
        assert(!g_uaf_detected.load());
        assert(g_tls_created.load() == g_tls_destroyed.load());
    }
    assert(g_event_group_created.load() == g_event_group_deleted.load());

    std::puts("Real WebSocket + MemoriaSslTransport + MemoriaProtocol retirement lifecycle checks passed");
    return 0;
}
""",
    )
    print(output.strip())


def test_concurrent_send_stall_does_not_block_ssl_receive_on_ping(tmp_path: Path) -> None:
    """Prove the exact generation=25 speaking-stall scenario is resolved.

    While main_task is blocked inside a stalled TLS write (SO_SNDTIMEO -> WANT_WRITE),
    a server Ping (0x89) followed by downlink audio frames and generation.completed
    arrives on ssl_receive.
    With WebSocketPingInterceptor + WriteBoundedTls:
    1. ssl_receive does NOT enter SendControlFrame(0xA) or contend on WebSocket::send_mutex_;
    2. ssl_receive immediately processes the Ping (queuing Pong for main_task and marking
       liveness via OnPong) and continues draining all downlink audio frames + generation.completed;
    3. main_task's WriteBoundedTls fails closed (-1) at the timeout instead of spinning forever.
    """
    protocol_src = PROTOCOL_SOURCE.read_text(encoding="utf-8")
    drain_body = function(protocol_src, "void MemoriaProtocol::DrainTransportActions(")
    queue_pong_body = function(protocol_src, "bool MemoriaProtocol::QueueTransportPong(")

    output = run_cpp(
        tmp_path,
        r"""
#include "memoria_ws_tls_guard.h"
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <functional>
#include <future>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using TaskHandle_t = void*;
static thread_local int g_task_id = 0;
TaskHandle_t xTaskGetCurrentTaskHandle() { return reinterpret_cast<TaskHandle_t>(&g_task_id); }
#define configASSERT(x) assert(x)
uint32_t esp_random() { return 0x13572468u; }

namespace memoria {

enum class TransportActionKind {
    kSendText,
    kSendPong,
    kRetire,
};

struct TransportAction {
    TransportActionKind kind = TransportActionKind::kSendText;
    uint32_t websocket_attempt = 0;
    std::string text;
};

struct WebSocket {
    std::mutex send_mutex_;
    bool connected_ = true;
    int control_frames_on_rx_ = 0;
    int pong_callbacks_ = 0;
    int data_frames_ = 0;

    bool IsConnected() const { return connected_; }
    bool Send(const std::string&) {
        std::lock_guard<std::mutex> lock(send_mutex_);
        return connected_;
    }

    // Simulates WebSocket::OnTcpData after WebSocketPingInterceptor.
    void FeedInterceptedFrame(uint8_t opcode, const std::string&) {
        if (opcode == 0x9) {
            // Unintercepted Ping would lock send_mutex_ on ssl_receive!
            std::lock_guard<std::mutex> lock(send_mutex_);
            ++control_frames_on_rx_;
        } else if (opcode == 0xA) {
            ++pong_callbacks_;
        } else {
            ++data_frames_;
        }
    }
};

struct MemoriaSslTransport {
    std::vector<std::string> raw_frames;
    bool allow_send = true;
    bool SendRawFrame(const std::string& frame) {
        if (!allow_send) return false;
        raw_frames.push_back(frame);
        return true;
    }
};

struct MemoriaProtocol {
    std::recursive_mutex playback_state_mutex_;
    std::atomic<bool> shutting_down_{false};
    std::atomic<uint32_t> websocket_attempt_id_{1};
    uint32_t stream_epoch_ = 1;
    bool error_occurred_ = false;
    TaskHandle_t transport_owner_task_ = nullptr;
    std::unique_ptr<WebSocket> websocket_ = std::make_unique<WebSocket>();
    MemoriaSslTransport ssl_;
    MemoriaSslTransport* media_ssl_transport_ = &ssl_;
    std::deque<TransportAction> transport_actions_;
    int retired_attempt_ = 0;
    int wakes_ = 0;

    bool QueueTransportAction(TransportActionKind kind, std::string text = {}) {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        transport_actions_.push_back(TransportAction{kind, websocket_attempt_id_.load(), std::move(text)});
        ++wakes_;
        return true;
    }

    void RetireTransportAttempt(uint32_t attempt) {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        if (attempt == websocket_attempt_id_.load()) {
            error_occurred_ = true;
            ++websocket_attempt_id_;
            retired_attempt_ = static_cast<int>(attempt);
        }
    }

    bool QueueTransportPong(uint32_t websocket_attempt, std::string payload);
    void DrainTransportActions();
};

"""
        + queue_pong_body
        + "\n"
        + drain_body
        + r"""
} // namespace memoria

int main() {
    using namespace memoria;
    MemoriaProtocol proto;

    std::promise<void> main_holding_send_mutex;
    std::promise<void> rx_finished_draining;

    // 1. main_task holds WebSocket::send_mutex_ while blocked in WriteBoundedTls.
    auto main_future = std::async(std::launch::async, [&]() {
        std::lock_guard<std::mutex> send_lock(proto.websocket_->send_mutex_);
        main_holding_send_mutex.set_value();
        // Wait until ssl_receive has processed Ping + downlink audio + generation.completed
        // while main_task is still holding send_mutex_!
        auto status = rx_finished_draining.get_future().wait_for(std::chrono::seconds(2));
        assert(status == std::future_status::ready);

        int64_t now_us = 0;
        int written = WriteBoundedTls(
            "uplink-audio", 12,
            [&](const char*, size_t) -> int {
                now_us += 10LL * 1000LL * 1000LL;
                return kTlsErrWantWrite;
            },
            [&]() { return now_us; });
        return written;
    });

    main_holding_send_mutex.get_future().wait();

    // 2. On ssl_receive, a server Ping + 2 downlink frames arrive while send_mutex_ is held by main_task.
    WebSocketPingInterceptor interceptor;
    std::string forwarded;
    const std::string handshake = "HTTP/1.1 101 Switching Protocols\r\n\r\n";
    const std::string rx_wire =
        handshake +
        std::string("\x89\x04ping", 6) +
        std::string("\x82\x05" "audio", 7) +
        std::string("\x81\x04" "json", 6);
    assert(interceptor.OnStreamChunk(
        rx_wire,
        [&](const std::string& out) { forwarded.append(out); },
        [&](const char* payload, size_t len) {
            proto.QueueTransportPong(1, std::string(payload, len));
        }));

    // Feed the intercepted frames into WebSocket: opcode 0x9 was rewritten to 0xA!
    const uint8_t rewritten_opcode = static_cast<uint8_t>(forwarded[handshake.size()]) & 0x0F;
    proto.websocket_->FeedInterceptedFrame(rewritten_opcode, "ping");
    proto.websocket_->FeedInterceptedFrame(0x2, "audio");
    proto.websocket_->FeedInterceptedFrame(0x1, "json");
    rx_finished_draining.set_value();

    assert(main_future.get() == -1);
    assert(proto.websocket_->control_frames_on_rx_ == 0);
    assert(proto.websocket_->pong_callbacks_ == 1);
    assert(proto.websocket_->data_frames_ == 2);
    assert(proto.transport_actions_.size() == 1);

    // 3. When main_task drains transport actions on a healthy socket, the masked Pong is sent.
    proto.DrainTransportActions();
    assert(proto.ssl_.raw_frames.size() == 1);
    assert(static_cast<uint8_t>(proto.ssl_.raw_frames[0][0]) == 0x8A);
    assert(proto.retired_attempt_ == 0);

    // 4. If SendRawFrame fails during DrainTransportActions, the transport attempt is retired cleanly.
    proto.ssl_.allow_send = false;
    assert(proto.QueueTransportPong(1, "ping2"));
    proto.DrainTransportActions();
    assert(proto.retired_attempt_ == 1);
    assert(proto.error_occurred_);

    std::puts("Concurrent send stall & Ping interception simulation passed");
    return 0;
}
""",
    )
    print(output.strip())


def test_protocol_wires_bounded_tls_and_ping_interceptor() -> None:
    header = PROTOCOL_HEADER.read_text(encoding="utf-8")
    source = PROTOCOL_SOURCE.read_text(encoding="utf-8")
    assert "kSendPong" in header
    assert "QueueTransportPong" in header
    assert "CreateMediaWebSocket" in header
    assert "RetireOrDestroyWebSocket" in header
    assert "ReapRetiredWebSockets" in header
    assert "retired_websockets_" in header
    assert '#include "memoria_ws_tls_guard.h"' in source
    assert "WriteBoundedTls(" in source
    assert "WebSocketPingInterceptor" in source
    assert "CreateMediaWebSocket(websocket_attempt, dedicated_wifi_wss)" in source
    assert "CanDestroyCallbackOwner()" in source
    assert "RetireOrDestroyWebSocket(" in source


if __name__ == "__main__":
    with TemporaryDirectory(prefix="memoria-ws-tls-guard-") as directory:
        tmp = Path(directory)
        for fn in (
            test_write_bounded_tls_bounds_want_write_and_handles_partial_writes,
            test_websocket_ping_interceptor_rewrites_ping_and_preserves_frames,
            test_real_memoria_ssl_transport_lifecycle_and_uaf_safety,
            test_real_websocket_and_protocol_retirement_lifecycle_across_all_disconnect_paths,
            test_concurrent_send_stall_does_not_block_ssl_receive_on_ping,
        ):
            sub = tmp / fn.__name__
            sub.mkdir()
            fn(sub)
        test_protocol_wires_bounded_tls_and_ping_interceptor()
    print("ws tls guard host checks passed")
