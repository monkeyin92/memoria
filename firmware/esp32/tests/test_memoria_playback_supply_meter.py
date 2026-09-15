"""Host-side behavioural checks for the Memoria playback supply meter.

memoria_playback_supply_meter.h has no ESP-IDF dependency, so the exact firmware
state machine is compiled on the host and fed the same events the AudioService
consumer, codec task and flush paths produce: generation announcements, decode
dequeue observations, codec submissions, playout-queue waits, exact DMA
completion polls and episode closes.

The tests drive that logic and read its fields, not its log strings.  The
harness carries two extras:

* a reference implementation of the counter this patch replaced, so the
  2026-09-14 session-4 miss-attribution is shown to happen with the old
  algorithm and not with the new one on the same event stream;
* named episode tokens, so the codec submission that AudioOutputTask performs
  outside the queue lock can be replayed after a flush, a generation switch or a
  stop exactly as the firmware performs it.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
AUDIO_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "audio"
HEADER = AUDIO_DIR / "memoria_playback_supply_meter.h"
METER_PATCH = FIRMWARE_ROOT / "overlay" / "patches" / "0025-playback-underrun-metering.patch"

SUMMARY_KEYS = [
    "layer",
    "scope",
    "generation",
    "close",
    "output_frames",
    "first_output",
    "first_output_latency_ms",
    "supply_waits",
    "supply_max_ms",
    "supply_total_ms",
    "prestart_waits",
    "prestart_max_ms",
    "boundary_waits",
    "boundary_max_ms",
    "close_dropped_waits",
    "close_dropped_ms",
    "outside_waits",
    "outside_max_ms",
    "exact_confirmed",
    "exact_timeouts",
    "exact_polls",
]

HARNESS = r"""
#include "memoria_playback_supply_meter.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

memoria::PlaybackSupplyMeter meter;

/* Reference implementation of the counter this patch replaced.  It keeps the
 * two defects the 2026-09-14 session-4 log exposed: the streaming flag is read
 * when the wait returns instead of when it begins, and it is never cleared on
 * an end of stream, flush or cancel.  Its output token is legacy_counted
 * because the replaced code printed these lines as "media playback starved
 * ...", a claim this patch withdraws. */
struct LegacyMeter {
    bool streaming = false;
    uint32_t generation = 0;
    uint32_t stream_generation = 0;
    uint32_t counted = 0;
    int64_t max_ms = 0;
    int64_t total_ms = 0;
    int64_t wait_from_ms = 0;
    bool waiting = false;

    void Decode(uint32_t decoded_generation) {
        if (decoded_generation != generation) {
            generation = decoded_generation;
            stream_generation = decoded_generation;
            streaming = true;
            counted = 0;
            max_ms = 0;
            total_ms = 0;
        }
    }

    void WaitBegin(int64_t now_ms) {
        waiting = true;
        wait_from_ms = now_ms;
    }

    void WaitEnd(int64_t now_ms, bool stopped) {
        if (!waiting) {
            return;
        }
        waiting = false;
        if (stopped || !streaming) {
            return;
        }
        const int64_t gap_ms = now_ms - wait_from_ms;
        if (gap_ms >= 40) {
            ++counted;
            total_ms += gap_ms;
            if (gap_ms > max_ms) {
                max_ms = gap_ms;
            }
            std::printf("legacy_counted gen=%u gap_ms=%lld count=%u\n",
                        static_cast<unsigned int>(stream_generation),
                        static_cast<long long>(gap_ms),
                        static_cast<unsigned int>(counted));
        }
    }
};

LegacyMeter legacy;

/* Reference implementation of the replaced submission rule.  The counter this
 * patch replaced decided from whatever episode happened to be current when the
 * submission was recorded, so a consumer that returned from the codec after a
 * flush closed the newer episode, emitted its summary early and reopened an
 * episode for the stale generation.  AudioOutputTask submits outside the audio
 * queue lock, which is exactly when that happens. */
struct LegacyEpisodeMeter {
    bool open = false;
    uint32_t generation = 0;
    uint32_t frames = 0;
    uint32_t summaries = 0;

    void Announce(uint32_t announced) {
        if (open) {
            ++summaries;
        }
        open = true;
        generation = announced;
        frames = 0;
    }

    void CloseEpisode() {
        if (open) {
            ++summaries;
            open = false;
            generation = 0;
            frames = 0;
        }
    }

    void Submit(uint32_t submitted) {
        if (submitted == 0) {
            return;
        }
        if (!open || generation != submitted) {
            if (open) {
                ++summaries;
            }
            open = true;
            generation = submitted;
            frames = 0;
        }
        ++frames;
    }
};

LegacyEpisodeMeter legacy_episode;

/* Episode tokens frozen by the consumer while it still owns the queue lock,
 * addressed by the slot names t0..t7 used in the scripts. */
memoria::PlaybackSupplyEpisodeToken slots[8];

int SlotIndex(const char* name) {
    if (name[0] == 't' && name[1] >= '0' && name[1] <= '7' && name[2] == '\0') {
        return name[1] - '0';
    }
    return -1;
}

memoria::SupplyCloseReason ParseReason(const char* name) {
    if (std::strcmp(name, "switch") == 0) {
        return memoria::SupplyCloseReason::kGenerationSwitch;
    }
    if (std::strcmp(name, "flush") == 0) {
        return memoria::SupplyCloseReason::kChannelFlush;
    }
    if (std::strcmp(name, "reset") == 0) {
        return memoria::SupplyCloseReason::kDecoderReset;
    }
    if (std::strcmp(name, "stop") == 0) {
        return memoria::SupplyCloseReason::kServiceStop;
    }
    return memoria::SupplyCloseReason::kNone;
}

void PrintToken(const char* name, const memoria::PlaybackSupplyEpisodeToken& token) {
    std::printf("token name=%s valid=%d id=%llu gen=%u fence=%u\n", name,
                token.valid ? 1 : 0,
                static_cast<unsigned long long>(token.episode_id),
                static_cast<unsigned int>(token.generation),
                static_cast<unsigned int>(token.playback_fence));
}

