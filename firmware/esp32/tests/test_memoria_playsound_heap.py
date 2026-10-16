"""PlaySound heap checks on a poisoned image must not walk all of PSRAM."""

from __future__ import annotations

import pathlib

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
PATCH = FIRMWARE_ROOT / "overlay" / "patches" / "0042-memoria-playsound-heap-check.patch"
BOARD = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
PRODUCT = BOARD / "config.json"
BENCH = BOARD / "config.bench.json"
KEEP_AWAKE = BOARD / "config.keep-awake.json"


def test_playsound_checks_internal_ram_and_one_new_packet() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    assert "heap_caps_check_integrity_all" not in patch
    assert "heap_caps_check_integrity(MALLOC_CAP_INTERNAL, true)" in patch
    guard = "if (!esp_ptr_external_ram(packet.get()))"
    call = "heap_caps_check_integrity_addr(reinterpret_cast<intptr_t>(packet.get()), true)"
    assert guard in patch
    assert call in patch
    assert patch.index(guard) < patch.index(call)
    assert "#include <esp_memory_utils.h>" in patch
    assert "heap_caps_check_integrity(MALLOC_CAP_SPIRAM, true)" in patch
    assert 'xTaskCreate(PsramHeapCheckTask, "psram_heap", 4096, nullptr, 1, nullptr)' in patch
    assert "pdMS_TO_TICKS(30000)" in patch
    assert "CONFIG_HEAP_POISONING_COMPREHENSIVE" in patch
    product = PRODUCT.read_text(encoding="utf-8")
    assert "CONFIG_ESP_INT_WDT_TIMEOUT_MS" not in product
    assert "CONFIG_HEAP_POISONING_COMPREHENSIVE" not in product
    for manifest in (BENCH, KEEP_AWAKE):
        text = manifest.read_text(encoding="utf-8")
        assert "CONFIG_HEAP_POISONING_COMPREHENSIVE=y" in text
        assert "CONFIG_ESP_INT_WDT_TIMEOUT_MS=10000" in text
