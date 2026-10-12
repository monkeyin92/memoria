// Host driver for memoria_diag_pcm.h: one Recorder over a heap arena, driven by a line script on stdin, so
// the tests exercise the real recorder, journal and PCMD encoder that the robot runs.
//
//   arm <id> <now_us>                          -> ok | <reason>
//   feed <frames> <channels> <flags> <now_us>  mic = running counter, ref = its negation
//   post <samples> <flags> <now_us>
//   play <samples> <rate> <flags> <now_us>
//   tick <now_us>                              -> sealed | -
//   dump                                       -> the PCMD lines, or one failed line
//   state                                      -> idle | rec | sealed | dumping
//   stress <ms>                                three writer threads race one arm and one seal; then dump

#include "memoria_diag_pcm.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>

namespace diag = memoria::diag;

namespace {

std::vector<std::uint8_t> g_arena(diag::kArenaBytes);
diag::Recorder g_recorder;
std::int16_t g_mic = 0;
std::int16_t g_post = 0;
std::int16_t g_play = 0;

void Emit(const char* line, std::size_t length) { std::printf("%.*s\n", static_cast<int>(length), line); }

void Dump() {
    const char* refused = g_recorder.BeginDump();
    if (refused != nullptr) {
        char line[diag::kPcmdLineCapacity];
        diag::PcmdFailLine(line, sizeof(line), g_recorder.id(), refused);
        std::printf("%s\n", line);
        return;
    }
    diag::StreamJournal(g_recorder.View(), g_recorder.id(), Emit);
    g_recorder.EndDump();
}

void Feed(std::size_t frames, int channels, unsigned flags, long long now) {
    std::vector<std::int16_t> pcm(frames * static_cast<std::size_t>(channels));
    for (std::size_t i = 0; i < frames; ++i) {
        pcm[i * channels] = g_mic;
        if (channels > 1) {
            pcm[i * channels + 1] = static_cast<std::int16_t>(-g_mic);
        }
        ++g_mic;
    }
    g_recorder.RecordFeed(pcm.data(), frames, channels, static_cast<std::uint8_t>(flags), now);
}

void Mono(std::int16_t& counter, std::size_t samples, std::vector<std::int16_t>& pcm) {
    pcm.resize(samples);
    for (auto& sample : pcm) {
        sample = counter++;
    }
}

long long NowUs() {
    return std::chrono::duration_cast<std::chrono::microseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

void Stress(int ms) {
    std::atomic<bool> stop{false};
    auto writer = [&](int which) {
        std::vector<std::int16_t> pcm(480 * 2);
        while (!stop.load()) {
            const long long now = NowUs();
            if (which == 0) {
                g_recorder.RecordFeed(pcm.data(), 160, 2, 0, now);
            } else if (which == 1) {
                g_recorder.RecordPostAfe(pcm.data(), 512, 0, now);
            } else {
                g_recorder.RecordPlay(pcm.data(), 480, 24000, 0, now);
            }
            std::this_thread::yield();
        }
    };
    std::thread threads[] = {std::thread(writer, 0), std::thread(writer, 1), std::thread(writer, 2)};
    const long long armed = NowUs();
    std::printf("%s\n", g_recorder.Arm(0x5eed, armed) ? "refused" : "armed");
    std::this_thread::sleep_for(std::chrono::milliseconds(ms));
    g_recorder.Tick(armed + static_cast<long long>(diag::kWindowMs) * 1000);
    stop.store(true);
    for (auto& thread : threads) {
        thread.join();
    }
    Dump();
}

}  // namespace

int main() {
    g_recorder.Attach(g_arena.data());
    char line[256];
    std::vector<std::int16_t> pcm;
    while (std::fgets(line, sizeof(line), stdin) != nullptr) {
        line[std::strcspn(line, "\r\n")] = '\0';
        unsigned long id = 0, frames = 0, flags = 0;
        int channels = 0, rate = 0, ms = 0;
        long long now = 0;
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        } else if (std::sscanf(line, "arm %lx %lld", &id, &now) == 2) {
            const char* refused = g_recorder.Arm(static_cast<std::uint32_t>(id), now);
            std::printf("%s\n", refused ? refused : "ok");
        } else if (std::sscanf(line, "feed %lu %d %lu %lld", &frames, &channels, &flags, &now) == 4) {
            Feed(frames, channels, flags, now);
        } else if (std::sscanf(line, "post %lu %lu %lld", &frames, &flags, &now) == 3) {
            Mono(g_post, frames, pcm);
            g_recorder.RecordPostAfe(pcm.data(), frames, static_cast<std::uint8_t>(flags), now);
        } else if (std::sscanf(line, "play %lu %d %lu %lld", &frames, &rate, &flags, &now) == 4) {
            Mono(g_play, frames, pcm);
            g_recorder.RecordPlay(pcm.data(), frames, rate, static_cast<std::uint8_t>(flags), now);
        } else if (std::sscanf(line, "tick %lld", &now) == 1) {
            std::printf("%s\n", g_recorder.Tick(now) ? "sealed" : "-");
        } else if (std::strcmp(line, "dump") == 0) {
            Dump();
        } else if (std::strcmp(line, "state") == 0) {
            std::printf("%s\n", diag::StateName(g_recorder.state()));
        } else if (std::sscanf(line, "stress %d", &ms) == 1) {
            Stress(ms);
        } else {
            std::fprintf(stderr, "bad line: %s\n", line);
            return 2;
        }
        std::fflush(stdout);
    }
    return 0;
}
