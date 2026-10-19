"""Host checks for local data erase on device unbinding (issue #281).

memoria_local_data_erase.cc is compiled with the host C++ compiler against the
fake NVS in tests/erase_host. The fake declares only nvs_open, nvs_erase_key,
nvs_commit and nvs_close, so any other erase API makes the module stop compiling.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
PATCH_DIR = FIRMWARE_ROOT / "overlay" / "patches"
HOST_DIR = pathlib.Path(__file__).parent / "erase_host"
ERASE_SOURCE = MEMORIA_DIR / "memoria_local_data_erase.cc"

ERASED_KEYS = [
    ("memoria_ui", "wake_mode"),
    ("memoria_ui", "companion"),
    ("memoria_power", "quiet_start"),
    ("memoria_power", "quiet_end"),
    ("memoria_power", "wifi_hold_ms"),
    ("memoria", "wake_word_id"),
    ("memoria", "wake_word_cmd"),
    ("memoria", "wake_word_disp"),
]
KEPT_KEYS = [
    ("memoria_ui", "brightness"),
    ("memoria_power", "deep_sleep_s"),
    ("memoria", "device_name"),
    ("memoria_runtime", "activation_ctr"),
    ("memoria_identity", "device_id"),
]
SEED = [f"put {ns} {key} 1" for ns, key in ERASED_KEYS + KEPT_KEYS]


@dataclass
class Step:
    trace: list[str]
    store: dict[tuple[str, str], int]
    ok: bool

    def opened(self) -> list[str]:
        return [line.split()[1] for line in self.trace if line.startswith("open ")]

    def closed(self) -> list[str]:
        return [line.split()[1] for line in self.trace if line.startswith("close ")]

    def erase_attempts(self) -> list[tuple[str, str]]:
        return [
            (parts[1], parts[2])
            for parts in (line.split() for line in self.trace)
            if parts[0] == "erase_key"
        ]


@pytest.fixture(scope="module")
def tool():
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        binary = pathlib.Path(tmp) / "erase_tool"
        compiled = subprocess.run(
            [
                compiler, "-std=c++17", "-O0", "-Wall",
                "-I", str(HOST_DIR), "-I", str(MEMORIA_DIR),
                str(HOST_DIR / "erase_harness.cc"), str(ERASE_SOURCE),
                "-o", str(binary),
            ],
            capture_output=True,
            text=True,
        )
        if compiled.returncode != 0:
            pytest.fail(f"erase module no longer compiles against the NVS fake:\n{compiled.stderr}")
        yield binary


def _drive(tool: pathlib.Path, *commands: str) -> list[Step]:
    completed = subprocess.run(
        [str(tool)], input="\n".join(commands) + "\n", text=True, capture_output=True, check=True
    )
    steps: list[Step] = []
    trace: list[str] = []
    store: dict[tuple[str, str], int] = {}
    for line in completed.stdout.splitlines():
        kind, _, rest = line.partition(" ")
        if kind == "t":
            trace.append(rest)
        elif kind == "s":
            ns, key, value = rest.split(" ")
            store[(ns, key)] = int(value)
        elif kind == "r":
            steps.append(Step(trace, store, rest == "true"))
            trace, store = [], {}
    return steps


def _balanced(step: Step) -> None:
    assert "bad_close" not in step.trace
    assert sorted(step.opened()) == sorted(step.closed())


def test_erase_removes_exactly_the_child_keys_and_the_wifi_list(tool: pathlib.Path) -> None:
    (step,) = _drive(tool, *SEED, "erase")

    assert step.ok
    assert sorted(step.erase_attempts()) == sorted(ERASED_KEYS)
    assert [ns for ns, _ in step.erase_attempts()].count("memoria_ui") == 2
    assert [ns for ns, _ in step.erase_attempts()].count("memoria_power") == 3
    assert [ns for ns, _ in step.erase_attempts()].count("memoria") == 3
    assert step.trace.count("wifi_clear") == 1
    assert set(step.store) == set(KEPT_KEYS)
    _balanced(step)


def test_erase_only_opens_the_three_child_namespaces(tool: pathlib.Path) -> None:
    (step,) = _drive(tool, *SEED, "erase")

    assert set(step.opened()) == {"memoria_ui", "memoria_power", "memoria"}
    assert "memoria_identity" not in step.opened()
    assert ("memoria_identity", "device_id") in step.store


def test_erase_tolerates_keys_that_are_already_gone(tool: pathlib.Path) -> None:
    (step,) = _drive(tool, "erase")

    assert step.ok
    assert sorted(step.erase_attempts()) == sorted(ERASED_KEYS)
    _balanced(step)


def test_missing_namespace_is_skipped_without_closing_a_handle_it_never_opened(
    tool: pathlib.Path,
) -> None:
    (step,) = _drive(tool, *SEED, "missing memoria_power", "erase")

    assert step.ok
    assert "open_missing memoria_power" in step.trace
    assert ("memoria_ui", "wake_mode") not in step.store
    assert ("memoria_power", "quiet_start") in step.store
    _balanced(step)


def test_failed_key_erase_does_not_stop_the_rest_and_reports_failure(tool: pathlib.Path) -> None:
    (step,) = _drive(tool, *SEED, "fail_erase memoria_power quiet_end", "erase")

    assert not step.ok
    assert ("memoria_power", "quiet_end") in step.store
    assert set(ERASED_KEYS) - set(step.store) == set(ERASED_KEYS) - {("memoria_power", "quiet_end")}
    _balanced(step)


def test_failed_commit_reports_failure_and_still_closes_the_handle(tool: pathlib.Path) -> None:
    (step,) = _drive(tool, *SEED, "fail_commit memoria_ui", "erase")

    assert not step.ok
    assert ("memoria_ui", "wake_mode") in step.store
    assert ("memoria", "wake_word_id") not in step.store
    _balanced(step)


def test_unopenable_namespace_reports_failure_and_erases_the_others(tool: pathlib.Path) -> None:
    (step,) = _drive(tool, *SEED, "broken memoria", "erase")

    assert not step.ok
    assert ("memoria", "wake_word_id") in step.store
    assert ("memoria_ui", "wake_mode") not in step.store
    _balanced(step)


def test_every_memoria_source_is_registered_in_the_firmware_build() -> None:
    registered: set[str] = set()
    for patch in PATCH_DIR.glob("*.patch"):
        registered.update(
            re.findall(r'^\+\s+"(memoria/\w+\.cc)"', patch.read_text(encoding="utf-8"), re.MULTILINE)
        )
    sources = {f"memoria/{path.name}" for path in MEMORIA_DIR.glob("*.cc")}

    assert sources - registered == set()


def test_factory_reset_entry_not_yet_implemented() -> None:
    """Document that factory reset UI/command entry is out of scope."""
    usb_header = (MEMORIA_DIR / "memoria_usb_command.h").read_text(encoding="utf-8")
    assert "kFactoryReset" not in usb_header
    assert "factory" not in usb_header.lower()


def test_activation_clear_only_on_success(tool: pathlib.Path) -> None:
    """Lock the 409 handler's activation_v clear is conditional on erase success."""
    activation_client = (
        (MEMORIA_DIR / "memoria_activation_client.cc").read_text(encoding="utf-8")
    )

    handler_409_start = activation_client.find("if (manifest_status == 409)")
    assert handler_409_start > 0
    handler_409_end = activation_client.find("return ESP_ERR_INVALID_STATE", handler_409_start)
    assert handler_409_end > handler_409_start
    handler_body = activation_client[handler_409_start:handler_409_end]

    assert "EraseLocalChildData()" in handler_body
    assert 'runtime.SetInt("activation_v", 0)' in handler_body
    assert 'previous_activation_v = runtime.GetInt("activation_v"' in handler_body

    guard_check = 'if (previous_activation_v != 0)'
    erase_call = "EraseLocalChildData()"
    clear_call = 'runtime.SetInt("activation_v", 0)'

    assert guard_check in handler_body
    guard_idx = handler_body.find(guard_check)
    erase_idx = handler_body.find(erase_call)
    clear_idx = handler_body.find(clear_call)

    assert guard_idx < erase_idx, "Erase must be inside the guard"
    assert erase_idx < clear_idx, "activation_v clear must come after erase"

    if_erase_success = handler_body.find("if (EraseLocalChildData()")
    assert if_erase_success > 0, "Caller must check erase result"
    assert handler_body.find(clear_call, if_erase_success) < handler_body.find("} else", if_erase_success), \
        "activation_v clear must be conditional on erase success"


def test_erase_uses_only_narrow_nvs_apis_and_never_writes_identity() -> None:
    """Lock the erase module's API surface: only nvs_open/erase_key/commit/close.

    Forbidden APIs are absent from tests/erase_host headers: nvs_erase_all,
    esp_partition_erase_range, esp_flash_erase_region, nvs_set_*, nvs_open_from_partition.

    The identity_guard scan already confirms the overlay passes with no allowlist additions,
    so this focuses on the source-level lock that the module never attempts nvs_erase_all or
    ranged flash erase. The identity partition is protected by the stub's namespace tracking.
    """
    impl = ERASE_SOURCE.read_text(encoding="utf-8")

    assert "nvs_erase_all" not in impl
    assert "nvs_flash_erase" not in impl
    assert "esp_partition_erase" not in impl
    assert "esp_flash_erase" not in impl
    assert "nvs_set_" not in impl
    assert "nvs_open_from_partition" not in impl
    assert "memoria_identity" in impl, "Header doc must mention it's preserved"
    assert impl.count("memoria_identity") == 1, "Only in the header doc comment"
