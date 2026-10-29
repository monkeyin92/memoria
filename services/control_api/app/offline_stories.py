from __future__ import annotations

from typing import Final

# Recorded in the shipping voice on 2026-10-28 and packed into the firmware assets.
# The device route and the firmware pack must name the same set.
OFFLINE_STORIES: Final[tuple[tuple[str, str], ...]] = (
    ("story_01", "小星星的枕头"),
    ("story_02", "月亮不着急"),
    ("story_03", "大熊和森林里的悄悄话"),
    ("night_soothe", "哄睡词"),
)
