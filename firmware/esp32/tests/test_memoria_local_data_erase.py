"""Host-side checks for local data erase on device unbinding (issue #281)."""

from __future__ import annotations

import pathlib
import re

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
PATCH_DIR = FIRMWARE_ROOT / "overlay" / "patches"
ACTIVATION_CLIENT = MEMORIA_DIR / "memoria_activation_client.cc"
HEADER = MEMORIA_DIR / "memoria_local_data_erase.h"
IMPL = MEMORIA_DIR / "memoria_local_data_erase.cc"


def test_erase_function_declared() -> None:
    """Verify EraseLocalChildData function is declared."""
    source = HEADER.read_text(encoding="utf-8")
    assert "bool EraseLocalChildData()" in source
    assert "memoria_identity" in source  # Must document that it's preserved


def test_erase_guard_prevents_repeated_calls() -> None:
    """Verify guard prevents erasure on every 409; only erase on state transition."""
    source = ACTIVATION_CLIENT.read_text(encoding="utf-8")

    # Find the 409 handler block
    match = re.search(
        r"if\s*\(\s*manifest_status\s*==\s*409\s*\)\s*\{([^}]+)\}",
        source,
        re.DOTALL
    )
    assert match, "409 handler block not found"
    block = match.group(1)

    # Must read activation_v BEFORE calling EraseLocalChildData
    activation_v_read = "previous_activation_v = runtime.GetInt"
    assert activation_v_read in block, "Must read activation_v before erase"

    # Must have the guard condition
    assert "if (previous_activation_v != 0)" in block, "Must guard erase with activation_v != 0"

    # EraseLocalChildData must be inside the guard
    erase_call_start = block.find("EraseLocalChildData()")
    guard_start = block.find("if (previous_activation_v != 0)")
    assert guard_start < erase_call_start, "Erase must be inside the guard"


def test_no_nvs_erase_all() -> None:
    """Verify nvs_erase_all is NOT used (would destroy activation_ctr and other keys)."""
    impl = IMPL.read_text(encoding="utf-8")
    assert "nvs_erase_all" not in impl, "Must not use nvs_erase_all; use nvs_erase_key instead"


def test_erase_keys_list_matches_required() -> None:
    """Verify the erase keys list includes exactly the required keys, no more, no less."""
    impl = IMPL.read_text(encoding="utf-8")

    # Extract all (namespace, key) pairs from the code
    key_pairs = re.findall(
        r'\{\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\}',
        impl
    )
    # Filter to only erase keys (exclude EraseKey struct definition itself)
    # By checking they appear after the kEraseKeys array starts
    erase_keys_start = impl.find("constexpr EraseKey kEraseKeys[]")
    erase_keys_end = impl.find("};", erase_keys_start) + 2

    erase_keys_section = impl[erase_keys_start:erase_keys_end]
    key_pairs = re.findall(
        r'\{\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\}',
        erase_keys_section
    )

    # Expected keys: (namespace, key)
    expected = {
        ("memoria_ui", "wake_mode"),
        ("memoria_ui", "companion"),
        ("memoria_power", "quiet_start"),
        ("memoria_power", "quiet_end"),
        ("memoria_power", "wifi_hold_ms"),
        ("memoria", "wake_word_id"),
        ("memoria", "wake_word_cmd"),
        ("memoria", "wake_word_disp"),
    }

    actual = set(key_pairs)
    assert actual == expected, f"Keys mismatch.\nExpected: {expected}\nActual: {actual}"

    # Verify activation_ctr is NOT in the list
    for ns, key in actual:
        assert key != "activation_ctr", "CRITICAL: activation_ctr must not be erased (replay prevention)"
        assert key != "memoria_identity", "CRITICAL: memoria_identity must not be erased"


def test_uses_ssidmanager_clear() -> None:
    """Verify Wi-Fi credentials are erased via SsidManager::GetInstance().Clear()."""
    impl = IMPL.read_text(encoding="utf-8")
    assert "SsidManager::GetInstance().Clear()" in impl, "Must use SsidManager::GetInstance().Clear()"
    assert "#include \"ssid_manager.h\"" in impl, "Must include ssid_manager.h"
    # Verify no esp_wifi_set_config (incorrect approach)
    assert "esp_wifi_set_config" not in impl, "Must not use esp_wifi_set_config; use SsidManager"
    assert "wifi_config_t" not in impl, "wifi_config_t is not needed with SsidManager"


def test_activation_client_calls_erase_with_guard() -> None:
    """Verify memoria_activation_client.cc calls EraseLocalChildData with proper guard."""
    source = ACTIVATION_CLIENT.read_text(encoding="utf-8")

    # Find the erase call
    assert "EraseLocalChildData()" in source, "EraseLocalChildData must be called"

    # Find the 409 block and verify guard structure
    match = re.search(
        r"if\s*\(\s*manifest_status\s*==\s*409\s*\)\s*\{([^}]*if\s*\(\s*previous_activation_v[^}]+)\}",
        source,
        re.DOTALL
    )
    assert match, "409 handler with guard not found"
    block = match.group(0)

    # Verify the guard is present
    assert "if (previous_activation_v != 0)" in block

    # Verify EraseLocalChildData is called inside a guard
    assert "EraseLocalChildData()" in block


def test_activation_client_includes_erase_header() -> None:
    """Verify memoria_activation_client.cc includes the erase header."""
    source = ACTIVATION_CLIENT.read_text(encoding="utf-8")
    assert '#include "memoria_local_data_erase.h"' in source


def test_no_nvs_erase_key_on_activation_ctr() -> None:
    """Mutation: verify activation_ctr is NOT erased."""
    impl = IMPL.read_text(encoding="utf-8")
    # activation_ctr must NEVER appear in any erase context
    for line in impl.split('\n'):
        if 'activation_ctr' in line:
            # Only allowed in comments explaining why we DON'T erase it
            assert line.strip().startswith('//'), f"activation_ctr referenced outside comment: {line}"


def test_factory_reset_entry_not_yet_implemented() -> None:
    """Document that factory reset UI/command entry is out of scope."""
    usb_header = (MEMORIA_DIR / "memoria_usb_command.h").read_text(encoding="utf-8")
    assert "kFactoryReset" not in usb_header
    assert "factory" not in usb_header.lower()