/* A frozen token is valid as a snapshot of the episode that was open when the
 * consumer dequeued the frame; current says whether it still names the episode
 * that is open now. */
void PrintSlotState(const char* name, const memoria::PlaybackSupplyEpisodeToken& token,
                    bool current) {
    std::printf("slot name=%s valid=%d current=%d id=%llu gen=%u fence=%u\n", name,
                token.valid ? 1 : 0, current ? 1 : 0,
                static_cast<unsigned long long>(token.episode_id),
                static_cast<unsigned int>(token.generation),
                static_cast<unsigned int>(token.playback_fence));
}

void PrintSummary(const memoria::SupplyEpisodeSummary& summary) {
    if (!summary.valid) {
        std::printf("summary none\n");
        return;
    }
    std::printf(
        "summary gen=%u close=%s frames=%u first=%s latency=%lld supply=%u "
        "supply_max=%lld supply_total=%lld prestart=%u prestart_max=%lld "
        "boundary=%u boundary_max=%lld close_dropped=%u close_dropped_max=%lld "
        "outside=%u outside_max=%lld exact_ok=%u exact_timeout=%u exact_polls=%u\n",
        static_cast<unsigned int>(summary.generation),
        memoria::SupplyCloseReasonName(summary.reason),
        static_cast<unsigned int>(summary.output_frames),
        summary.first_output_submitted ? "yes" : "no",
        static_cast<long long>(summary.first_output_latency_ms),
        static_cast<unsigned int>(summary.supply.count),
        static_cast<long long>(summary.supply.max_ms),
        static_cast<long long>(summary.supply.total_ms),
        static_cast<unsigned int>(summary.prestart.count),
        static_cast<long long>(summary.prestart.max_ms),
        static_cast<unsigned int>(summary.boundary.count),
        static_cast<long long>(summary.boundary.max_ms),
        static_cast<unsigned int>(summary.close_dropped.count),
        static_cast<long long>(summary.close_dropped.max_ms),
        static_cast<unsigned int>(summary.outside.count),
        static_cast<long long>(summary.outside.max_ms),
        static_cast<unsigned int>(summary.exact_confirmed),
        static_cast<unsigned int>(summary.exact_timeouts),
        static_cast<unsigned int>(summary.exact_polls));
}

void PrintWait(const memoria::SupplyWaitRecord& record) {
    if (!record.valid) {
        std::printf("wait none\n");
        return;
    }
    std::printf("wait gen=%u ms=%lld count=%u\n",
                static_cast<unsigned int>(record.generation),
                static_cast<long long>(record.wait_ms),
                static_cast<unsigned int>(record.supply_waits));
    char buffer[memoria::kSupplyWaitLogCapacity];
    if (memoria::FormatSupplyWaitLog(buffer, sizeof(buffer), record) > 0) {
        std::printf("wait_line %s\n", buffer);
    }
}

void PrintSummaryAndLine(const memoria::SupplyEpisodeSummary& summary) {
    PrintSummary(summary);
    char buffer[memoria::kSupplySummaryLogCapacity];
    if (memoria::FormatSupplySummaryLog(buffer, sizeof(buffer), summary) > 0) {
        std::printf("summary_line %s\n", buffer);
    } else {
        std::printf("summary_line none\n");
    }
}

/* Fill every summary field with its widest possible value and render it into
 * the firmware buffer and into the 512-byte buffer this patch replaced. */
void PrintWide() {
    const int64_t lowest = INT64_MIN;
    memoria::SupplyEpisodeSummary summary;
    summary.valid = true;
    summary.generation = 0xFFFFFFFFu;
    summary.reason = memoria::SupplyCloseReason::kGenerationSwitch;
    summary.output_frames = 0xFFFFFFFFu;
    summary.first_output_submitted = true;
    summary.first_output_latency_ms = lowest;
    summary.supply.count = 0xFFFFFFFFu;
    summary.supply.max_ms = lowest;
    summary.supply.total_ms = lowest;
    summary.prestart.count = 0xFFFFFFFFu;
    summary.prestart.max_ms = lowest;
    summary.boundary.count = 0xFFFFFFFFu;
    summary.boundary.max_ms = lowest;
    summary.close_dropped.count = 0xFFFFFFFFu;
    summary.close_dropped.max_ms = lowest;
    summary.outside.count = 0xFFFFFFFFu;
    summary.outside.max_ms = lowest;
    summary.exact_polls = 0xFFFFFFFFu;
    summary.exact_confirmed = 0xFFFFFFFFu;
    summary.exact_timeouts = 0xFFFFFFFFu;
    char wide[memoria::kSupplySummaryLogCapacity];
    char narrow[512];
    const int wide_written = memoria::FormatSupplySummaryLog(wide, sizeof(wide), summary);
    const int narrow_written = memoria::FormatSupplySummaryLog(narrow, sizeof(narrow), summary);
    std::printf("wide ok=%d len=%d capacity=%zu narrow_ok=%d narrow_len=%d\n",
                wide_written > 0 ? 1 : 0, wide_written, sizeof(wide),
                narrow_written > 0 ? 1 : 0, narrow_written);
}

}  // namespace

static int64_t supply_now_ms = 0;

static int64_t MemoriaSupplyNowMs() { return supply_now_ms; }

static void DecoderResetFromOverlay([[maybe_unused]] uint32_t accepted_server_generation_,
                                    [[maybe_unused]] uint32_t playback_generation_,
                                    int64_t now_ms) {
    auto& playback_supply_meter_ = meter;
    supply_now_ms = now_ms;
    memoria::SupplyEpisodeSummary supply_summary;
    // AUDIO_SERVICE_DECODER_RESET_OBSERVATION
    PrintSummaryAndLine(supply_summary);
}

