"""Curated wake-word catalog for device settings."""

from __future__ import annotations

from typing import Final, TypedDict


class WakeWordOption(TypedDict):
    id: str
    display: str
    pinyin: str
    syllables: int
    device_ready: bool
    note: str


WAKE_WORD_CATALOG: Final[tuple[WakeWordOption, ...]] = (
    {
        "id": "mo_li",
        "display": "茉莉",
        "pinyin": "mo li",
        "syllables": 2,
        "device_ready": True,
        "note": "当前板卡默认唤醒词。",
    },
    {
        "id": "mei_mo_li_ya",
        "display": "梅莫里亚",
        "pinyin": "mei mo li ya",
        "syllables": 4,
        "device_ready": False,
        "note": "符合 ESP-SR 3–6 音节建议；需后续固件预装对应模型。",
    },
)

DEFAULT_WAKE_WORD_ID: Final = "mo_li"
WAKE_WORD_IDS: Final[frozenset[str]] = frozenset(item["id"] for item in WAKE_WORD_CATALOG)


def wake_word_by_id(wake_word_id: str) -> WakeWordOption | None:
    for item in WAKE_WORD_CATALOG:
        if item["id"] == wake_word_id:
            return item
    return None


def wake_word_catalog_payload() -> list[dict[str, object]]:
    return [dict(item) for item in WAKE_WORD_CATALOG]
