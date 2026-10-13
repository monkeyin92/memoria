#pragma once

// Bench-only synchronized PCM capture (TODOLIST N-10): a one-shot journal of what three audio tasks handled
// during a fixed window, so a later bench session can tell whether a missed spoken "stop" was lost in the
// echo reference, in AEC/NS, or upstream of them.
//
// A bench build (CONFIG_MEMORIA_BENCH_SERIAL; never published, see memoria_bench_snap.h) answers two more USB
// lines:
//
//   pcmarm\n   start recording now; the journal seals itself kWindowMs later
//   pcmdump\n  stream the sealed journal as PCMD log lines (re-sendable: same id, same bytes)
//
// The journal has three append-only lanes in one PSRAM arena, each written by exactly one task:
//
//   play_pre_i2s              the PCM AudioOutputTask hands to OutputData. On this board there is no software
//                             volume, so these are the bytes written to I2S; the ES8311 DAC volume applies
//                             after them, so they are NOT the exact DAC signal.
//   pre_afe_mic, pre_afe_ref  interleaved, exactly what AudioInputTask reads to Feed the AFE. On VoCat,
//                             pre_afe_ref is the software-injected play path (WritePlay into a 24 kHz ring,
//                             ApplyToFeed over ES7210 ch1 before the 24→16 kHz resample), not the empty
//                             hardware ADC channel.
//   post_afe                  afe_fetch_result_t::data: AEC and NS merged. One AFE instance offers no separate
//                             post-AEC or post-NS buffer, so this lane is never split or renamed that way.
//
// Every block carries one invented timebase, the mic clock: 16 kHz samples read for Feed since arm. Downlink
// sample positions are a different clock and are not used. Writers never wrap or wait; the USB task seals
// every lane before it reads a byte, so readers and writers never share bytes.
//
// Wire lines (the CRC is the one zlib.crc32 computes; chunk base64 as in memoria_bench_snap.h):
//
//   PCMD <id> armed window_ms=<ms> lanes=<lane specs> psram_free=<n>
//   PCMD <id> sealed mic=<samples> bytes=<journal bytes>
//   PCMD <id> begin fmt=memoria-diagpcm-v1 bytes=<B> chunks=<N>
//   PCMD <id> <seq>/<N> <crc> <base64>
//   PCMD <id> end <N> <B> <crc> mic=<m> blocks=<p>,<f>,<a> drops=<p>,<f>,<a> cut=<p>,<f>,<a>
//   PCMD <id> failed reason=<recording|busy|empty|no_psram|console|encode>
//
// scripts/diag_pcm_extract.py verifies the journal and writes one WAV per channel. This header has no ESP-IDF
// dependency so the host tests compile the real recorder and encoder; without the bench switch it is empty.

#if __has_include("sdkconfig.h")
#include "sdkconfig.h"
#endif
#ifndef CONFIG_MEMORIA_BENCH_SERIAL
#define CONFIG_MEMORIA_BENCH_SERIAL 0
#endif

#if CONFIG_MEMORIA_BENCH_SERIAL

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>

#include "memoria_bench_snap.h"