int main() {
    char line[256];
    while (std::fgets(line, sizeof(line), stdin) != nullptr) {
        char command[32] = {0};
        char arg1[32] = {0};
        char arg2[32] = {0};
        char arg3[32] = {0};
        char arg4[32] = {0};
        const int fields = std::sscanf(line, "%31s %31s %31s %31s %31s",
                                       command, arg1, arg2, arg3, arg4);
        if (fields <= 0 || command[0] == '#') {
            continue;
        }
        if (std::strcmp(command, "announce") == 0) {
            const uint32_t announced = static_cast<uint32_t>(std::strtoul(arg1, nullptr, 10));
            if (announced != 0) {
                legacy_episode.Announce(announced);
            }
            PrintSummary(meter.NoteGenerationAnnounced(
                announced,
                static_cast<uint32_t>(std::strtoul(arg2, nullptr, 10)),
                static_cast<int64_t>(std::strtoll(arg3, nullptr, 10))));
        } else if (std::strcmp(command, "decoder_reset") == 0) {
            DecoderResetFromOverlay(
                static_cast<uint32_t>(std::strtoul(arg1, nullptr, 10)),
                static_cast<uint32_t>(std::strtoul(arg2, nullptr, 10)),
                static_cast<int64_t>(std::strtoll(arg3, nullptr, 10)));
        } else if (std::strcmp(command, "decoded") == 0) {
            const uint32_t generation = static_cast<uint32_t>(std::strtoul(arg1, nullptr, 10));
            legacy.Decode(generation);
            PrintToken("dequeue",
                       meter.NoteDecodeDequeued(
                           generation, static_cast<uint32_t>(std::strtoul(arg2, nullptr, 10))));
        } else if (std::strcmp(command, "freeze") == 0) {
            const int index = SlotIndex(arg1);
            if (index < 0) {
                std::printf("unknown_slot %s\n", arg1);
                return 2;
            }
            slots[index] = meter.CaptureOutputToken(
                static_cast<uint32_t>(std::strtoul(arg2, nullptr, 10)),
                static_cast<uint32_t>(std::strtoul(arg3, nullptr, 10)));
            char label[16];
            std::snprintf(label, sizeof(label), "freeze_%s", arg1);
            PrintToken(label, slots[index]);
        } else if (std::strcmp(command, "submit") == 0) {
            const int index = SlotIndex(arg1);
            if (index < 0) {
                std::printf("unknown_slot %s\n", arg1);
                return 2;
            }
            PrintSummary(meter.NoteOutputSubmitted(
                slots[index], static_cast<int64_t>(std::strtoll(arg2, nullptr, 10))));
            legacy_episode.Submit(slots[index].generation);
        } else if (std::strcmp(command, "arm") == 0) {
            const int index = SlotIndex(arg1);
            if (index < 0) {
                std::printf("unknown_slot %s\n", arg1);
                return 2;
            }
            meter.NoteExactOutputArmed(slots[index]);
        } else if (std::strcmp(command, "wait_begin") == 0) {
            const int64_t now_ms = static_cast<int64_t>(std::strtoll(arg1, nullptr, 10));
            meter.NoteWaitBegin(now_ms);
            legacy.WaitBegin(now_ms);
        } else if (std::strcmp(command, "wait_end") == 0) {
            const int64_t now_ms = static_cast<int64_t>(std::strtoll(arg3, nullptr, 10));
            PrintWait(meter.NoteWaitEnd(std::strcmp(arg1, "yes") == 0,
                                        static_cast<uint32_t>(std::strtoul(arg2, nullptr, 10)),
                                        now_ms));
            legacy.WaitEnd(now_ms, std::strcmp(arg4, "stopped") == 0);
        } else if (std::strcmp(command, "poll_begin") == 0) {
            meter.NoteExactWaitBegin(static_cast<int64_t>(std::strtoll(arg1, nullptr, 10)));
        } else if (std::strcmp(command, "poll_end") == 0) {
            PrintWait(meter.NoteWaitEnd(std::strcmp(arg1, "yes") == 0,
                                        static_cast<uint32_t>(std::strtoul(arg2, nullptr, 10)),
                                        static_cast<int64_t>(std::strtoll(arg3, nullptr, 10))));
        } else if (std::strcmp(command, "exact_ok") == 0) {
            meter.NoteExactCompletionConfirmed();
        } else if (std::strcmp(command, "exact_timeout") == 0) {
            meter.NoteExactCompletionTimeout();
        } else if (std::strcmp(command, "close") == 0) {
            legacy_episode.CloseEpisode();
            PrintSummaryAndLine(meter.Close(ParseReason(arg1),
                                            static_cast<int64_t>(std::strtoll(arg2, nullptr, 10))));
        } else if (std::strcmp(command, "legacy_state") == 0) {
            std::printf("legacy_episode open=%d gen=%u frames=%u summaries=%u\n",
                        legacy_episode.open ? 1 : 0,
                        static_cast<unsigned int>(legacy_episode.generation),
                        static_cast<unsigned int>(legacy_episode.frames),
                        static_cast<unsigned int>(legacy_episode.summaries));
        } else if (std::strcmp(command, "reset") == 0) {
            meter.Reset();
            legacy = LegacyMeter();
            legacy_episode = LegacyEpisodeMeter();
        } else if (std::strcmp(command, "state") == 0) {
            std::printf("state episode=%d gen=%u fence=%u id=%llu playing=%d waiting=%d\n",
                        meter.has_episode() ? 1 : 0,
                        static_cast<unsigned int>(meter.episode_generation()),
                        static_cast<unsigned int>(meter.playback_fence()),
                        static_cast<unsigned long long>(meter.episode_id()),
                        meter.episode_playing() ? 1 : 0,
                        meter.wait_in_flight() ? 1 : 0);
        } else if (std::strcmp(command, "slot_state") == 0) {
            const int index = SlotIndex(arg1);
            if (index < 0) {
                std::printf("unknown_slot %s\n", arg1);
                return 2;
            }
            char label[16];
            std::snprintf(label, sizeof(label), "%s", arg1);
            PrintSlotState(label, slots[index], meter.token_is_current(slots[index]));
        } else if (std::strcmp(command, "wide") == 0) {
            PrintWide();
        } else {
            std::printf("unknown %s\n", command);
            return 2;
        }
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def meter_tool() -> pathlib.Path:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        # Compile the actual observation block wired after ResetDecoderLocked,
        # not a hand-written approximation of that lifecycle event.  This
        # reproduces the speaking-entry close-before-output defect even when
        # the standalone meter's announcement/output tests are all green.
        patched_lines = "\n".join(
            line[1:]
            for line in METER_PATCH.read_text(encoding="utf-8").splitlines()
            if line.startswith(("+", " ")) and not line.startswith("+++")
        )
        reset_method = patched_lines.split("void AudioService::ResetDecoder() {", 1)[1]
        observation = reset_method.split(
            "notify_drained = ResetDecoderLocked(accepted_server_generation_);", 1
        )[1].split("\n    }", 1)[0]
        assert "playback_supply_meter_." in observation
        source.write_text(
            HARNESS.replace("// AUDIO_SERVICE_DECODER_RESET_OBSERVATION", observation),
            encoding="utf-8",
        )
        binary = pathlib.Path(tmp) / "meter_tool"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O0",
                "-Wall",
                "-Wextra",
                "-I",
                str(AUDIO_DIR),
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        yield binary


def _run(meter_tool: pathlib.Path, script: str) -> list[tuple[str, dict[str, str], str]]:
    completed = subprocess.run(
        [str(meter_tool)], input=script, text=True, capture_output=True, check=True
    )
    events = []
    for raw in completed.stdout.splitlines():
        if not raw.strip():
            continue
        head, _, rest = raw.partition(" ")
        fields = {}
        for token in rest.split():
            if "=" in token:
                key, _, value = token.partition("=")
                fields[key] = value
        events.append((head, fields, raw))
    return events


def _script(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _valid(events: list[tuple[str, dict[str, str], str]], kind: str) -> list[dict[str, str]]:
    return [fields for head, fields, _ in events if head == kind and fields]


def _only(events: list[tuple[str, dict[str, str], str]], kind: str) -> dict[str, str]:
    found = _valid(events, kind)
    assert len(found) == 1, f"expected exactly one {kind}: {found}"
    return found[0]


def _raw(events: list[tuple[str, dict[str, str], str]], kind: str) -> list[str]:
    return [raw for head, _, raw in events if head == kind]


def _counted_waits(events: list[tuple[str, dict[str, str], str]]) -> list[dict[str, str]]:
    return _valid(events, "wait")


def _token(events: list[tuple[str, dict[str, str], str]], name: str) -> dict[str, str]:
    matches = [fields for fields in _valid(events, "token") if fields.get("name") == name]
    assert len(matches) == 1, f"expected exactly one token {name}: {matches}"
    return matches[0]


def _slot(events: list[tuple[str, dict[str, str], str]], name: str) -> dict[str, str]:
    matches = [
        fields for head, fields, _ in events if head == "slot" and fields.get("name") == name
    ]
    assert len(matches) == 1, f"expected exactly one slot {name}: {matches}"
    return matches[0]


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "freertos", "lvgl", "lv_", "driver/gpio", "esp_timer"):
        assert forbidden not in source


def test_reply_open_wait_is_not_billed_as_supply(meter_tool: pathlib.Path) -> None:
    """The 1377 ms pre-start wait recorded on 2026-09-14 session-4."""
    events = _run(
        meter_tool,
        _script(
            "announce 1 10 0",
            "wait_begin 10",
            "decoded 1 10",
            "wait_end yes 1 1387",
            "freeze t0 1 10",
            "submit t0 1390",
            "close flush 1500",
        ),
    )
    assert _counted_waits(events) == []
    summary = _only(events, "summary")
    assert summary["gen"] == "1"
    assert summary["close"] == "channel_flush"
    assert summary["supply"] == "0"
    assert summary["prestart"] == "1"
    assert summary["prestart_max"] == "1377"
    assert summary["boundary"] == "0"
    assert summary["outside"] == "0"
    assert summary["close_dropped"] == "0"
    assert summary["frames"] == "1"
    assert summary["first"] == "yes"
    assert summary["latency"] == "1390"


def test_inter_generation_gap_is_not_billed_to_the_new_generation(
    meter_tool: pathlib.Path,
) -> None:
    """The recorded 5857 ms reply-to-reply gap was billed to generation 2.

    The same event stream is fed to the replaced counter's reference
    implementation, which still bills it; the new meter books it as the new
    generation's pre-start idle instead.
    """
    events = _run(
        meter_tool,
        _script(
            "announce 1 10 0",
            "decoded 1 10",
            "freeze t0 1 10",
            "submit t0 10",
            "close flush 20",
            "wait_begin 25",
            "announce 2 11 5870",
            "decoded 2 11",
            "wait_end yes 2 5882",
            "freeze t1 2 11",
            "submit t1 5885",
            "close flush 6000",
        ),
    )
    assert _counted_waits(events) == []
    summaries = _valid(events, "summary")
    assert [summary["gen"] for summary in summaries] == ["1", "2"]
    assert summaries[0]["supply"] == "0"
    assert summaries[1]["supply"] == "0"
    assert summaries[1]["prestart"] == "1"
    assert summaries[1]["prestart_max"] == "5857"
    # The replaced counter still bills the same gap to generation 2.
    legacy = _valid(events, "legacy_counted")
    assert legacy == [{"gen": "2", "gap_ms": "5857", "count": "1"}]


def test_same_generation_playout_waits_are_counted(meter_tool: pathlib.Path) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 3 10 0",
            "decoded 3 10",
            "freeze t0 3 10",
            "submit t0 10",
            "wait_begin 100",
            "wait_end yes 3 140",
            "wait_begin 200",
            "wait_end yes 3 280",
            "freeze t1 3 10",
            "submit t1 300",
            "wait_begin 400",
            "wait_end yes 3 410",
            "close flush 500",
        ),
    )
    waits = _counted_waits(events)
    assert [(wait["gen"], wait["ms"], wait["count"]) for wait in waits] == [
        ("3", "40", "1"),
        ("3", "80", "2"),
        ("3", "10", "3"),
    ]
    summary = _only(events, "summary")
    assert summary["supply"] == "3"
    assert summary["supply_max"] == "80"
    assert summary["supply_total"] == "130"
    assert summary["prestart"] == "0"
    assert summary["frames"] == "2"


