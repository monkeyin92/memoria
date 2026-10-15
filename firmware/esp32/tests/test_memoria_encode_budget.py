"""Host check for the uplink encode budget.

The device drops uplink frames while the mascot is on screen. opus_codec is
already a higher priority than mascot_anim, so another priority cut does not
change who runs. This test runs the stall the 87 ms listening frame causes
and checks that the queue depth and the uplink cadence together finish 120 s
with no drops. The same stall with the old depth, or with the old cadence,
still drops.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
HEADER = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria" / "memoria_encode_budget.h"
SCENE = (
    FIRMWARE_ROOT
    / "overlay"
    / "files"
    / "main"
    / "boards"
    / "memoria"
    / "esp-vocat"
    / "memoria_mascot_scene.cc"
)
PATCH = (
    FIRMWARE_ROOT
    / "overlay"
    / "patches"
    / "0040-memoria-encode-queue-budget.patch"
)

HARNESS = r"""
#include "memoria_encode_budget.h"

#include <cstdio>

int drops_for(int depth, int interval_ms, int seconds) {
    const int stall = MEMORIA_LISTENING_FRAME_MS;
    const int service = MEMORIA_ENCODE_SERVICE_MS;
    const int frame = MEMORIA_ENCODE_FRAME_MS;
    const int period = interval_ms > stall ? interval_ms : stall;
    int queued = 0;
    int drops = 0;
    bool in_service = false;
    int remain = 0;
    const int end = seconds * 1000;
    for (int ms = 0; ms < end; ++ms) {
        const bool in_stall = (ms % period) < stall;
        if (!in_stall) {
            if (in_service) {
                remain -= 1;
                if (remain <= 0) {
                    in_service = false;
                }
            }
            if (!in_service && queued > 0) {
                queued -= 1;
                in_service = true;
                remain = service;
            }
        }
        if (ms % frame == 0) {
            if (queued >= depth) {
                drops += 1;
            } else {
                queued += 1;
            }
        }
    }
    return drops;
}

int main() {
    // The wired budget, then the old queue of 2 at 40 ms, then depth 8 at the
    // 140 ms cadence the frame pacer already reaches for an 87 ms frame.
    std::printf("%d\n", drops_for(MEMORIA_ENCODE_QUEUE_DEPTH, MEMORIA_UPLINK_MASCOT_INTERVAL_MS, 120));
    std::printf("%d\n", drops_for(2, 40, 120));
    std::printf("%d\n", drops_for(MEMORIA_ENCODE_QUEUE_DEPTH, 140, 120));
    std::printf("%d\n", MEMORIA_ENCODE_QUEUE_PCM_BYTES);
    return 0;
}
"""


def _stall_report() -> list[int]:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("host C++ compiler unavailable")
    with tempfile.TemporaryDirectory(prefix="encode-budget-") as tmp:
        source = pathlib.Path(tmp) / "stall.cc"
        binary = pathlib.Path(tmp) / "stall"
        source.write_text(HARNESS, encoding="utf-8")
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                f"-I{HEADER.parent}",
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        out = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
    return [int(line) for line in out.stdout.splitlines()]


def test_uplink_budget_survives_one_listening_frame_of_stall() -> None:
    wired, old_queue, old_cadence, pcm_bytes = _stall_report()
    assert wired == 0
    assert old_queue == 5998
    assert old_cadence == 1452
    assert pcm_bytes == 5120


def test_listening_phases_use_the_uplink_cadence() -> None:
    text = SCENE.read_text(encoding="utf-8")
    assert '#include "memoria_encode_budget.h"' in text
    fn = text[text.index("uint32_t MascotScene::FrameIntervalMs") :]
    fn = fn[: fn.index("void MascotScene::SetPhase")]
    uplink = fn[: fn.index("default:")]
    for phase in ("kListening", "kThinking", "kSpeaking"):
        assert f"ScenePhase::{phase}:" in uplink
    assert uplink.count("return MEMORIA_UPLINK_MASCOT_INTERVAL_MS;") == 1


def test_memoria_encode_queue_uses_the_budget_depth() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    added = "\n".join(
        line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    memoria = added[added.index("#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT") :]
    memoria = memoria[: memoria.index("#else")]
    assert '#include "memoria_encode_budget.h"' in memoria
    assert "MAX_ENCODE_TASKS_IN_QUEUE MEMORIA_ENCODE_QUEUE_DEPTH" in memoria
    assert "#define MAX_ENCODE_TASKS_IN_QUEUE 2" in added
