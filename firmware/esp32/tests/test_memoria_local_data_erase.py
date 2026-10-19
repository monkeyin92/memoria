"""Host-side checks for local data erase on device unbinding and factory reset."""

from __future__ import annotations

import pathlib
import re

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
PATCH_DIR = FIRMWARE_ROOT / "overlay" / "patches"
HEADER = MEMORIA_DIR / "memoria_local_data_erase.h"


def test_header_declares_erase_function() -> None:
    """Verify EraseLocalChildData function is declared."""
    source = HEADER.read_text(encoding="utf-8")
    assert "bool EraseLocalChildData()" in source
    assert "memoria_identity" in source  # mention in comment about preservation


def test_erase_function_lists_all_required_namespaces() -> None:
    """Verify the implementation includes all child-related NVS namespaces."""
    impl = (MEMORIA_DIR / "memoria_local_data_erase.cc").read_text(encoding="utf-8")

    # Check all required namespaces are mentioned
    required_namespaces = [
        "memoria_runtime",   # activation_v, activation_ctr
        "memoria_ui",        # wake_mode, companion
        "memoria_power",     # quiet_start, quiet_end, wifi_hold_ms
        "memoria",           # wake_word_id, wake_word_cmd, wake_word_disp
    ]

    for ns in required_namespaces:
        assert f'"{ns}"' in impl, f"Missing namespace {ns}"

    # Verify memoria_identity is NOT erased
    assert 'memoria_identity' not in impl or 'erase' not in impl.lower().split('memoria_identity')[0][-100:]


def test_erase_function_clears_wifi_config() -> None:
    """Verify Wi-Fi SSID and password are erased."""
    impl = (MEMORIA_DIR / "memoria_local_data_erase.cc").read_text(encoding="utf-8")
    assert "esp_wifi_set_config" in impl
    assert "wifi_config_t" in impl
    assert "ssid[0] = '\\0'" in impl or 'ssid[0] = 0' in impl


def test_erase_called_on_device_release() -> None:
    """Verify EraseLocalChildData is called when server releases device (manifest 409)."""
    patch = (PATCH_DIR / "0042-memoria-local-data-erase-on-unbind.patch").read_text(encoding="utf-8")
    assert "EraseLocalChildData" in patch
    assert "409" in patch or "manifest_status == 409" in patch


def test_erase_preserves_device_identity() -> None:
    """Verify memoria_identity partition is never touched by erase function."""
    impl = (MEMORIA_DIR / "memoria_local_data_erase.cc").read_text(encoding="utf-8")
    header = HEADER.read_text(encoding="utf-8")

    # memoria_identity should be mentioned as NOT being erased
    assert "memoria_identity" in header
    # The implementation must not try to erase it
    assert impl.count("memoria_identity") == 0


def test_namespace_list_does_not_duplicate() -> None:
    """Ensure the namespace list in the erase function has no duplicates."""
    impl = (MEMORIA_DIR / "memoria_local_data_erase.cc").read_text(encoding="utf-8")

    # Extract namespace array
    start = impl.find("kEraseNamespaces[]")
    end = impl.find("};", start) + 2
    namespaces_section = impl[start:end]

    namespaces = re.findall(r'"(memoria[^"]*)"', namespaces_section)
    assert len(namespaces) == len(set(namespaces)), f"Duplicate namespaces: {namespaces}"

    # Verify we have exactly 4 namespaces
    assert len(namespaces) == 4, f"Expected 4 namespaces, got {len(namespaces)}: {namespaces}"


def test_activation_client_includes_erase_header() -> None:
    """Verify memoria_activation_client.cc includes the erase header."""
    patch = (PATCH_DIR / "0042-memoria-local-data-erase-on-unbind.patch").read_text(encoding="utf-8")
    assert '#include "memoria_local_data_erase.h"' in patch


def test_erase_returns_bool() -> None:
    """Verify EraseLocalChildData returns a bool indicating success."""
    header = HEADER.read_text(encoding="utf-8")
    assert "bool EraseLocalChildData()" in header

    impl = (MEMORIA_DIR / "memoria_local_data_erase.cc").read_text(encoding="utf-8")
    assert "return success" in impl


def test_no_factory_reset_entry_point_yet() -> None:
    """Verify no factory reset UI/command entry exists yet."""
    # Check USB commands - factory reset should not be there
    usb_header = (MEMORIA_DIR / "memoria_usb_command.h").read_text(encoding="utf-8")
    assert "kFactoryReset" not in usb_header
    assert "factory" not in usb_header.lower()

    # This test documents that factory reset entry is OUT OF SCOPE per issue #281
