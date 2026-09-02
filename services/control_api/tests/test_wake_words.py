from __future__ import annotations

import pytest
from services.control_api.app.wake_words import (
    CUSTOM_WAKE_WORD_ID,
    DEFAULT_WAKE_WORD_ID,
    resolve_wake_word_settings,
    validate_custom_wake_word,
    wake_word_catalog_payload,
    wake_word_validation_warnings,
)


def test_wake_word_catalog_includes_whitelist_entries() -> None:
    payload = wake_word_catalog_payload()
    ids = {item["id"] for item in payload}
    assert DEFAULT_WAKE_WORD_ID in ids
    assert "mei_mo_li_ya" in ids
    assert all(item["device_ready"] for item in payload)


def test_validate_custom_wake_word_normalizes_pinyin() -> None:
    resolved = validate_custom_wake_word(pinyin=" Xiao   Hei ", display="小黑")
    assert resolved["wake_word_id"] == CUSTOM_WAKE_WORD_ID
    assert resolved["wake_word_pinyin"] == "xiao hei"
    assert resolved["wake_word_display"] == "小黑"
    assert resolved["syllables"] == 2
    assert wake_word_validation_warnings(resolved)


def test_resolve_catalog_wake_word_ignores_stale_custom_fields() -> None:
    resolved = resolve_wake_word_settings(wake_word_id="mei_mo_li_ya")
    assert resolved["wake_word_pinyin"] == "mei mo li ya"
    assert resolved["wake_word_display"] == "梅莫里亚"


@pytest.mark.parametrize(
    ("pinyin", "display"),
    [
        ("x", "单"),
        ("one two three four five six seven", "太长"),
        ("xiao-hei", "小黑"),
    ],
)
def test_custom_wake_word_rejects_invalid_pinyin(pinyin: str, display: str) -> None:
    with pytest.raises(ValueError):
        validate_custom_wake_word(pinyin=pinyin, display=display)
