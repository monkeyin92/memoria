import shutil
import subprocess
from pathlib import Path

import pytest

FIRMWARE = Path(__file__).parents[1]
BOARD_CONFIG = FIRMWARE / "overlay/files/main/boards/memoria/esp-vocat/config.h"


def test_memoria_board_is_in_device_aec_dependency_list() -> None:
    patch = (FIRMWARE / "overlay/patches/0001-register-memoria-board.patch").read_text(
        encoding="utf-8"
    )
    assert "config USE_DEVICE_AEC" in patch
    assert any(
        line.startswith("+") and "|| BOARD_TYPE_MEMORIA_ESP_VOCAT" in line
        for line in patch.splitlines()
    )


@pytest.mark.parametrize(
    ("processor", "device_aec", "server_aec"),
    [(None, None, None), (1, 1, None)]
    + [(p, d, s) for p in (0, 1) for d in (0, 1) for s in (0, 1)],
)
def test_board_rejects_a_build_without_effective_device_aec(
    tmp_path: Path, processor: int | None, device_aec: int | None, server_aec: int | None
) -> None:
    compiler = shutil.which("c++")
    assert compiler is not None, "the firmware host gate requires a C++ compiler"
    (tmp_path / "driver").mkdir()
    for name in ("gpio.h", "uart.h", "spi_master.h"):
        (tmp_path / "driver" / name).write_text("", encoding="utf-8")
    settings = {
        "USE_AUDIO_PROCESSOR": processor,
        "USE_DEVICE_AEC": device_aec,
        "USE_SERVER_AEC": server_aec,
    }
    (tmp_path / "sdkconfig.h").write_text(
        "".join(
            f"#define CONFIG_{name} {value}\n"
            for name, value in settings.items()
            if value is not None
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [compiler, "-E", "-x", "c++", "-I", str(tmp_path), str(BOARD_CONFIG)],
        capture_output=True,
        text=True,
        check=False,
    )
    if processor == 1 and device_aec == 1 and server_aec != 1:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "Memoria ESP-VoCat requires device-side AEC" in result.stderr