def test_exact_completion_polls_are_never_supply_waits(meter_tool: pathlib.Path) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 4 10 0",
            "decoded 4 10",
            "freeze t0 4 10",
            "submit t0 10",
            "arm t0",
            "poll_begin 20",
            "poll_end no 0 25",
            "poll_begin 25",
            "poll_end no 0 30",
            "poll_begin 30",
            "poll_end no 0 35",
            "exact_ok",
            "close flush 60",
        ),
    )
    assert _counted_waits(events) == []
    summary = _only(events, "summary")
    assert summary["supply"] == "0"
    assert summary["exact_polls"] == "3"
    assert summary["exact_ok"] == "1"
    assert summary["exact_timeout"] == "0"


def test_exact_completion_timeout_is_counted_not_billed(meter_tool: pathlib.Path) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 5 10 0",
            "decoded 5 10",
            "freeze t0 5 10",
            "submit t0 10",
            "arm t0",
            "poll_begin 20",
            "poll_end no 0 25",
            "exact_timeout",
            "close flush 40",
        ),
    )
    summary = _only(events, "summary")
    assert summary["supply"] == "0"
    assert summary["exact_timeout"] == "1"
    assert summary["exact_ok"] == "0"


def test_supply_wait_after_an_exact_completion_is_still_counted(
    meter_tool: pathlib.Path,
) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 6 10 0",
            "decoded 6 10",
            "freeze t0 6 10",
            "submit t0 10",
            "arm t0",
            "poll_begin 20",
            "poll_end no 0 25",
            "exact_ok",
            "wait_begin 50",
            "wait_end yes 6 90",
            "close flush 120",
        ),
    )
    waits = _counted_waits(events)
    assert [(wait["gen"], wait["ms"]) for wait in waits] == [("6", "40")]
    summary = _only(events, "summary")
    assert summary["supply"] == "1"
    assert summary["supply_max"] == "40"
    assert summary["exact_ok"] == "1"