namespace memoria {
namespace diag {

constexpr std::uint32_t kWindowMs = 3000;
constexpr std::uint32_t kMicRate = 16000;
constexpr std::uint32_t kPlayRate = 24000;

enum class Lane : std::uint8_t { kPlay = 0, kFeed = 1, kPostAfe = 2 };
constexpr std::size_t kLaneCount = 3;

struct LaneSpec {
    std::uint32_t rate;
    std::uint8_t channels;
    std::uint32_t capacity;
    const char* channel_names;
};

// Room for the window's PCM plus a quarter, plus 16 KiB for block headers: playback can run ahead of real time
// by the depth of the I2S DMA ring.
constexpr std::uint32_t LaneCapacity(std::uint32_t rate, std::uint32_t channels) {
    return rate / 1000 * kWindowMs * channels * 2 * 5 / 4 + 16 * 1024;
}

constexpr LaneSpec kLaneSpecs[kLaneCount] = {
    {kPlayRate, 1, LaneCapacity(kPlayRate, 1), "play_pre_i2s"},
    {kMicRate, 2, LaneCapacity(kMicRate, 2), "pre_afe_mic,pre_afe_ref"},
    {kMicRate, 1, LaneCapacity(kMicRate, 1), "post_afe"},
};

constexpr std::uint32_t kArenaBytes =
    kLaneSpecs[0].capacity + kLaneSpecs[1].capacity + kLaneSpecs[2].capacity;

enum : std::uint8_t {
    kFlagSettleDropped = 1u << 0,    // feed: input_settle dropped it, the AFE never saw it
    kFlagOutputReenabled = 1u << 1,  // play: the codec output was off and was enabled for this frame
    kFlagWakeConsumer = 1u << 2,     // post_afe: the wake / stop-keyword path read it
    kFlagUplinkConsumer = 1u << 3,   // post_afe: the uplink encoder read it
};

constexpr std::uint8_t ConsumerFlags(bool wake, bool uplink) {
    return static_cast<std::uint8_t>((wake ? kFlagWakeConsumer : 0) | (uplink ? kFlagUplinkConsumer : 0));
}

// Little-endian on the wire as in memory (the ESP32-S3 and the hosts that read it are little-endian).
struct BlockHeader {
    std::uint16_t magic;
    std::uint8_t lane;
    std::uint8_t flags;
    std::uint32_t lane_seq;
    std::uint32_t lane_sample;  // first frame of this block in the lane's own rate, counted from arm
    std::uint32_t mic_clock;    // mic samples since arm when the writer took this block
    std::uint32_t t_us;         // esp_timer since arm, for reading against log time stamps only
    std::uint16_t frames;
    std::uint16_t channels;
};
static_assert(sizeof(BlockHeader) == 24, "memoria-diagpcm-v1 block header");
constexpr std::uint16_t kBlockMagic = 0xD1A6;

struct LaneSummary {
    std::uint8_t lane;
    std::uint8_t channels;
    std::uint16_t reserved;
    std::uint32_t rate;
    std::uint32_t used_bytes;
    std::uint32_t blocks;
    std::uint16_t drops;  // blocks refused: the lane was full or the block did not fit the lane's shape
    std::uint16_t cut;    // blocks whose copy lost the race with the seal
};
static_assert(sizeof(LaneSummary) == 20, "memoria-diagpcm-v1 lane summary");

struct JournalPreamble {
    char magic[8];
    std::uint32_t id;
    std::uint32_t window_ms;
    std::uint32_t mic_samples_at_seal;
    std::uint32_t arm_us_lo;
    std::uint32_t seal_us_lo;
    std::uint32_t lane_count;
    LaneSummary lanes[kLaneCount];
};
static_assert(sizeof(JournalPreamble) == 92, "memoria-diagpcm-v1 preamble");
constexpr char kPreambleMagic[8] = {'M', 'D', 'P', 'C', 'M', '1', '\0', '\0'};

// The dump: the preamble, then each lane's committed bytes in Lane order.
struct JournalView {
    const JournalPreamble* preamble = nullptr;
    const std::uint8_t* lane_base[kLaneCount] = {};
    std::uint32_t lane_bytes[kLaneCount] = {};

    std::size_t total_bytes() const {
        std::size_t total = sizeof(JournalPreamble);
        for (std::uint32_t bytes : lane_bytes) {
            total += bytes;
        }
        return total;
    }

