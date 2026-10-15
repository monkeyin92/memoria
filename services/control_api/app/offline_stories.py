"""Titles of the offline story pack the device firmware ships."""

from __future__ import annotations

from typing import Final

#: (id, title) for every production clip in the firmware pack, in pack order.
#: The control-api image has no firmware tree, so this copies STORIES from
#: firmware/esp32/scripts/build_offline_stories.py. test_offline_stories.py
#: fails when it drifts from the shipped stories/index.json.
OFFLINE_STORIES: Final[tuple[tuple[str, str], ...]] = (
    ("story_01", "小星星的枕头"),
    ("story_02", "月亮不着急"),
    ("story_03", "大熊和森林里的悄悄话"),
)
