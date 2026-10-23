"""Host checks for session.close fallback playback.

Media-edge sends ``session.close`` with ``reason`` and an optional
``play_line``. ``tts_unavailable`` follows that flag. ``llm_unavailable``
always enters the offline menu without replaying the clip the server already
spoke. Other reasons leave the menu alone. The harness links no ESP-IDF code
and does not open a serial port.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"

HARNESS = r"""
#include "memoria_offline_menu.h"
#include "memoria_session_close.h"

#include <cstdio>
#include <cstring>

int main() {
    char line[1024];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0' || line[0] == '#') continue;
        if (strcmp(line, "reset") == 0) {
            memoria::OfflineMenuSignals::ConsumeLlmUnavailable(nullptr);
            printf("ok\n");
        } else if (strncmp(line, "close ", 6) == 0) {
            char reason[129] = {};
            int play_line = 0;
            if (sscanf(line + 6, "%128s %d", reason, &play_line) != 2) {
                return 2;
            }
            const memoria::SessionCloseFallback fallback =
                memoria::ClassifySessionCloseFallback(reason, play_line != 0);
            if (fallback != memoria::SessionCloseFallback::kNone) {
                memoria::OfflineMenuSignals::PostLlmUnavailable(
                    fallback == memoria::SessionCloseFallback::kMenuPlayLine);
            }
            bool play = false;
            const bool pending = memoria::OfflineMenuSignals::ConsumeLlmUnavailable(&play);
            printf("accepted=%d pending=%d play=%d\n",
                   fallback != memoria::SessionCloseFallback::kNone ? 1 : 0,
                   pending ? 1 : 0, play ? 1 : 0);
        } else {
            return 2;
        }
        fflush(stdout);
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def parser():
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "session_close_tool"
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


def _run(tool: pathlib.Path, *lines: str) -> list[str]:
    completed = subprocess.run(
        [str(tool)], input="\n".join(lines) + "\n", text=True, capture_output=True, check=True
    )
    return completed.stdout.splitlines()


def _close(reason: str, play_line: bool | None = None) -> str:
    if play_line is None:
        return f"close {reason} 0"
    return f"close {reason} {1 if play_line else 0}"


def test_tts_unavailable_play_line_queues_the_offline_clip(parser: pathlib.Path) -> None:
    lines = _run(parser, "reset", _close("tts_unavailable", True))
    assert lines == ["ok", "accepted=1 pending=1 play=1"]


def test_tts_unavailable_without_play_line_does_not_replay(parser: pathlib.Path) -> None:
    lines = _run(parser, "reset", _close("tts_unavailable", False))
    assert lines == ["ok", "accepted=1 pending=1 play=0"]


def test_omitted_play_line_does_not_replay(parser: pathlib.Path) -> None:
    lines = _run(parser, "reset", _close("tts_unavailable"))
    assert lines == ["ok", "accepted=1 pending=1 play=0"]


def test_llm_unavailable_never_replays_even_if_play_line_is_set(parser: pathlib.Path) -> None:
    lines = _run(
        parser,
        "reset",
        _close("llm_unavailable", True),
        _close("llm_unavailable"),
    )
    assert lines == [
        "ok",
        "accepted=1 pending=1 play=0",
        "accepted=1 pending=1 play=0",
    ]


def test_other_close_reasons_do_not_enter_the_offline_menu(parser: pathlib.Path) -> None:
    lines = _run(parser, "reset", _close("owner_silence_timeout", True))
    assert lines == ["ok", "accepted=0 pending=0 play=0"]


def test_close_decision_uses_the_server_field_names() -> None:
    """The handler reads the same reason and play_line keys media-edge sends."""

    source = (MEMORIA_DIR / "memoria_protocol.cc").read_text(encoding="utf-8")
    handler = source[source.index('if (type == "session.close")') :]
    handler = handler[: handler.index("Ignoring unknown v2 server message")]
    assert 'GetString(root.value, "reason"' in handler
    assert 'GetBool(root.value, "play_line"' in handler
    assert "ClassifySessionCloseFallback(" in handler