    // Copies up to `want` bytes of the journal starting `offset` bytes in; returns the bytes copied.
    std::size_t Gather(std::size_t offset, std::uint8_t* out, std::size_t want) const {
        const std::uint8_t* spans[kLaneCount + 1] = {reinterpret_cast<const std::uint8_t*>(preamble),
                                                     lane_base[0], lane_base[1], lane_base[2]};
        const std::size_t sizes[kLaneCount + 1] = {sizeof(JournalPreamble), lane_bytes[0], lane_bytes[1],
                                                   lane_bytes[2]};
        std::size_t copied = 0;
        std::size_t span_start = 0;
        for (std::size_t i = 0; i < kLaneCount + 1 && copied < want; ++i) {
            const std::size_t span_end = span_start + sizes[i];
            const std::size_t position = offset + copied;
            if (position < span_end) {
                std::size_t take = span_end - position;
                if (take > want - copied) {
                    take = want - copied;
                }
                std::memcpy(out + copied, spans[i] + (position - span_start), take);
                copied += take;
            }
            span_start = span_end;
        }
        return copied;
    }
};

enum class State : std::uint8_t { kIdle, kRecording, kSealed, kDumping };

inline const char* StateName(State state) {
    switch (state) {
        case State::kIdle:
            return "idle";
        case State::kRecording:
            return "rec";
        case State::kSealed:
            return "sealed";
        case State::kDumping:
            return "dumping";
    }
    return "unknown";
}

// One lane's commit word: [31] closed, [30:24] epoch, [23:0] committed bytes. A writer copies past the
// committed offset and then publishes with one CAS; the sealer sets the closed bit with one fetch_or, after
// which every byte below the offset is final. The epoch changes at every arm, so a writer preempted across a
// seal and the next arm cannot commit into the new capture even when both offsets are zero.
struct LaneCursor {
    static constexpr std::uint32_t kClosed = 1u << 31;
    static constexpr std::uint32_t kOffsetMask = (1u << 24) - 1;

    std::atomic<std::uint32_t> word{kClosed};
    std::atomic<std::uint16_t> drops{0};
    std::atomic<std::uint16_t> cut{0};
    // Writer-owned: only the lane's one task touches these.
    std::uint32_t next_seq = 0;
    std::uint32_t next_sample = 0;
};

// The whole capture. The USB task alone calls Attach/Arm/Tick/BeginDump/EndDump; each Record* has exactly one
// calling task (input, AFE processing, output). Constant-initialisable, so the device's single instance needs
// no guard on the audio path.
class Recorder {
public:
    constexpr Recorder() = default;

    void Attach(std::uint8_t* arena) { arena_ = arena; }
    bool attached() const { return arena_ != nullptr; }
    State state() const { return state_.load(std::memory_order_relaxed); }
    std::uint32_t id() const { return id_; }

    // Idle or Sealed → Recording. Returns nullptr, or why not.
    const char* Arm(std::uint32_t id, std::int64_t now_us) {
        const State current = state();
        if (current == State::kRecording) {
            return "recording";
        }
        if (current == State::kDumping) {
            return "busy";
        }
        if (arena_ == nullptr) {
            return "no_psram";
        }
        id_ = id;
        arm_us_ = now_us;
        arm_mic_.store(mic_clock_.load(std::memory_order_relaxed), std::memory_order_relaxed);
        arm_us_lo_.store(static_cast<std::uint32_t>(now_us), std::memory_order_relaxed);
        epoch_ = static_cast<std::uint8_t>((epoch_ + 1) & 0x7f);
        for (LaneCursor& cursor : cursors_) {
            cursor.drops.store(0, std::memory_order_relaxed);
            cursor.cut.store(0, std::memory_order_relaxed);
            cursor.word.store(static_cast<std::uint32_t>(epoch_) << 24, std::memory_order_release);
        }
        state_.store(State::kRecording, std::memory_order_relaxed);
        return nullptr;
    }

    // Seals the journal once the window has passed; true exactly when it sealed on this call.
    bool Tick(std::int64_t now_us) {
        if (state() != State::kRecording || now_us - arm_us_ < static_cast<std::int64_t>(kWindowMs) * 1000) {
            return false;
        }
        Seal(now_us);
        return true;
    }

    // Sealed → Dumping. Returns nullptr, or why not.
    const char* BeginDump() {
        switch (state()) {
            case State::kIdle:
                return "empty";
            case State::kRecording:
                return "recording";
            case State::kDumping:
                return "busy";
            case State::kSealed:
                break;
        }
        state_.store(State::kDumping, std::memory_order_relaxed);
        return nullptr;
    }

    void EndDump() {
        if (state() == State::kDumping) {
            state_.store(State::kSealed, std::memory_order_relaxed);
        }
    }

    // Valid while Sealed or Dumping.
    JournalView View() const {
        JournalView view;
        view.preamble = &preamble_;
        std::uint32_t base = 0;
        for (std::size_t i = 0; i < kLaneCount; ++i) {
            view.lane_base[i] = arena_ + base;
            view.lane_bytes[i] = preamble_.lanes[i].used_bytes;
            base += kLaneSpecs[i].capacity;
        }
        return view;
    }

