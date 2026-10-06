"""The PC side of the bench build's `status` line (TODOLIST M-2): parse it from the serial log, wait for it
after a request, and turn two of them into rates.

The robot's formatter is compiled and run in firmware/esp32/tests/test_memoria_mascot_status.py, which also
hands its real output to this script.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from scripts import bench_status as status

SCRIPT = Path(status.__file__)
ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = ROOT / "firmware/esp32/overlay/files/main/boards/memoria/esp-vocat"

# The line a companion that has idled for a while writes (the same one the firmware test formats).
BODY = (
    "status up_ms=123456 phase=idle mood=neutral frame=default_blink screen_off=0 sleeping=0 captioned=0 "
    "frames=5000 drawn=4000 render_us=36000000 render_max_us=21000 busy_us=52000000 px=800000000 "
    "composed_px=650000000 extra_ms=10 heap_free=150000 psram_free=4100000 anim_stack_free=2300"
)
LINE = f"22:11:55.151 I (123456) MemoriaBench: {BODY}"


def _line(**changes: int | str) -> str:
    """BODY with some fields replaced."""
    text = BODY
    for key, value in changes.items():
        text, count = re.subn(rf"\b{key}=\S+", f"{key}={value}", text)
        assert count == 1, key
    return f"10:00:00.000 I (1) MemoriaBench: {text}"


def test_a_status_line_is_read_field_by_field() -> None:
    parsed = status.parse_status(LINE)
    assert parsed == {
        "up_ms": 123456,
        "phase": "idle",
        "mood": "neutral",
        "frame": "default_blink",
        "screen_off": 0,
        "sleeping": 0,
        "captioned": 0,
        "frames": 5000,
        "drawn": 4000,
        "render_us": 36000000,
        "render_max_us": 21000,
        "busy_us": 52000000,
        "px": 800000000,
        "composed_px": 650000000,
        "extra_ms": 10,
        "heap_free": 150000,
        "psram_free": 4100000,
        "anim_stack_free": 2300,
        "at": "22:11:55.151",
    }


def test_colour_codes_a_missing_time_stamp_and_the_full_64_bit_range_do_not_matter() -> None:
    coloured = f"\x1b[0;32mI (123456) MemoriaBench: {BODY}\x1b[0m\r"
    parsed = status.parse_status(coloured)
    assert parsed is not None and parsed["at"] == "" and parsed["anim_stack_free"] == 2300
    big = status.parse_status(_line(busy_us=2**64 - 1, px=2**63 + 5))
    assert big is not None and big["busy_us"] == 2**64 - 1 and big["px"] == 2**63 + 5


@pytest.mark.parametrize(
    "line",
    [
        "I (1) MemoriaBench: snap id=0001e240 sent=1",
        "I (1) MemoriaMascot: anim frames=791 drawn=498",
        LINE[:-30],  # cut: the last fields are gone
        LINE.replace(" mood=neutral", ""),  # a field missing
        LINE.replace("frames=5000", "frames=5k"),  # not a number
        LINE.replace("drawn=4000", "drawn=-4"),
        LINE + " extra=1",  # a field the robot does not print
        "",
    ],
)
def test_a_line_that_is_not_a_complete_status_line_is_skipped(line: str) -> None:
    assert status.parse_status(line) is None


def test_the_unknown_frame_before_the_first_draw_is_a_name_like_the_others() -> None:
    parsed = status.parse_status(_line(frame="none"))
    assert parsed is not None and parsed["frame"] == "none"


def test_the_field_list_is_the_one_the_firmware_formats() -> None:
    source = (BOARD_DIR / "memoria_mascot_status.h").read_text(encoding="utf-8")
    body = source.split("BenchStatusLine(", 1)[1].split("static_cast", 1)[0]
    assert tuple(re.findall(r"(\w+)=%", body)) == status.FIELDS


def test_collect_keeps_the_order_and_skips_everything_else() -> None:
    text = "\n".join(
        [
            "boot noise",
            _line(up_ms=1000),
            "I (2) MemoriaBench: snap id=00000001 sent=1",
            LINE[:-20],
            _line(up_ms=2000),
        ]
    )
    assert [line["up_ms"] for line in status.collect(text)] == [1000, 2000]


def _statuses(**second: int | str) -> tuple[status.Status, status.Status]:
    first = status.parse_status(
        _line(up_ms=1000, frames=1000, drawn=900, render_us=10_000_000, busy_us=12_000_000)
        .replace("px=800000000", "px=100000000")
        .replace("composed_px=650000000", "composed_px=50000000")
    )
    changes = {
        "up_ms": 61000,
        "frames": 2500,
        "drawn": 2370,
        "render_us": 28_000_000,
        "busy_us": 42_000_000,
        "px": 247_000_000,
        "composed_px": 123_500_000,
        "render_max_us": 33_000,
        "extra_ms": 20,
        "heap_free": 140_000,
        "psram_free": 4_000_000,
        "anim_stack_free": 2_100,
    } | second
    later = status.parse_status(_line(**changes))
    assert first is not None and later is not None
    return first, later


def test_two_lines_give_the_rates_between_them() -> None:
    result = status.rates(*_statuses())
    assert result["seconds"] == 60.0
    assert result["frames_per_s"] == 25.0  # 1500 frames
    assert result["drawn_per_s"] == 24.5  # 1470 frames
    assert result["render_ms_per_drawn"] == pytest.approx(18_000_000 / 1470 / 1000)
    assert result["busy_pct"] == pytest.approx(50.0)  # 30 s of 60 s
    assert result["px_per_drawn"] == 100_000  # 147,000,000 / 1470
    assert result["composed_px_per_drawn"] == 50_000  # 73,500,000 / 1470
    assert result["render_max_ms_since_boot"] == 33.0
    assert (result["extra_ms"], result["heap_free"]) == (20, 140_000)
    assert (result["psram_free"], result["anim_stack_free"]) == (4_000_000, 2_100)


def test_the_millisecond_clock_may_wrap_between_the_lines() -> None:
    first, later = _statuses()
    first["up_ms"] = 2**32 - 10_000
    later["up_ms"] = 50_000
    assert status.rates(first, later)["seconds"] == 60.0


def test_a_dark_panel_has_frames_but_nothing_drawn() -> None:
    first, later = _statuses(
        drawn=900, render_us=10_000_000, px=100_000_000, composed_px=50_000_000
    )
    result = status.rates(first, later)
    assert result["drawn_per_s"] == 0
    assert result["render_ms_per_drawn"] is None and result["px_per_drawn"] is None
    assert "no frame was drawn" in status.format_rates(first, later)


def test_lines_that_cannot_be_compared_are_refused() -> None:
    first, later = _statuses()
    with pytest.raises(status.StatusError, match="restarted"):
        status.rates(later, first)  # asked the wrong way round: every counter goes backwards
    reboot = dict(later) | {"up_ms": 5000, "frames": 40, "drawn": 30, "render_us": 400_000}
    with pytest.raises(status.StatusError, match="restarted"):
        status.rates(first, reboot)
    with pytest.raises(status.StatusError, match="no time between"):
        status.rates(first, first)


def test_the_report_names_what_it_measured() -> None:
    first, later = _statuses()
    text = status.format_rates(first, later)
    assert text.splitlines()[0] == "between up_ms 1000 and 61000 (60.0 s), phase idle -> idle:"
    assert "loop      25.0 /s   drawn 24.5 /s" in text
    assert "busy      50.0 % of the animation task's time" in text
    assert "heap 140,000" in text and "animation stack free 2,100" in text
    assert status.format_status(first).splitlines()[0].split() == ["up_ms", "1000"]


class _Rig:
    """A log file the 'robot' keeps writing to every time the waiting code sleeps."""

    def __init__(self, path: Path, script: list[list[str]]) -> None:
        self.path = path
        self.script = script
        self.now = 0.0
        self.sleeps = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        if self.script:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write("".join(f"{line}\n" for line in self.script.pop(0)))


def test_waiting_returns_the_first_line_written_after_the_request(tmp_path: Path) -> None:
    log = tmp_path / "robot.log"
    log.write_text(_line(up_ms=111) + "\n", encoding="utf-8")  # an earlier answer: not this one's
    offset = log.stat().st_size
    rig = _Rig(
        log, [["I (5) MemoriaUsb: usb status accepted"], [_line(up_ms=222), _line(up_ms=333)]]
    )
    got = status.wait_for_status(log, offset, 10, sleep=rig.sleep, clock=rig.clock)
    assert got["up_ms"] == 222 and rig.sleeps == 2


@pytest.mark.parametrize(
    ("robot_says", "match"),
    [
        ("I (9) MemoriaUsb: usb command ignored (not a command)", "not running a bench image"),
        (
            "I (9) MemoriaUsb: usb status ignored reason=no_display",
            "usb status ignored reason=no_display",
        ),
    ],
)
def test_a_robot_that_does_not_take_the_request_ends_the_wait_at_once(
    tmp_path: Path, robot_says: str, match: str
) -> None:
    log = tmp_path / "robot.log"
    log.write_text("", encoding="utf-8")
    rig = _Rig(log, [[robot_says]])
    with pytest.raises(status.StatusError, match=match):
        status.wait_for_status(log, 0, 10, sleep=rig.sleep, clock=rig.clock)
    assert rig.sleeps == 1


def test_silence_ends_in_a_timeout_not_a_hang(tmp_path: Path) -> None:
    log = tmp_path / "robot.log"
    rig = _Rig(log, [])
    with pytest.raises(status.StatusError, match="no status line in 2 s"):
        status.wait_for_status(log, 0, 2, poll=0.5, sleep=rig.sleep, clock=rig.clock)


def test_the_cli_prints_the_last_line_or_the_rates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first, later = _statuses()
    log = tmp_path / "robot.log"
    log.write_text(
        "boot\n" + _line(up_ms=1000) + "\n" + _line(up_ms=61000) + "\n", encoding="utf-8"
    )
    assert status.main([str(log)]) == 0
    assert "up_ms  61000" in capsys.readouterr().out
    assert status.main([str(log), "--rates"]) == 0
    assert "between up_ms 1000 and 61000" in capsys.readouterr().out
    log.write_text(_line(up_ms=1000) + "\n", encoding="utf-8")
    assert status.main([str(log), "--rates"]) == 1
    assert "need 2" in capsys.readouterr().err
    log.write_text(_line(up_ms=1000) + "\n" + _line(up_ms=1000) + "\n", encoding="utf-8")
    assert status.main([str(log), "--rates"]) == 1
    assert "no time between" in capsys.readouterr().err
    assert first and later


def test_the_robot_texts_the_script_waits_for_are_the_ones_the_board_writes() -> None:
    board = (BOARD_DIR / "memoria_esp_vocat.cc").read_text(encoding="utf-8")
    assert f'"{status.IGNORED_COMMAND}"' in board
    assert '"usb status ignored reason=no_display"' in board
    bench = (BOARD_DIR / "memoria_mascot_bench.cc").read_text(encoding="utf-8")
    assert 'constexpr char kTag[] = "MemoriaBench";' in bench
    assert 'ESP_LOGI(kTag, "%s", text);' in bench


def test_the_script_needs_nothing_but_the_standard_library() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    imported = set(re.findall(r"^(?:import|from) ([A-Za-z_][A-Za-z0-9_]*)", source, re.MULTILINE))
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