def test_flush_cuts_the_wait_and_reused_generation_stays_clean(
    meter_tool: pathlib.Path,
) -> None:
    """A flush followed by the same generation id must not inherit the wait."""
    events = _run(
        meter_tool,
        _script(
            "announce 7 10 0",
            "decoded 7 10",
            "freeze t0 7 10",
            "submit t0 10",
            "wait_begin 100",
            "close flush 130",
            "announce 7 11 1000",
            "decoded 7 11",
            "freeze t1 7 11",
            "wait_end yes 7 1010",
            "submit t1 1010",
            "wait_begin 1100",
            "wait_end yes 7 1140",
            "close flush 1200",
        ),
    )
    summaries = _valid(events, "summary")
    assert [summary["close"] for summary in summaries] == ["channel_flush", "channel_flush"]
    assert summaries[0]["supply"] == "0"
    assert summaries[0]["close_dropped"] == "1"
    assert summaries[0]["close_dropped_max"] == "30"
    # The re-bound remainder of that wait resolves onto the reused generation,
    # which books it as pre-start idle and never as its own supply wait.
    assert summaries[1]["supply"] == "1"
    assert summaries[1]["supply_max"] == "40"
    assert summaries[1]["prestart"] == "1"
    assert summaries[1]["prestart_max"] == "880"
    assert summaries[1]["close_dropped"] == "0"
    waits = _counted_waits(events)
    assert [(wait["gen"], wait["ms"]) for wait in waits] == [("7", "40")]


def test_episode_summarises_once_and_a_repeat_close_is_silent(
    meter_tool: pathlib.Path,
) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 8 10 0",
            "decoded 8 10",
            "freeze t0 8 10",
            "submit t0 10",
            "close switch 20",
            "close switch 30",
            "close none 40",
            "state",
        ),
    )
    summaries = _valid(events, "summary")
    assert len(summaries) == 1
    assert summaries[0]["gen"] == "8"
    assert summaries[0]["close"] == "generation_switch"
    state = _only(events, "state")
    assert state["episode"] == "0"
    assert state["gen"] == "0"
    assert state["playing"] == "0"
    assert state["waiting"] == "0"


def test_zero_wait_generation_is_still_summarised(meter_tool: pathlib.Path) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 9 10 0",
            "decoded 9 10",
            "freeze t0 9 10",
            "submit t0 10",
            "close flush 20",
            "state",
        ),
    )
    lines = [raw for raw in _raw(events, "summary_line") if raw != "summary_line none"]
    assert len(lines) == 1
    summary = _only(events, "summary")
    assert summary["gen"] == "9"
    assert summary["supply"] == "0"
    assert summary["frames"] == "1"
    assert _only(events, "state")["episode"] == "0"


def test_generation_change_without_a_flush_is_not_supply(meter_tool: pathlib.Path) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 1 10 0",
            "decoded 1 10",
            "freeze t0 1 10",
            "submit t0 10",
            "wait_begin 100",
            "wait_end yes 2 140",
            "close flush 200",
        ),
    )
    assert _counted_waits(events) == []
    summary = _only(events, "summary")
    assert summary["supply"] == "0"
    assert summary["boundary"] == "1"
    assert summary["boundary_max"] == "40"