    const JournalPreamble& preamble() const { return preamble_; }

    // Input task. Advances the mic clock on every call, recording or not, so it exists before any arm.
    void RecordFeed(const std::int16_t* interleaved, std::size_t frames, int channels, std::uint8_t flags,
                    std::int64_t now_us) {
        const std::uint32_t start = mic_clock_.load(std::memory_order_relaxed);
        Append(Lane::kFeed, start, interleaved, frames, channels, flags, now_us);
        mic_clock_.store(start + static_cast<std::uint32_t>(frames), std::memory_order_relaxed);
    }

    // AFE processing task.
    void RecordPostAfe(const std::int16_t* pcm, std::size_t samples, std::uint8_t flags, std::int64_t now_us) {
        Append(Lane::kPostAfe, mic_clock_.load(std::memory_order_relaxed), pcm, samples, 1, flags, now_us);
    }

    // Output task. A rate other than the lane's is refused (counted as a drop) rather than resampled.
    void RecordPlay(const std::int16_t* pcm, std::size_t samples, int sample_rate, std::uint8_t flags,
                    std::int64_t now_us) {
        if (sample_rate != static_cast<int>(kPlayRate)) {
            Refuse(Lane::kPlay);
            return;
        }
        Append(Lane::kPlay, mic_clock_.load(std::memory_order_relaxed), pcm, samples, 1, flags, now_us);
    }

private:
    void Refuse(Lane lane) {
        LaneCursor& cursor = cursors_[static_cast<std::size_t>(lane)];
        if ((cursor.word.load(std::memory_order_relaxed) & LaneCursor::kClosed) == 0) {
            cursor.drops.fetch_add(1, std::memory_order_relaxed);
        }
    }

    void Append(Lane lane, std::uint32_t mic_now, const std::int16_t* pcm, std::size_t frames, int channels,
                std::uint8_t flags, std::int64_t now_us) {
        const std::size_t index = static_cast<std::size_t>(lane);
        LaneCursor& cursor = cursors_[index];
        std::uint32_t word = cursor.word.load(std::memory_order_acquire);
        if ((word & LaneCursor::kClosed) != 0) {
            return;
        }
        const LaneSpec& spec = kLaneSpecs[index];
        const std::size_t samples = frames * static_cast<std::size_t>(channels);
        const std::size_t bytes = sizeof(BlockHeader) + samples * sizeof(std::int16_t);
        const std::uint32_t offset = word & LaneCursor::kOffsetMask;
        if (pcm == nullptr || frames == 0 || frames > 0xffff || channels != spec.channels ||
            offset + bytes > spec.capacity) {
            cursor.drops.fetch_add(1, std::memory_order_relaxed);
            return;
        }
        // The first block of a capture is the one at offset 0: the writer's counters restart there.
        if (offset == 0) {
            cursor.next_seq = 0;
            cursor.next_sample = 0;
        }
        BlockHeader header{};
        header.magic = kBlockMagic;
        header.lane = static_cast<std::uint8_t>(lane);
        header.flags = flags;
        header.lane_seq = cursor.next_seq;
        header.lane_sample = cursor.next_sample;
        header.mic_clock = mic_now - arm_mic_.load(std::memory_order_relaxed);
        header.t_us = static_cast<std::uint32_t>(now_us) - arm_us_lo_.load(std::memory_order_relaxed);
        header.frames = static_cast<std::uint16_t>(frames);
        header.channels = static_cast<std::uint16_t>(channels);
        std::uint8_t* at = arena_ + LaneBase(index) + offset;
        std::memcpy(at, &header, sizeof(header));
        std::memcpy(at + sizeof(header), pcm, samples * sizeof(std::int16_t));
        if (!cursor.word.compare_exchange_strong(word, word + static_cast<std::uint32_t>(bytes),
                                                 std::memory_order_release, std::memory_order_relaxed)) {
            cursor.cut.fetch_add(1, std::memory_order_relaxed);
            return;
        }
        ++cursor.next_seq;
        cursor.next_sample += static_cast<std::uint32_t>(frames);
    }

