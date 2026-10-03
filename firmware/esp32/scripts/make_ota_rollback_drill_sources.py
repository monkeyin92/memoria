#!/usr/bin/env python3
"""Turn a copy of the firmware sources into the OTA rollback drill image's sources (never shipped).

    make_ota_rollback_drill_sources.py <memoria_firmware_release.h> <memoria_firmware_update.cc> <build>

The drill image is the shipping firmware with a newer build number that never confirms itself. Installed
over the air it boots as PENDING_VERIFY; because nothing marks it valid, the bootloader (which has
CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE) must go back to the previous slot at the next reset. That path was
never exercised on a real board (TODOLIST P2-08, "OTA 回滚没演练过"). ``build_ota_rollback_drill.sh`` runs
this on the build cache's files and restores them afterwards, so no drill code is ever committed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BUILD_DEFINE = re.compile(r"^#define MEMORIA_FIRMWARE_BUILD \d+$", re.MULTILINE)
CONFIRM_CALL = "if (esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {"
DRILL_LOG = (
    'ESP_LOGW(kTag, "OTA rollback drill: build %lu stays PENDING_VERIFY (state=%d); '
    'reset the board to roll back", static_cast<unsigned long>(kFirmwareBuild), static_cast<int>(state));\n'
    "    "
)


def apply(release_header: Path, update_source: Path, build: int) -> None:
    header = release_header.read_text(encoding="utf-8")
    if len(BUILD_DEFINE.findall(header)) != 1:
        raise SystemExit(f"{release_header}: expected exactly one MEMORIA_FIRMWARE_BUILD define")
    release_header.write_text(
        BUILD_DEFINE.sub(f"#define MEMORIA_FIRMWARE_BUILD {build}", header), encoding="utf-8"
    )
    source = update_source.read_text(encoding="utf-8")
    if source.count(CONFIRM_CALL) != 1:
        raise SystemExit(f"{update_source}: the confirm call the drill suppresses was not found once")
    update_source.write_text(
        source.replace(CONFIRM_CALL, DRILL_LOG + "if (false && " + CONFIRM_CALL[len("if (") :]),
        encoding="utf-8",
    )


def main(argv: list[str]) -> int:
    if len(argv) != 4 or not argv[3].isdigit() or int(argv[3]) < 1:
        raise SystemExit(__doc__)
    apply(Path(argv[1]), Path(argv[2]), int(argv[3]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