def test_service_stop_does_not_bill_the_wait(meter_tool: pathlib.Path) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 1 10 0",
            "decoded 1 10",
            "freeze t0 1 10",
            "submit t0 10",
            "wait_begin 100",
            "close stop 150",
            "wait_end no 0 200 stopped",
        ),
    )
    assert _counted_waits(events) == []
    summary = _only(events, "summary")
    assert summary["close"] == "service_stop"
    assert summary["supply"] == "0"
    assert summary["close_dropped"] == "1"
    assert summary["close_dropped_max"] == "50"
    assert summary["outside"] == "0"


def test_wait_released_without_a_frame_is_not_supply(meter_tool: pathlib.Path) -> None:
    """A stop can release the wait before the close reaches the audio queue."""
    events = _run(
        meter_tool,
        _script(
            "announce 1 10 0",
            "decoded 1 10",
            "freeze t0 1 10",
            "submit t0 10",
            "wait_begin 100",
            "wait_end no 0 140",
            "close flush 200",
        ),
    )
    assert _counted_waits(events) == []
    summary = _only(events, "summary")
    assert summary["supply"] == "0"
    assert summary["outside"] == "1"
    assert summary["outside_max"] == "40"


def test_speaking_entry_reset_keeps_metering_through_terminal(
    meter_tool: pathlib.Path,
) -> None:
    """Real order: announce -> speaking ResetDecoder -> output -> flush."""
    events = _run(
        meter_tool,
        _script(
            "wait_begin 0",
            "announce 3 10 100",
            "decoder_reset 3 11 200",
            "wait_end yes 3 230",
            "freeze t0 3 11",
            "submit t0 235",
            "wait_begin 240",
            "wait_end yes 3 285",
            "freeze t1 3 11",
            "submit t1 290",
            "arm t1",
            "exact_ok",
            "close flush 320",
            "close flush 321",
        ),
    )
    summary = _only(events, "summary")
    assert summary["frames"] == "2"
    assert summary["close"] == "channel_flush"
    assert summary["first"] == "yes"
    assert summary["latency"] == "135"  # from announcement, not speaking entry
    assert summary["prestart_max"] == "230"
    assert summary["supply"] == "1"
    assert summary["supply_total"] == "45"
    assert summary["exact_ok"] == "1"


def test_decoder_reset_cuts_wait_and_rejects_pre_reset_output_and_exact_tokens(
    meter_tool: pathlib.Path,
) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 3 10 0",
            "freeze t0 3 10",
            "submit t0 10",
            "arm t0",
            "exact_ok",
            "wait_begin 20",
            "wait_end yes 3 60",
            "wait_begin 70",
            "decoder_reset 3 11 100",
            "slot_state t0",
            "submit t0 110",
            "arm t0",
            "exact_ok",
            "exact_timeout",
            "wait_end yes 3 130",
            "freeze t1 3 11",
            "submit t1 140",
            "arm t1",
            "exact_ok",
            "wait_begin 150",
            "wait_end yes 3 200",
            "close flush 210",
        ),
    )
    summary = _only(events, "summary")
    assert summary["frames"] == "2"
    assert summary["latency"] == "10"
    assert summary["supply"] == "2"
    assert summary["supply_total"] == "90"
    assert summary["close_dropped"] == "1"
    assert summary["close_dropped_max"] == "30"
    assert summary["prestart_max"] == "30"
    assert summary["exact_ok"] == "2"
    assert summary["exact_timeout"] == "0"
    assert _slot(events, "t0")["current"] == "0"


def test_decoder_reset_cuts_exact_poll_and_requires_fresh_output(
    meter_tool: pathlib.Path,
) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 3 10 0",
            "freeze t0 3 10",
            "submit t0 10",
            "arm t0",
            "poll_begin 20",
            "decoder_reset 3 11 22",
            "poll_end no 0 25",
            "exact_ok",
            "poll_begin 25",
            "poll_end no 0 30",
            "wait_begin 30",
            "wait_end yes 3 130",
            "freeze t1 3 11",
            "submit t1 135",
            "arm t1",
            "poll_begin 135",
            "poll_end no 0 140",
            "exact_ok",
            "close flush 150",
        ),
    )
    summary = _only(events, "summary")
    assert summary["frames"] == "2"
    assert summary["supply"] == "0"
    assert summary["prestart_max"] == "100"
    assert summary["exact_polls"] == "2"
    assert summary["exact_ok"] == "1"


@pytest.mark.parametrize("terminal", ["flush", "stop", "reset"])
def test_decoder_reset_never_reopens_after_terminal(
    meter_tool: pathlib.Path,
    terminal: str,
) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 3 10 0",
            "freeze t0 3 10",
            f"close {terminal} 10",
            "decoder_reset 3 11 20",
            "decoder_reset 0 12 30",
            "submit t0 35",
            "freeze t1 3 12",
            "submit t1 40",
            "state",
            "close flush 50",
        ),
    )
    assert len(_valid(events, "summary")) == 1
    assert _only(events, "state")["episode"] == "0"
    assert _token(events, "freeze_t1")["valid"] == "0"


@pytest.mark.parametrize("generation,fence", [(0, 11), (4, 11), (3, 10), (3, 9), (3, 12)])
def test_decoder_reset_invalid_generation_or_fence_fails_closed(
    meter_tool: pathlib.Path,
    generation: int,
    fence: int,
) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 3 10 0",
            "freeze t0 3 10",
            "submit t0 10",
            f"decoder_reset {generation} {fence} 20",
            "slot_state t0",
            "state",
            "close flush 30",
        ),
    )
    summary = _only(events, "summary")
    assert summary["close"] == "decoder_reset"
    assert summary["frames"] == "1"
    assert _only(events, "state")["episode"] == "0"
    assert _slot(events, "t0")["current"] == "0"