    static constexpr std::uint32_t LaneBase(std::size_t index) {
        std::uint32_t base = 0;
        for (std::size_t i = 0; i < index; ++i) {
            base += kLaneSpecs[i].capacity;
        }
        return base;
    }

    void Seal(std::int64_t now_us) {
        JournalPreamble p{};
        std::memcpy(p.magic, kPreambleMagic, sizeof(p.magic));
        p.id = id_;
        p.window_ms = kWindowMs;
        p.arm_us_lo = static_cast<std::uint32_t>(arm_us_);
        p.seal_us_lo = static_cast<std::uint32_t>(now_us);
        p.lane_count = kLaneCount;
        for (std::size_t i = 0; i < kLaneCount; ++i) {
            LaneCursor& cursor = cursors_[i];
            const std::uint32_t final_word = cursor.word.fetch_or(LaneCursor::kClosed, std::memory_order_acq_rel);
            LaneSummary& lane = p.lanes[i];
            lane.lane = static_cast<std::uint8_t>(i);
            lane.channels = kLaneSpecs[i].channels;
            lane.rate = kLaneSpecs[i].rate;
            lane.used_bytes = final_word & LaneCursor::kOffsetMask;
            lane.blocks = CountBlocks(arena_ + LaneBase(i), lane.used_bytes);
            lane.drops = cursor.drops.load(std::memory_order_relaxed);
            lane.cut = cursor.cut.load(std::memory_order_relaxed);
        }
        p.mic_samples_at_seal = mic_clock_.load(std::memory_order_relaxed) - arm_mic_.load(std::memory_order_relaxed);
        preamble_ = p;
        state_.store(State::kSealed, std::memory_order_relaxed);
    }

    static std::uint32_t CountBlocks(const std::uint8_t* base, std::uint32_t used) {
        std::uint32_t blocks = 0;
        std::uint32_t at = 0;
        while (at + sizeof(BlockHeader) <= used) {
            BlockHeader header;
            std::memcpy(&header, base + at, sizeof(header));
            at += static_cast<std::uint32_t>(sizeof(header) + header.frames * header.channels * sizeof(std::int16_t));
            ++blocks;
        }
        return blocks;
    }

