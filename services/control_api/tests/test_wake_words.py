from __future__ import annotations

from services.control_api.app.wake_words import DEFAULT_WAKE_WORD_ID, wake_word_catalog_payload


def test_wake_word_catalog_includes_current_default() -> None:
    payload = wake_word_catalog_payload()
    assert any(item["id"] == DEFAULT_WAKE_WORD_ID for item in payload)
    default = next(item for item in payload if item["id"] == DEFAULT_WAKE_WORD_ID)
    assert default["display"] == "茉莉"
    assert default["device_ready"] is True

    pending = next(item for item in payload if item["id"] == "mei_mo_li_ya")
    assert pending["device_ready"] is False