def test_repeated_same_generation_reset_preserves_announcement_latency_and_fence_wrap(
    meter_tool: pathlib.Path,
) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 3 4294967294 100",
            "freeze t0 3 4294967294",
            "decoder_reset 3 4294967295 200",
            "freeze t1 3 4294967295",
            "decoder_reset 3 0 300",
            "submit t0 310",
            "submit t1 320",
            "freeze t2 3 0",
            "submit t2 350",
            "close flush 400",
        ),
    )
    summary = _only(events, "summary")
    assert summary["close"] == "channel_flush"
    assert summary["frames"] == "1"
    assert summary["latency"] == "250"
    assert _token(events, "freeze_t2")["valid"] == "1"


def test_decoder_reset_close_reason_is_reported(meter_tool: pathlib.Path) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 1 10 0",
            "decoded 1 10",
            "freeze t0 1 10",
            "submit t0 10",
            "close reset 20",
        ),
    )
    summary = _only(events, "summary")
    assert summary["close"] == "decoder_reset"
    assert summary["frames"] == "1"


def test_local_cue_never_opens_an_episode_or_bills_a_wait(meter_tool: pathlib.Path) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 1 10 0",
            "decoded 1 10",
            "freeze t0 1 10",
            "submit t0 10",
            "decoded 0 10",
            "freeze t1 0 10",
            "submit t1 25",
            "wait_begin 30",
            "wait_end yes 0 40",
            "close flush 100",
        ),
    )
    summaries = _valid(events, "summary")
    assert len(summaries) == 1
    assert summaries[0]["gen"] == "1"
    assert summaries[0]["frames"] == "1"
    assert summaries[0]["supply"] == "0"
    assert summaries[0]["boundary"] == "1"
    assert _counted_waits(events) == []
    # A local cue has no episode token, so nothing can be attached to it.
    assert _token(events, "freeze_t1")["valid"] == "0"


def test_decode_dequeue_only_observes_and_never_opens_an_episode(
    meter_tool: pathlib.Path,
) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 2 10 0",
            "decoded 2 10",
            "close flush 10",
            "decoded 2 10",
            "state",
        ),
    )
    tokens = _valid(events, "token")
    assert [token["name"] for token in tokens] == ["dequeue", "dequeue"]
    assert tokens[0]["valid"] == "1"
    assert tokens[0]["gen"] == "2"
    # The second observation happens after the flush, so it is stale and must
    # not have built a new episode or a fence of its own.
    assert tokens[1]["valid"] == "0"
    assert _only(events, "state")["episode"] == "0"


def test_summary_and_wait_lines_report_the_agreed_schema(meter_tool: pathlib.Path) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 2 10 0",
            "decoded 2 10",
            "freeze t0 2 10",
            "submit t0 10",
            "wait_begin 20",
            "wait_end yes 2 100",
            "close flush 200",
        ),
    )
    wait_lines = _raw(events, "wait_line")
    assert len(wait_lines) == 1
    wait_body = wait_lines[0].partition(" ")[2]
    assert wait_body.startswith("media playback supply wait ")
    assert [token.partition("=")[0] for token in wait_body.split() if "=" in token] == [
        "layer",
        "scope",
        "generation",
        "wait_ms",
        "supply_waits",
    ]
    assert "layer=playout_queue" in wait_body
    assert "scope=software_queue_wait" in wait_body
    for forbidden in ("underrun", "starve", "audible", "dac", "starvation"):
        assert forbidden not in wait_body.lower()

    summary_lines = [raw for raw in _raw(events, "summary_line") if raw != "summary_line none"]
    assert len(summary_lines) == 1
    summary_body = summary_lines[0].partition(" ")[2]
    assert summary_body.startswith("media playback supply summary ")
    assert [
        token.partition("=")[0] for token in summary_body.split() if "=" in token
    ] == SUMMARY_KEYS
    assert "generation=2" in summary_body
    assert "close=channel_flush" in summary_body
    assert "supply_waits=1" in summary_body
    for forbidden in ("underrun", "starve", "audible", "dac", "starvation"):
        assert forbidden not in summary_body.lower()


def test_reset_forgets_an_in_flight_wait(meter_tool: pathlib.Path) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 3 10 0",
            "decoded 3 10",
            "freeze t0 3 10",
            "submit t0 10",
            "wait_begin 20",
            "reset",
            "wait_end yes 3 200",
            "state",
        ),
    )
    assert _counted_waits(events) == []
    assert _valid(events, "summary") == []
    state = _only(events, "state")
    assert state["episode"] == "0"
    assert state["gen"] == "0"
    assert state["playing"] == "0"
    assert state["waiting"] == "0"


def test_late_submission_after_a_flush_does_not_revive_the_old_episode(
    meter_tool: pathlib.Path,
) -> None:
    """AudioOutputTask submits outside the queue lock.

    The consumer can freeze the token of generation 1, a flush can then end that
    episode and a generation switch open generation 2, and only then can the
    codec submission of generation 1 return.  That late submission must be
    dropped as an observation: it may not close generation 2, may not reopen
    generation 1 and may not emit a second summary for either.
    """
    events = _run(
        meter_tool,
        _script(
            "announce 1 10 0",
            "decoded 1 10",
            "freeze t0 1 10",
            "submit t0 10",
            "close flush 20",
            "announce 2 11 30",
            "submit t0 40",
            "legacy_state",
            "state",
            "slot_state t0",
            "freeze t1 2 11",
            "submit t1 50",
            "state",
            "close flush 60",
        ),
    )
    summaries = _valid(events, "summary")
    assert [summary["gen"] for summary in summaries] == ["1", "2"]
    assert summaries[0]["frames"] == "1"
    assert summaries[1]["frames"] == "1"
    # The replaced rule bills the late submission to generation 1 again and
    # summarises generation 2 early; the new meter does neither.
    replaced = _only(events, "legacy_episode")
    assert replaced["gen"] == "1"
    assert replaced["frames"] == "1"
    assert replaced["summaries"] == "2"
    # The late submission neither played nor ended generation 2.
    after_late = _valid(events, "state")[0]
    assert after_late["episode"] == "1"
    assert after_late["gen"] == "2"
    assert after_late["playing"] == "0"
    # The frozen generation-1 token no longer names the open episode, and the
    # late submission did not create one that it could name.
    assert _slot(events, "t0")["current"] == "0"
    assert _token(events, "freeze_t1")["valid"] == "1"
    assert after_late["id"] == _token(events, "freeze_t1")["id"]
    after_new = _valid(events, "state")[1]
    assert after_new["playing"] == "1"


