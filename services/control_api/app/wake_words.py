"""Curated and custom wake-word catalog for device settings."""

from __future__ import annotations

import re
from typing import Final, TypedDict

PINYIN_TOKEN = re.compile(r"^[a-z]{1,16}$")
CUSTOM_WAKE_WORD_ID: Final = "custom"


class WakeWordOption(TypedDict):
    id: str
    display: str
    pinyin: str
    syllables: int
    device_ready: bool
    note: str


class ResolvedWakeWord(TypedDict):
    wake_word_id: str
    wake_word_pinyin: str
    wake_word_display: str
    syllables: int
    source: str


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
        "device_ready": True,
        "note": "符合 ESP-SR 3–6 音节建议；与默认 MultiNet 模型共用。",
    },
)

DEFAULT_WAKE_WORD_ID: Final = "mo_li"
WAKE_WORD_IDS: Final[frozenset[str]] = frozenset(
    {item["id"] for item in WAKE_WORD_CATALOG} | {CUSTOM_WAKE_WORD_ID}
)


def wake_word_by_id(wake_word_id: str) -> WakeWordOption | None:
    for item in WAKE_WORD_CATALOG:
        if item["id"] == wake_word_id:
            return item
    return None


def wake_word_catalog_payload(*, include_pending: bool = False) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for item in WAKE_WORD_CATALOG:
        payload = dict(item)
        if not include_pending and not item["device_ready"]:
            continue
        items.append(payload)
    return items


def normalize_wake_word_pinyin(value: str) -> str:
    tokens = [token.strip().lower() for token in value.strip().split() if token.strip()]
    if not tokens:
        raise ValueError("wake_word_pinyin must not be empty")
    for token in tokens:
        if not PINYIN_TOKEN.fullmatch(token):
            raise ValueError("wake_word_pinyin must use lowercase a-z syllables separated by spaces")
    return " ".join(tokens)


def validate_custom_wake_word(*, pinyin: str, display: str) -> ResolvedWakeWord:
    display_text = display.strip()
    if not display_text:
        raise ValueError("wake_word_display must not be empty")
    if len(display_text) > 16:
        raise ValueError("wake_word_display must be at most 16 characters")
    normalized = normalize_wake_word_pinyin(pinyin)
    syllables = len(normalized.split())
    if syllables < 2 or syllables > 6:
        raise ValueError("custom wake words must contain 2 to 6 pinyin syllables")
    return {
        "wake_word_id": CUSTOM_WAKE_WORD_ID,
        "wake_word_pinyin": normalized,
        "wake_word_display": display_text,
        "syllables": syllables,
        "source": "custom",
    }


def resolve_wake_word_settings(
    *,
    wake_word_id: str,
    wake_word_pinyin: str | None = None,
    wake_word_display: str | None = None,
) -> ResolvedWakeWord:
    if wake_word_id not in WAKE_WORD_IDS:
        raise ValueError(f"wake_word_id must be one of {sorted(WAKE_WORD_IDS)}")
    if wake_word_id == CUSTOM_WAKE_WORD_ID:
        if wake_word_pinyin is None or wake_word_display is None:
            raise ValueError("custom wake words require wake_word_pinyin and wake_word_display")
        return validate_custom_wake_word(pinyin=wake_word_pinyin, display=wake_word_display)
    catalog = wake_word_by_id(wake_word_id)
    if catalog is None or not catalog["device_ready"]:
        raise ValueError("selected wake word is not device-ready")
    return {
        "wake_word_id": catalog["id"],
        "wake_word_pinyin": catalog["pinyin"],
        "wake_word_display": catalog["display"],
        "syllables": catalog["syllables"],
        "source": "catalog",
    }


def wake_word_validation_warnings(resolved: ResolvedWakeWord) -> list[str]:
    warnings: list[str] = []
    if resolved["syllables"] < 3:
        warnings.append("两音节唤醒词误唤醒风险较高，建议在安静环境下单独验收。")
    if resolved["wake_word_id"] == CUSTOM_WAKE_WORD_ID:
        warnings.append("自定义唤醒词通过 MultiNet 命令词生效，识别率需真机验收。")
    return warnings
