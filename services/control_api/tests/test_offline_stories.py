from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.control_api.app.offline_stories import OFFLINE_STORIES

FIRMWARE_STORIES = (
    Path(__file__).resolve().parents[3]
    / "firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/assets/stories/index.json"
)


@pytest.mark.hermetic
def test_offline_stories_match_the_shipped_firmware_pack() -> None:
    manifest = json.loads(FIRMWARE_STORIES.read_text(encoding="utf-8"))
    shipped = tuple(
        (story["id"], story["title"])
        for story in manifest["stories"]
        if story["placeholder"] is False
    )
    assert OFFLINE_STORIES == shipped