    std::uint8_t* arena_ = nullptr;
    std::atomic<State> state_{State::kIdle};
    std::uint32_t id_ = 0;
    std::int64_t arm_us_ = 0;
    std::uint8_t epoch_ = 0;
    std::atomic<std::uint32_t> mic_clock_{0};
    std::atomic<std::uint32_t> arm_mic_{0};
    std::atomic<std::uint32_t> arm_us_lo_{0};
    LaneCursor cursors_[kLaneCount];
    JournalPreamble preamble_{};
};

constexpr std::size_t kPcmdLineCapacity = bench::kSnapLineCapacity + 64;

inline std::size_t FinishLine(int written, std::size_t capacity) {
    return written > 0 && static_cast<std::size_t>(written) < capacity ? static_cast<std::size_t>(written) : 0;
}

inline std::size_t PcmdArmedLine(char* out, std::size_t capacity, std::uint32_t id, std::uint32_t psram_free) {
    return FinishLine(
        std::snprintf(out, capacity,
                      "PCMD %08x armed window_ms=%u lanes=play_pre_i2s@%ux1,pre_afe_mic+pre_afe_ref@%ux2,"
                      "post_afe@%ux1 psram_free=%u",
                      static_cast<unsigned>(id), static_cast<unsigned>(kWindowMs), static_cast<unsigned>(kPlayRate),
                      static_cast<unsigned>(kMicRate), static_cast<unsigned>(kMicRate),
                      static_cast<unsigned>(psram_free)),
        capacity);
}

inline std::size_t PcmdSealedLine(char* out, std::size_t capacity, const JournalView& view) {
    return FinishLine(std::snprintf(out, capacity, "PCMD %08x sealed mic=%u bytes=%zu",
                                    static_cast<unsigned>(view.preamble->id),
                                    static_cast<unsigned>(view.preamble->mic_samples_at_seal), view.total_bytes()),
                      capacity);
}

inline std::size_t PcmdFailLine(char* out, std::size_t capacity, std::uint32_t id, const char* reason) {
    return FinishLine(std::snprintf(out, capacity, "PCMD %08x failed reason=%s", static_cast<unsigned>(id),
                                    reason != nullptr ? reason : "unknown"),
                      capacity);
}

inline std::size_t PcmdChunkLine(char* out, std::size_t capacity, std::uint32_t id, std::size_t seq,
                                 std::size_t total, const std::uint8_t* bytes, std::size_t length) {
    if (out == nullptr || bytes == nullptr || length == 0 || length > bench::kSnapChunkBytes || seq == 0 ||
        seq > total) {
        return 0;
    }
    char encoded[bench::Base64Length(bench::kSnapChunkBytes) + 1];
    bench::Base64Encode(bytes, length, encoded);
    return FinishLine(std::snprintf(out, capacity, "PCMD %08x %zu/%zu %08x %s", static_cast<unsigned>(id), seq,
                                    total, static_cast<unsigned>(bench::Crc32(0, bytes, length)), encoded),
                      capacity);
}

inline std::size_t PcmdEndLine(char* out, std::size_t capacity, std::uint32_t id, std::size_t total,
                               std::size_t bytes, std::uint32_t crc, const JournalPreamble& p) {
    const LaneSummary* l = p.lanes;
    return FinishLine(
        std::snprintf(out, capacity, "PCMD %08x end %zu %zu %08x mic=%u blocks=%u,%u,%u drops=%u,%u,%u cut=%u,%u,%u",
                      static_cast<unsigned>(id), total, bytes, static_cast<unsigned>(crc),
                      static_cast<unsigned>(p.mic_samples_at_seal), static_cast<unsigned>(l[0].blocks),
                      static_cast<unsigned>(l[1].blocks), static_cast<unsigned>(l[2].blocks), l[0].drops, l[1].drops,
                      l[2].drops, l[0].cut, l[1].cut, l[2].cut),
        capacity);
}

// Streams the journal: begin, every chunk, end; one `emit(line, length)` per line. A journal that cannot be
// encoded emits one failure line instead and returns false.
template <typename Emit>
bool StreamJournal(const JournalView& view, std::uint32_t id, Emit&& emit) {
    char line[kPcmdLineCapacity];
    const std::size_t bytes = view.total_bytes();
    const std::size_t total = bench::SnapChunkCount(bytes);
    std::size_t length = FinishLine(std::snprintf(line, sizeof(line), "PCMD %08x begin fmt=memoria-diagpcm-v1 "
                                                                      "bytes=%zu chunks=%zu",
                                                  static_cast<unsigned>(id), bytes, total),
                                    sizeof(line));
    if (length == 0) {
        return false;
    }
    emit(static_cast<const char*>(line), length);
    std::uint8_t chunk[bench::kSnapChunkBytes];
    std::uint32_t crc = 0;
    for (std::size_t seq = 1; seq <= total; ++seq) {
        const std::size_t got = view.Gather((seq - 1) * bench::kSnapChunkBytes, chunk, sizeof(chunk));
        length = PcmdChunkLine(line, sizeof(line), id, seq, total, chunk, got);
        if (got == 0 || length == 0) {
            length = PcmdFailLine(line, sizeof(line), id, "encode");
            if (length > 0) {
                emit(static_cast<const char*>(line), length);
            }
            return false;
        }
        crc = bench::Crc32(crc, chunk, got);
        emit(static_cast<const char*>(line), length);
    }
    length = PcmdEndLine(line, sizeof(line), id, total, bytes, crc, *view.preamble);
    if (length == 0) {
        return false;
    }
    emit(static_cast<const char*>(line), length);
    return true;
}

// Device side (memoria_diag_pcm.cc): the one Recorder, its PSRAM arena and the console. The audio code and the
// board name nothing else.
void RecordFeed(const std::int16_t* interleaved, std::size_t frames, int channels, std::uint8_t flags);
void RecordPostAfe(const std::int16_t* pcm, std::size_t samples, std::uint8_t flags);
void RecordPlay(const std::int16_t* pcm, std::size_t samples, int sample_rate, std::uint8_t flags);
void Arm();
void Tick();
void Dump();

}  // namespace diag
}  // namespace memoria

#endif  // CONFIG_MEMORIA_BENCH_SERIAL
