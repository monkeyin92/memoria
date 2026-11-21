"""Host checks for the device vad.start RMS floor and its peak window.

False starts measured in the field were 0.0005 to 0.0013. Real test speech
was 0.0067, but the AFE reports the start edge on its first speaking frame,
which for weak speech is 0.0007 to 0.0019; the frames that follow the same
utterance reach 0.0031 to 0.0088. A start below the floor therefore holds the
loudest frame for 250 ms and decides on that peak. The default floor is 3
thousandths, an rms of 0.003.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

MEMORIA_DIR = pathlib.Path(__file__).parents[1] / "overlay" / "files" / "main" / "memoria"
AUDIO_LEVEL_PATCH = (
    pathlib.Path(__file__).parents[1] / "overlay" / "patches" / "0034-memoria-audio-level.patch"
)

HARNESS = r"""
#include "memoria_vad_start.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

const char* Name(memoria::VadEdgeAction action) {
    switch (action) {
    case memoria::VadEdgeAction::kIgnore:
        return "ignore";
    case memoria::VadEdgeAction::kSuppressStart:
        return "suppress";
    case memoria::VadEdgeAction::kAwaitPeak:
        return "await";
    case memoria::VadEdgeAction::kReport:
        return "report";
    }
    return "invalid";
}

float ParseRms(const char* text, float floor) {
    if (std::strcmp(text, "floor") == 0) {
        return floor;
    }
    return std::strtof(text, nullptr);
}

}  // namespace