def test_late_submission_of_a_reused_generation_id_lands_nowhere(
    meter_tool: pathlib.Path,
) -> None:
    """A flush can be followed by the same generation id, so the generation id
    alone cannot identify the episode a submission belongs to."""
    events = _run(
        meter_tool,
        _script(
            "announce 7 10 0",
            "decoded 7 10",
            "freeze t0 7 10",
            "submit t0 10",
            "close flush 20",
            "announce 7 11 1000",
            "freeze t1 7 11",
            "submit t0 1010",
            "legacy_state",
            "state",
            "submit t1 1020",
            "close flush 1100",
        ),
    )
    summaries = _valid(events, "summary")
    assert [summary["gen"] for summary in summaries] == ["7", "7"]
    assert summaries[0]["frames"] == "1"
    # The late submission carries the same generation id but the previous
    # episode id, so it is dropped and the new episode still has one frame.
    assert summaries[1]["frames"] == "1"
    # Same generation id is not enough for the replaced rule either: it cannot
    # tell the two episodes apart and keeps counting into a stale one.
    assert _only(events, "legacy_episode")["summaries"] == "1"
    after_late = _only(events, "state")
    assert after_late["playing"] == "0"
    assert after_late["id"] == _token(events, "freeze_t1")["id"]
    assert _token(events, "freeze_t0")["id"] != _token(events, "freeze_t1")["id"]


def test_late_submission_after_stop_never_reopens_an_episode(
    meter_tool: pathlib.Path,
) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 3 10 0",
            "decoded 3 10",
            "freeze t0 3 10",
            "submit t0 5",
            "close stop 10",
            "submit t0 20",
            "legacy_state",
            "state",
            "slot_state t0",
        ),
    )
    summaries = _valid(events, "summary")
    assert len(summaries) == 1
    assert summaries[0]["close"] == "service_stop"
    # The replaced rule reopens an episode for the stopped generation.
    replaced = _only(events, "legacy_episode")
    assert replaced["open"] == "1"
    assert replaced["gen"] == "3"
    assert replaced["frames"] == "1"
    state = _only(events, "state")
    assert state["episode"] == "0"
    assert state["gen"] == "0"
    assert state["playing"] == "0"
    assert _slot(events, "t0")["current"] == "0"


def test_late_exact_completion_is_not_credited_to_the_new_episode(
    meter_tool: pathlib.Path,
) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 4 10 0",
            "decoded 4 10",
            "freeze t0 4 10",
            "submit t0 5",
            "arm t0",
            "close flush 10",
            "announce 5 11 20",
            "exact_ok",
            "exact_timeout",
            "freeze t1 5 11",
            "submit t1 30",
            "arm t1",
            "exact_ok",
            "close flush 40",
        ),
    )
    summaries = _valid(events, "summary")
    assert [summary["gen"] for summary in summaries] == ["4", "5"]
    assert summaries[0]["exact_ok"] == "0"
    # Generation 4 was closed before its watermark completed, so its completion
    # is not credited to generation 5 either.
    assert summaries[1]["exact_ok"] == "1"
    assert summaries[1]["exact_timeout"] == "0"


def test_late_exact_poll_is_not_counted_against_the_new_episode(
    meter_tool: pathlib.Path,
) -> None:
    events = _run(
        meter_tool,
        _script(
            "announce 6 10 0",
            "decoded 6 10",
            "freeze t0 6 10",
            "submit t0 5",
            "arm t0",
            "poll_begin 6",
            "close flush 7",
            "announce 7 11 10",
            "poll_end no 0 15",
            "close flush 20",
        ),
    )
    summaries = _valid(events, "summary")
    assert [summary["gen"] for summary in summaries] == ["6", "7"]
    assert summaries[0]["exact_polls"] == "1"
    assert summaries[1]["exact_polls"] == "0"
    assert summaries[1]["supply"] == "0"


def test_exact_completion_follows_the_episode_not_the_generation_id(
    meter_tool: pathlib.Path,
) -> None:
    """Three episodes share generation 7; only the armed one may be credited."""
    events = _run(
        meter_tool,
        _script(
            "announce 7 10 0",
            "decoded 7 10",
            "freeze t0 7 10",
            "submit t0 5",
            "arm t0",
            "close flush 7",
            "announce 7 11 10",
            "exact_ok",
            "close flush 20",
            "announce 7 12 30",
            "decoded 7 12",
            "freeze t1 7 12",
            "submit t1 40",
            "arm t1",
            "exact_ok",
            "close flush 60",
        ),
    )
    summaries = _valid(events, "summary")
    assert [summary["gen"] for summary in summaries] == ["7", "7", "7"]
    # The first episode closed before its watermark completed, and its stale arm
    # may not be credited to the second episode that shares its generation id.
    assert [summary["exact_ok"] for summary in summaries] == ["0", "0", "1"]
    assert _token(events, "freeze_t0")["id"] != _token(events, "freeze_t1")["id"]


def test_summary_capacity_covers_the_widest_fields(meter_tool: pathlib.Path) -> None:
    """A summary must not be dropped because its fields grew.

    The previous 512-byte buffer silently discarded the summary of the busiest
    and of the final generation, which is exactly the generation the report is
    read for.
    """
    events = _run(meter_tool, _script("wide"))
    wide = _only(events, "wide")
    assert wide["ok"] == "1"
    assert wide["narrow_ok"] == "0"
    assert int(wide["len"]) > 512
    assert int(wide["len"]) < int(wide["capacity"])
