"""Host-side checks for the audio level meter behind the mascot's mouth and glow (TODOLIST M-1).

`memoria_audio_level.h` has no ESP-IDF dependency, so the dB scale, the PCM measurement and the lock-free
tap compile on the host. The audio service patch that publishes into the taps only builds inside ESP-IDF
and is checked as patch text.

Why it exists: the mouth used to flap on a random timer whenever the device state said "speaking", so a
stalled or lost reply still looked like talking (round 18, p03: 30.8 s in speaking). The audio tasks now
measure the PCM they actually move.
"""

from __future__ import annotations

import math
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
HEADER = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria" / "memoria_audio_level.h"
PATCH = FIRMWARE_ROOT / "overlay" / "patches" / "0034-memoria-audio-level.patch"

HARNESS = r"""
#include "memoria_audio_level.h"

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace {

void PrintReading(const memoria::AudioLevelTap::Reading& r) {
    printf("%u %u %u\n", static_cast<unsigned>(r.peak), static_cast<unsigned>(r.valley),
           static_cast<unsigned>(r.blocks));
}

}  // namespace

int main() {
    char line[8192];
    memoria::AudioLevelTap tap;
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
        std::istringstream in(line);
        std::string cmd;
        in >> cmd;
        if (cmd == "level") {
            std::string token;
            in >> token;
            const double rms = strtod(token.c_str(), nullptr);  // accepts "nan", which operator>> does not
            printf("%u\n", static_cast<unsigned>(memoria::RmsToLevel(static_cast<float>(rms))));
        } else if (cmd == "pcm") {
            // pcm <kind> <count> <amplitude>
            std::string kind;
            int count = 0;
            int amp = 0;
            in >> kind >> count >> amp;
            std::vector<int16_t> pcm(static_cast<size_t>(count));
            for (int i = 0; i < count; ++i) {
                if (kind == "zero") pcm[i] = 0;
                else if (kind == "const") pcm[i] = static_cast<int16_t>(amp);
                else if (kind == "alt") pcm[i] = static_cast<int16_t>(i % 2 == 0 ? amp : -amp);
                else if (kind == "sine") pcm[i] = static_cast<int16_t>(amp * std::sin(2.0 * 3.14159265358979 * i / 40.0));
                else return 2;
            }
            printf("%.6f %u\n", memoria::PcmRms(pcm.data(), pcm.size()),
                   static_cast<unsigned>(memoria::PcmLevel(pcm.data(), pcm.size())));
        } else if (cmd == "chunks") {
            // chunks <sample rate> <count> <amplitude of the first half> <amplitude of the second half>
            int rate = 0;
            int count = 0;
            int first = 0;
            int second = 0;
            in >> rate >> count >> first >> second;
            std::vector<int16_t> pcm(static_cast<size_t>(count));
            for (int i = 0; i < count; ++i) pcm[i] = static_cast<int16_t>((i < count / 2 ? first : second) * (i % 2 == 0 ? 1 : -1));
            memoria::AudioLevelTap fresh;
            memoria::PublishPcm(fresh, pcm.data(), pcm.size(), rate);
            PrintReading(fresh.Take());
        } else if (cmd == "null") {
            printf("%.6f %u\n", memoria::PcmRms(nullptr, 10), static_cast<unsigned>(memoria::PcmLevel(nullptr, 10)));
        } else if (cmd == "publish") {
            int level;
            while (in >> level) tap.Publish(static_cast<uint8_t>(level));
        } else if (cmd == "take") {
            PrintReading(tap.Take());
        } else if (cmd == "flood") {
            unsigned long count = 0;
            int level = 0;
            in >> count >> level;
            for (unsigned long i = 0; i < count; ++i) tap.Publish(static_cast<uint8_t>(level));
        } else if (cmd == "threads") {
            // One audio task publishing a known sequence, the display task taking all the while: nothing
            // may be lost or double counted, and every reading must be self-consistent.
            unsigned long total = 0;
            in >> total;
            memoria::AudioLevelTap shared;
            std::atomic<bool> done{false};
            unsigned long seen = 0;
            unsigned bad = 0;
            unsigned highest = 0;
            unsigned lowest = 255;
            std::thread reader([&] {
                for (;;) {
                    const bool last = done.load();
                    const memoria::AudioLevelTap::Reading r = shared.Take();
                    seen += r.blocks;
                    if (r.blocks != 0) {
                        if (r.valley > r.peak) ++bad;
                        if (r.peak > highest) highest = r.peak;
                        if (r.valley < lowest) lowest = r.valley;
                    } else if (r.peak != 0 || r.valley != 0) {
                        ++bad;
                    }
                    if (last) break;
                }
            });
            for (unsigned long i = 0; i < total; ++i) {
                shared.Publish(static_cast<uint8_t>(20 + (i * 7) % 200));  // 20..219
            }
            done.store(true);
            reader.join();
            printf("%lu %u %u %u\n", seen, bad, highest, lowest);
        } else {
            return 2;
        }
        fflush(stdout);
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def tool():
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "audio_level_tool"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O1",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-pthread",
                "-fsanitize=undefined",
                "-fno-sanitize-recover=undefined",
                "-I",
                str(HEADER.parent),
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        yield binary


def _run(tool: pathlib.Path, *lines: str) -> list[str]:
    completed = subprocess.run(
        [str(tool)], input="\n".join(lines) + "\n", text=True, capture_output=True, check=True
    )
    return completed.stdout.splitlines()


def _dbfs(db: float) -> float:
    return 10 ** (db / 20)


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "lv_", "freertos", "nvs", "settings.h", "driver/"):
        assert forbidden not in source
    assert re.findall(r'#include "([^"]+)"', source) == []


def test_silence_and_the_floor_read_as_zero_and_the_ceiling_as_full(tool: pathlib.Path) -> None:
    got = _run(
        tool,
        "level 0",
        "level -0.5",
        "level nan",
        f"level {_dbfs(-54)}",
        f"level {_dbfs(-60)}",
        f"level {_dbfs(-12)}",
        f"level {_dbfs(-6)}",
        "level 1.0",
    )
    assert got == ["0", "0", "0", "0", "0", "255", "255", "255"]


def test_the_scale_is_linear_in_db_between_floor_and_ceiling(tool: pathlib.Path) -> None:
    probes = [-50, -45, -40, -33, -26, -20, -16, -13]
    got = [int(x) for x in _run(tool, *(f"level {_dbfs(db)}" for db in probes))]
    assert got == sorted(got)
    for db, level in zip(probes, got, strict=True):
        assert level == pytest.approx((db + 54) / 42 * 255, abs=1.0)
    # What the mouth is tuned on: TTS speech sits near -26..-16 dBFS (170..231), the gaps at -45 or less (55 or less).
    assert 168 <= got[probes.index(-26)] <= 172
    assert got[probes.index(-45)] <= 55


def test_every_step_of_a_sweep_is_monotonic(tool: pathlib.Path) -> None:
    rms = [10 ** (db / 20 / 10) for db in range(-700, 20)]
    got = [int(x) for x in _run(tool, *(f"level {r}" for r in rms))]
    assert got == sorted(got)
    assert got[0] == 0 and got[-1] == 255


def test_pcm_rms_matches_the_definition_for_simple_waveforms(tool: pathlib.Path) -> None:
    zero, const, alt, sine = _run(tool, "pcm zero 320 0", "pcm const 320 8192", "pcm alt 320 16384", "pcm sine 320 20000")
    assert zero == "0.000000 0"
    assert float(const.split()[0]) == pytest.approx(0.25, abs=1e-5)
    assert float(alt.split()[0]) == pytest.approx(0.5, abs=1e-5)
    assert float(sine.split()[0]) == pytest.approx(20000 / 32768 / math.sqrt(2), abs=2e-3)


def test_pcm_rms_survives_the_most_negative_sample_and_handles_nothing_to_measure(tool: pathlib.Path) -> None:
    loudest, nothing = _run(tool, "pcm const 320 -32768", "null")
    assert float(loudest.split()[0]) == pytest.approx(1.0, abs=1e-6)
    assert loudest.split()[1] == "255"
    assert nothing == "0.000000 0"


def test_a_playback_frame_is_one_block_and_a_longer_cue_is_split_into_20_ms_blocks(tool: pathlib.Path) -> None:
    # 24 kHz: a 20 ms frame is 480 samples. One frame, one block; a 60 ms cue frame, three; a cut-off tail counts.
    one, three, tail = _run(tool, "chunks 24000 480 8000 8000", "chunks 24000 1440 1000 16000", "chunks 24000 500 8000 8000")
    assert one.split()[2] == "1"
    peak, valley, blocks = (int(x) for x in three.split())
    assert blocks == 3 and peak > valley + 60
    assert tail.split()[2] == "2"


def test_nothing_to_publish_publishes_nothing(tool: pathlib.Path) -> None:
    assert _run(tool, "chunks 24000 0 100 100", "chunks 0 480 100 100", "chunks -1 480 100 100") == ["0 0 0"] * 3


def test_a_tap_that_nobody_published_to_reads_as_no_audio(tool: pathlib.Path) -> None:
    assert _run(tool, "take") == ["0 0 0"]


def test_a_take_reports_the_loudest_the_quietest_and_the_count_then_starts_over(tool: pathlib.Path) -> None:
    assert _run(tool, "publish 10 200 90", "take", "take", "publish 120", "take") == ["200 10 3", "0 0 0", "120 120 1"]


def test_levels_at_the_ends_of_the_scale_are_not_mistaken_for_an_empty_tap(tool: pathlib.Path) -> None:
    got = _run(tool, "publish 0", "take", "publish 255", "take", "publish 0 255", "take", "publish 255 0", "take")
    assert got == ["0 0 1", "255 255 1", "255 0 2", "255 0 2"]


def test_the_block_count_saturates_instead_of_wrapping_into_the_levels(tool: pathlib.Path) -> None:
    assert _run(tool, "flood 70000 100", "take", "publish 40 160", "take") == ["100 100 65535", "160 40 2"]


def test_the_audio_task_and_the_display_task_never_lose_or_invent_a_block(tool: pathlib.Path) -> None:
    seen, bad, highest, lowest = _run(tool, "threads 40000")[0].split()
    assert int(seen) == 40000
    assert int(bad) == 0
    assert (int(highest), int(lowest)) == (219, 20)


def test_the_audio_service_patch_publishes_both_sides() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    added = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    code = "\n".join(added)
    assert '#include "memoria_audio_level.h"' in code
    # The speaker side measures what is handed to the codec; the microphone side what is sent upstream.
    assert "PublishPcm(memoria::output_level_tap" in code
    assert "memoria::input_level_tap.Publish(" in code
    # Both publishing sites are Memoria-board only (the include is harmless everywhere: no dependencies).
    assert code.count("#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT") == 2
    assert [line for line in added if "OutputData" in line] == []
    order = patch.index("PublishPcm(memoria::output_level_tap")
    assert patch.index("codec_->OutputData(task->pcm);", order) > order