int main() {
    memoria::VadStartGate gate;
    memoria::VadNoiseFloor noise_floor;
    memoria::PlaybackVadHandoff handoff;
    uint64_t sim_now_ms = 0;
    char line[256];
    while (std::fgets(line, sizeof(line), stdin) != nullptr) {
        line[std::strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        }
        if (std::strncmp(line, "complete ", 9) == 0) {
            handoff.Completed(std::atoi(line + 9) != 0);
            std::puts("complete");
        } else if (std::strncmp(line, "listen ", 7) == 0 || std::strncmp(line, "arming ", 7) == 0) {
            unsigned epoch;
            unsigned long long now;
            if (std::sscanf(line + 7, "%u %llu", &epoch, &now) != 2) return 2;
            const bool armed = line[0] == 'l' ? handoff.Listening(epoch, now) : handoff.TryArm(epoch, now);
            std::puts(armed ? "armed" : "idle");
        } else if (std::strncmp(line, "packet ", 7) == 0) {
            int eligible;
            unsigned long long produced, now;
            float rms;
            if (std::sscanf(line + 7, "%d %llu %llu %f", &eligible, &produced, &now, &rms) != 4) return 2;
            const float floor = noise_floor.EffectiveFloor(.003f, now);
            std::puts(handoff.Frame(eligible != 0, produced, now, rms, floor) ? "report" : "ignore");
        } else if (std::strncmp(line, "sim_frame ", 10) == 0) {
            float rms = 0.0f;
            int milli = 0;
            unsigned long long now = 0;
            if (std::sscanf(line + 10, "%f %d %llu", &rms, &milli, &now) != 3) return 2;
            const float floor = noise_floor.EffectiveFloor(memoria::VadStartRmsFloorFromMilli(milli), now);
            const memoria::VadEdgeAction action = gate.Frame(rms, floor, now, true);
            if (action != memoria::VadEdgeAction::kReport && !gate.awaiting() && !gate.recovering()) {
                noise_floor.Observe(rms, now);
            }
            std::printf("%s\n", Name(action));
        } else if (std::strncmp(line, "floor ", 6) == 0) {
            const int milli = std::atoi(line + 6);
            std::printf("%.9f\n", static_cast<double>(memoria::VadStartRmsFloorFromMilli(milli)));
        } else if (std::strncmp(line, "edge ", 5) == 0) {
            int epoch = 0;
            int speaking = 0;
            int milli = 0;
            unsigned long long now = 0;
            char rms_text[64] = {};
            if (std::sscanf(line + 5, "%d %d %63s %d %llu", &epoch, &speaking, rms_text, &milli,
                            &now) != 5) {
                return 2;
            }
            const float floor = memoria::VadStartRmsFloorFromMilli(milli);
            const memoria::VadEdgeAction action = gate.Edge(
                epoch != 0, speaking != 0, ParseRms(rms_text, floor), floor, now);
            std::printf("%s\n", Name(action));
        } else if (std::strncmp(line, "frame ", 6) == 0) {
            int milli = 0;
            unsigned long long now = 0;
            char rms_text[64] = {};
            if (std::sscanf(line + 6, "%63s %d %llu", rms_text, &milli, &now) != 3) {
                return 2;
            }
            const float floor = memoria::VadStartRmsFloorFromMilli(milli);
            const bool eligible = std::strstr(line, " cancelled") == nullptr;
            std::printf("%s\n", Name(gate.Frame(ParseRms(rms_text, floor), floor, now, eligible)));
        } else if (std::strcmp(line, "peak") == 0) {
            std::printf("%.4f\n", static_cast<double>(gate.peak()));
        } else if (std::strncmp(line, "observe ", 8) == 0) {
            float rms = 0.0f;
            int count = 1;
            const int parsed = std::sscanf(line + 8, "%f %d", &rms, &count);
            if (parsed < 1) return 2;
            for (int i = 0; i < count; ++i) {
                sim_now_ms += 20;
                noise_floor.Observe(rms, sim_now_ms);
            }
            std::printf("%.4f\n", static_cast<double>(noise_floor.ambient()));
        } else if (std::strncmp(line, "advance ", 8) == 0) {
            unsigned long long delta = 0;
            if (std::sscanf(line + 8, "%llu", &delta) != 1) return 2;
            sim_now_ms += delta;
            std::printf("%llu\n", static_cast<unsigned long long>(sim_now_ms));
        } else if (std::strncmp(line, "keepalive ", 10) == 0) {
            int count = 1;
            if (std::sscanf(line + 10, "%d", &count) != 1) return 2;
            for (int i = 0; i < count; ++i) {
                sim_now_ms += 20;
                noise_floor.KeepAlive(sim_now_ms);
            }
            std::printf("%llu\n", static_cast<unsigned long long>(sim_now_ms));
        } else if (std::strncmp(line, "eff_floor ", 10) == 0) {
            const int milli = std::atoi(line + 10);
            const float base = memoria::VadStartRmsFloorFromMilli(milli);
            std::printf("%.4f\n", static_cast<double>(noise_floor.EffectiveFloor(base, sim_now_ms)));
        } else if (std::strcmp(line, "noise_reset") == 0) {
            noise_floor.Reset();
            sim_now_ms = 0;
            std::puts("noise_reset");
        } else if (std::strcmp(line, "reset") == 0) {
            gate.Reset();
            std::puts("reset");
        } else {
            return 2;
        }
        std::fflush(stdout);
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
        binary = pathlib.Path(tmp) / "vad_start_tool"
        completed = subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(MEMORIA_DIR),
                str(source),
                "-o",
                str(binary),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.fail(completed.stderr or completed.stdout)
        yield binary


def _run(binary: pathlib.Path, *lines: str) -> list[str]:
    completed = subprocess.run(
        [str(binary)],
        input="\n".join(lines) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.splitlines()


def test_floor_milli_is_thousandths_of_full_scale(tool: pathlib.Path) -> None:
    assert abs(float(_run(tool, "floor 3")[0]) - 0.003) < 1e-6
    assert abs(float(_run(tool, "floor 0")[0]) - 0.0) < 1e-6


def test_a_weak_first_frame_is_held_until_the_speech_peak(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "edge 0 1 0.0007 3 0",
        "frame 0.0009 3 20",
        "frame 0.0050 3 60",
        "peak",
    ) == ["await", "await", "report", "0.0050"]


def test_the_window_suppresses_speech_that_never_reaches_the_floor(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "edge 0 1 0.0007 3 0",
        "frame 0.0013 3 100",
        "frame 0.0019 3 240",
        "frame 0.0008 3 260",
    ) == ["await", "await", "await", "suppress"]


def test_an_end_edge_inside_the_window_suppresses_the_start(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 0.0007 3 0", "edge 0 0 0.0005 3 40") == ["await", "suppress"]


def test_t008_continuous_speech_recovers_once_after_the_early_peak_window(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "edge 0 1 0.00128 3 0", "frame 0.0019 3 200", "frame 0.0010 3 260",
        "frame 0.0042 3 500", "frame 0.0011 3 520",
        "frame 0.0031 3 3200", "frame 0.0055 3 3220", "frame 0.0055 3 3240",
        "edge 1 0 0.0003 3 4000",
    ) == ["await", "await", "suppress", "ignore", "ignore", "ignore", "report", "ignore", "report"]


@pytest.mark.parametrize("cancel,expected", [("edge 0 0 0.0002 3 280", "ignore"), ("reset", "reset")])
def test_ended_candidate_cannot_be_revived_by_later_energy(tool: pathlib.Path, cancel: str, expected: str) -> None:
    assert _run(
        tool, "edge 0 1 0.001 3 0", "frame 0.001 3 260", "frame 0.005 3 275",
        cancel, "frame 0.006 3 300", "frame 0.006 3 320",
    ) == ["await", "suppress", "ignore", expected, "ignore", "ignore"]


def test_a_first_frame_at_the_floor_reports_without_waiting(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 floor 3 0") == ["report"]
    assert _run(tool, "edge 0 1 0.0067 3 0") == ["report"]


def test_t013_held_playback_edge_needs_fresh_quiet_then_two_loud_packets(tool: pathlib.Path) -> None:
    assert _run(tool, "complete 1", "listen 13 1000", "packet 1 980 1300 0.001",
                "packet 1 1250 1260 0.008", "packet 1 1270 1280 0.006",
                "packet 1 1500 1510 0.001", "packet 1 1870 2200 0.008",
                "packet 1 5790 5800 0.006", "packet 1 5810 5820 0.008",
                "packet 1 5830 5840 0.008") == ["complete", "armed", *["ignore"] * 6, "report", "ignore"]


@pytest.mark.parametrize("barrier", ["complete 0", "packet 0 1500 1500 0.006",
                                      "packet 1 7000 7000 0.006"])
def test_closed_changed_afe_or_expired_handoff_cannot_reopen(tool: pathlib.Path, barrier: str) -> None:
    after = 7020 if "7000" in barrier else 1520
    result = _run(tool, "complete 1", "listen 7 1000", "packet 1 1300 1300 0.001", barrier,
                  f"packet 1 {after} {after} 0.008", f"packet 1 {after+20} {after+20} 0.008")
    assert "report" not in result


@pytest.mark.parametrize("complete,current", [(0, 3), (1, 4), (1, 0)])
def test_only_normal_end_with_live_playback_speech_arms(tool: pathlib.Path, complete: int,
                                                       current: int) -> None:
    assert _run(tool, f"complete {complete}", f"listen {current} 900") == ["complete", "idle"]


@pytest.mark.parametrize("published,expected", [(1020, "armed"), (1260, "idle")])
def test_late_afe_publication_cannot_extend_playback_handoff(tool: pathlib.Path, published: int, expected: str) -> None:
    assert _run(tool, "complete 1", "listen 4 1000", f"arming 7 {published}") == ["complete", "idle", expected]


@pytest.mark.parametrize(
    "frames",
    [
        ("frame 0.001 3 260 cancelled", "frame 0.004 3 280", "frame 0.005 3 300"),
        ("frame 0.001 3 260", "frame 0.004 3 280 cancelled", "frame 0.005 3 300"),
        ("frame 0.001 3 260", "frame 0.004 3 4990", "frame 0.005 3 5010"),
    ],
)
def test_ineligible_or_expired_candidate_stays_suppressed(tool: pathlib.Path, frames: tuple[str, ...]) -> None:
    assert _run(tool, "edge 0 1 0.001 3 0", *frames) == ["await", "suppress", "ignore", "ignore"]


def test_a_second_start_while_the_epoch_is_open_is_ignored(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 1 1 0.0067 3 0") == ["ignore"]


def test_a_quiet_end_is_reported(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 1 0 0.0005 3 0") == ["report"]


def test_milli_zero_reports_the_false_start_band(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 0.0013 0 0") == ["report"]


def test_a_higher_floor_suppresses_the_measured_speech(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 0.0067 7 0", "frame 0.0068 7 260") == ["await", "suppress"]


def test_the_peak_is_cleared_between_starts(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "edge 0 1 0.0007 3 0",
        "edge 0 0 0.0005 3 40",
        "edge 0 1 0.0007 3 1000",
        "frame 0.0009 3 1010",
    ) == ["await", "suppress", "await", "await"]


def test_the_audio_task_publishes_its_measured_rms_for_the_window() -> None:
    added = "\n".join(
        line[1:]
        for line in AUDIO_LEVEL_PATCH.read_text(encoding="utf-8").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    assert '#include "memoria_vad_start.h"' in added
    assert "memoria::uplink_frame_rms.store(" in added


def test_adaptive_noise_floor_keeps_base_floor_in_quiet_room(tool: pathlib.Path) -> None:
    """Field 2026-11-21 soak-1534 (-55 dBFS quiet room): idle rms ~0.0002..0.0007 keeps 0.0030."""

    out = _run(
        tool,
        "noise_reset",
        "observe 0.0002 200",
        "eff_floor 3",
        "observe 0.0007 200",
        "eff_floor 3",
        "observe 0.0500 5",  # brief 100 ms transient / clap must not lift floor
        "eff_floor 3",
    )
    assert out[2] == "0.0030"
    assert out[4] == "0.0030"
    assert out[6] == "0.0030"


def test_adaptive_noise_floor_suppresses_steady_minus_40dbfs_room_noise_and_recovers(
    tool: pathlib.Path,
) -> None:
    """Field 2026-11-21 soak-1244 (-40 dBFS noise): idle rms ~0.0018 lifts floor past 0.0053."""

    out = _run(
        tool,
        "noise_reset",
        "observe 0.0018 300",  # ~6 s of steady -40 dBFS room noise
        "eff_floor 3",
        "observe 0.0002 60",  # ~1.2 s after noise turns off -> returns to base floor
        "eff_floor 3",
    )
    noisy_floor = float(out[2])
    recovered_floor = float(out[4])
    assert 0.0053 < noisy_floor <= 0.0100
    assert recovered_floor == pytest.approx(0.0030)


def test_adaptive_noise_floor_respects_zero_disable_and_max_cap(tool: pathlib.Path) -> None:
    out = _run(
        tool,
        "noise_reset",
        "observe 0.0500 1000",  # extreme sustained noise capped at 0.0100
        "eff_floor 3",
        "eff_floor 0",  # milli=0 still disables gate
        "eff_floor 12",  # custom floor higher than cap stays intact
    )
    assert out[2] == "0.0100"
    assert out[3] == "0.0000"
    assert out[4] == "0.0120"


def test_weak_speech_recovery_freezes_noise_floor_until_candidate_resolves(
    tool: pathlib.Path,
) -> None:
    """Soft speech during recovering() must not raise the floor against its own recovery."""

    lines = [
        "reset",
        "noise_reset",
        "edge 0 1 0.0012 3 0",
        "sim_frame 0.0019 3 200",
        "sim_frame 0.0010 3 260",  # suppresses -> enters recovering()
    ]
    # 60 frames (1.2 s) of soft 0.0028 speech during recovery must NOT lift the 0.0030 floor.
    for i in range(60):
        lines.append(f"sim_frame 0.0028 3 {280 + i * 20}")
    lines.extend([
        "eff_floor 3",
        "sim_frame 0.0033 3 1500",
        "sim_frame 0.0033 3 1520",
    ])
    out = _run(tool, *lines)
    assert out[2:5] == ["await", "await", "suppress"]
    assert out[5:65] == ["ignore"] * 60
    assert out[65] == "0.0030"
    assert out[66:68] == ["ignore", "report"]


def test_first_handoff_after_short_noisy_warmup_suppresses_noise_and_allows_speech(
    tool: pathlib.Path,
) -> None:
    """Field 2026-11-21 soak-1244: 25 idle frames (0.5 s) at 0.0018 must suppress 0.0033 handoff."""

    out = _run(
        tool,
        "noise_reset",
        "observe 0.0018 25",
        "eff_floor 3",
        "complete 1",
        "listen 13 1000",
        "packet 1 1300 1310 0.0018",  # quiet noise frame after 250 ms echo margin
        "packet 1 1320 1330 0.0033",  # false noise ripple (previously triggered 20 s overlong VAD)
        "packet 1 1340 1350 0.0033",
        "packet 1 1360 1370 0.0080",  # real user speech well above noise floor
        "packet 1 1380 1390 0.0080",
    )
    assert float(out[2]) > 0.0053
    assert out[3:] == ["complete", "armed", "ignore", "ignore", "ignore", "ignore", "report"]


def test_startup_transient_never_masks_soft_speech_before_or_after_warmup(
    tool: pathlib.Path,
) -> None:
    """Brief loud startup sound must neither lift the floor early nor bias the warmup median."""

    out = _run(
        tool,
        "noise_reset",
        "observe 0.0030 5",   # 5 loud transient frames at boot
        "observe 0.0002 10",  # still < 25 warmup frames -> floor stays at base 0.0030
        "eff_floor 3",
        "observe 0.0002 10",  # completes 25-frame median warmup -> median is 0.0002
        "eff_floor 3",
    )
    assert out[3] == "0.0030"
    assert out[5] == "0.0030"


def test_stale_noise_floor_expires_after_long_standby_gap(tool: pathlib.Path) -> None:
    """When room noise turns off during a long standby gap, resumed weak speech is not dropped."""

    out = _run(
        tool,
        "noise_reset",
        "observe 0.0018 25",  # steady -40 dBFS fan -> floor 0.0063
        "eff_floor 3",
        "advance 10000",      # brief 10 s reconnect keeps calibrated room floor
        "eff_floor 3",
        "advance 60000",      # long >=60 s standby gap expires stale floor
        "eff_floor 3",
        "observe 0.0002 3",   # 3 quiet frames after tap stay at base 0.0030
        "eff_floor 3",
    )
    assert float(out[2]) > 0.0053
    assert float(out[4]) > 0.0053
    assert out[6] == "0.0030"
    assert out[8] == "0.0030"


def test_long_uninterrupted_playback_preserves_calibrated_noise_floor_for_handoff(
    tool: pathlib.Path,
) -> None:
    """A 75 s story playback keeps capture alive and must not expire the calibrated room floor."""

    out = _run(
        tool,
        "noise_reset",
        "observe 0.0018 25",   # steady -40 dBFS room -> calibrated floor 0.0063 (t = 500 ms)
        "keepalive 3750",      # 75 s of uninterrupted playback uplink frames (t = 75500 ms)
        "eff_floor 3",
        "complete 1",
        "listen 13 75500",
        "packet 1 75800 75810 0.0018",
        "packet 1 75820 75830 0.0033",
        "packet 1 75840 75850 0.0033",
        "packet 1 75860 75870 0.0080",
        "packet 1 75880 75890 0.0080",
    )
    assert float(out[3]) > 0.0053
    assert out[4:] == ["complete", "armed", "ignore", "ignore", "ignore", "ignore", "report"]
