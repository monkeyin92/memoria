"""Host checks for activation retry after a failed first manifest request.

The pure policy is compiled with the host C++ compiler; the same translation
unit is what the firmware registers in main/CMakeLists.txt. No ESP-IDF header
may leak into it, so a stub-heavy include path hides that mistake.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
HOST_DIR = pathlib.Path(__file__).parent / "activation_retry_host"
PATCH_DIR = FIRMWARE_ROOT / "overlay" / "patches"
POLICY_SOURCE = MEMORIA_DIR / "memoria_activation_retry_policy.cc"
POLICY_HEADER = MEMORIA_DIR / "memoria_activation_retry_policy.h"
TEXT_HEADER = MEMORIA_DIR / "memoria_activation_error_text.h"
PAGE_SOURCE = MEMORIA_DIR / "memoria_activation_error_page.cc"
PAGE_HEADER = MEMORIA_DIR / "memoria_activation_error_page.h"
PROTOCOL_SOURCE = MEMORIA_DIR / "memoria_protocol.cc"
PROTOCOL_TEXT = PROTOCOL_SOURCE.read_text(encoding="utf-8")
CLIENT_HEADER = MEMORIA_DIR / "memoria_activation_client.h"
CLIENT_SOURCE = MEMORIA_DIR / "memoria_activation_client.cc"
REGISTER_PATCH = PATCH_DIR / "0043-memoria-register-activation-retry.patch"


@pytest.fixture(scope="module")
def tool():
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        binary = pathlib.Path(tmp) / "activation_retry_tool"
        compiled = subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O0",
                "-Wall",
                "-I",
                str(HOST_DIR),
                "-I",
                str(MEMORIA_DIR),
                str(HOST_DIR / "activation_retry_harness.cc"),
                str(POLICY_SOURCE),
                "-o",
                str(binary),
            ],
            capture_output=True,
            text=True,
        )
        if compiled.returncode != 0:
            pytest.fail(
                "activation retry policy must compile without ESP-IDF:\n"
                f"{compiled.stderr}"
            )
        yield binary


def _run(tool: pathlib.Path, *commands: str) -> list[str]:
    completed = subprocess.run(
        [str(tool)],
        input="\n".join(commands) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.splitlines()


def test_backoff_is_five_then_ten_then_twenty_forever(tool: pathlib.Path) -> None:
    assert _run(tool, "delay 1", "delay 2", "delay 3", "delay 4", "delay 5", "delay 100") == [
        "delay 5000",
        "delay 10000",
        "delay 20000",
        "delay 20000",
        "delay 20000",
        "delay 20000",
    ]


def test_unbound_first_failure_retries_for_any_error(tool: pathlib.Path) -> None:
    lines = _run(
        tool,
        "boot 1 unbound",
        "boot 1 error",
        "boot 1 ok",
        "boot 0 unbound",
        "boot 0 error",
        "boot 0 ok",
    )

    assert lines == [
        "boot true",
        "boot true",
        "boot false",
        # A 409 always keeps the QR, even if the local erase was incomplete.
        "boot true",
        # A bound board keeps today's behaviour for any other failure.
        "boot false",
        "boot false",
    ]


def test_error_page_only_after_the_twenty_second_wait_for_non_409(tool: pathlib.Path) -> None:
    # The first failure is logged by Start(); failures 2, 3 and 4 follow the
    # 5 s, 10 s and 20 s waits, so the page appears about 35 s in.
    assert _run(tool, "page 1 error", "page 2 error", "page 3 error", "page 4 error", "page 99 error") == [
        "page false",
        "page false",
        "page false",
        "page true",
        "page true",
    ]
    assert _run(
        tool,
        *[f"page {failures} unbound" for failures in range(1, 21)],
    ) == ["page false"] * 20


def test_409_after_the_page_restores_the_qr(tool: pathlib.Path) -> None:
    assert _run(tool, "restore unbound", "restore error", "restore ok") == [
        "restore true",
        "restore false",
        "restore false",
    ]


def test_copy_is_the_final_product_wording(tool: pathlib.Path) -> None:
    assert _run(tool, "copy") == [
        "title 网络没连上",
        "body 请检查家里的无线网，网好了会自动连上",
        "detail %s (%d)  HTTP %d  TLS 0x%x",
    ]


def test_policy_has_no_esp_idf_dependency() -> None:
    for path in (POLICY_HEADER, POLICY_SOURCE):
        source = path.read_text(encoding="utf-8")
        assert "esp_" not in source
        assert "freertos" not in source


def test_warn_carries_esp_err_http_transport_and_internal_heap() -> None:
    for pattern in (
        r'ESP_LOGW\(kTag,\s*"Activation failed while unbound,.*?\);',
        r'ESP_LOGW\(kTag, "Activation retry pending,.*?\);',
    ):
        match = re.search(pattern, PROTOCOL_TEXT, re.S)
        assert match is not None, f"missing WARN matching {pattern}"
        warn = match.group(0)
        assert "esp_err_to_name(" in warn
        assert "heap_caps_get_free_size(MALLOC_CAP_INTERNAL)" in warn
        assert "heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)" in warn
        assert "last_http_status()" in warn
        assert "last_transport_error()" in warn
        lowered = warn.lower()
        for forbidden in ("ssid", "password", "signature", "token", "nonce", "url"):
            assert forbidden not in lowered


def test_activation_client_exposes_the_last_transport_failure() -> None:
    header = CLIENT_HEADER.read_text(encoding="utf-8")
    source = CLIENT_SOURCE.read_text(encoding="utf-8")
    assert "int last_http_status() const" in header
    assert "int last_transport_error() const" in header
    assert "GetLastError()" in source
    assert "&last_transport_error_" in source
    assert "last_http_status_ = manifest_status;" in source
    assert "last_http_status_ = ack_status;" in source


def test_unbound_start_keeps_the_bootstrap_qr_and_does_not_set_error() -> None:
    start = PROTOCOL_TEXT[
        PROTOCOL_TEXT.index("bool MemoriaProtocol::Start()") :
        PROTOCOL_TEXT.index("void MemoriaProtocol::StartActivationRetry()")
    ]
    assert "ActivationRetryPolicy::ShouldRetryOnBoot(ActivationAwaitingBinding(), outcome)" in start
    assert "MemoriaBootstrap::GetInstance().Start(display)" in start
    assert "StartActivationRetry();" in start
    retry_branch = start[start.index("ShouldRetryOnBoot") : start.index("ESP_LOGE(kTag, \"Memoria activation failed")]
    assert "SetError(" not in retry_branch


def test_error_page_has_no_button_hints_or_voice_path() -> None:
    sources = {
        "protocol": PROTOCOL_TEXT,
        "policy header": POLICY_HEADER.read_text(encoding="utf-8"),
        "policy source": POLICY_SOURCE.read_text(encoding="utf-8"),
        "copy header": TEXT_HEADER.read_text(encoding="utf-8"),
        "page header": PAGE_HEADER.read_text(encoding="utf-8"),
        "page source": PAGE_SOURCE.read_text(encoding="utf-8"),
        "register patch": REGISTER_PATCH.read_text(encoding="utf-8"),
    }
    for name, source in sources.items():
        assert "长按" not in source, f"{name} must not hint at a long press"
        assert "重新配网" not in source, f"{name} must not offer re-provisioning"
    page = sources["page source"]
    for forbidden in ("tts", "audio", "PlaySound", "SetEmotion", "ShowNotification", "sound", "badge", "aigc"):
        assert forbidden.lower() not in page.lower()


def test_round_page_fits_the_circle_with_the_existing_fonts() -> None:
    page = PAGE_SOURCE.read_text(encoding="utf-8")
    assert "LV_HOR_RES * kLabelWidthPercent / 100" in page
    assert "kLabelWidthPercent = 68" in page
    assert "LV_LABEL_LONG_WRAP" in page
    assert "LV_ALIGN_BOTTOM_MID" in page
    assert "LV_OPA_COVER" in page
    assert page.count("LV_FONT_DECLARE") == 1
    assert "font_noto_sans_basic_14_1" in page


def test_retry_restores_the_qr_before_hiding_the_page() -> None:
    retry = PROTOCOL_TEXT[
        PROTOCOL_TEXT.index("void MemoriaProtocol::RunActivationRetry()") :
        PROTOCOL_TEXT.index("void MemoriaProtocol::StartDisplayProfilePoll()")
    ]
    assert "ActivationRetryPolicy::ShouldRestoreQr(outcome)" in retry
    start_qr = retry.index("MemoriaBootstrap::GetInstance().Start(display)")
    hide_page = retry.index("HideActivationNetworkErrorPage(display)")
    assert start_qr < hide_page, "the QR must exist before the error page is removed"
    assert "ActivationRetryPolicy::ShouldShowErrorPage(failures, outcome)" in retry
    assert retry.index("display->ClearQrCode()") < retry.index(
        "ShowActivationNetworkErrorPage("
    )


def test_new_sources_are_registered_by_the_next_overlay_patch() -> None:
    patch = REGISTER_PATCH.read_text(encoding="utf-8")
    assert '"memoria/memoria_activation_retry_policy.cc"' in patch
    assert '"memoria/memoria_activation_error_page.cc"' in patch
    assert "main/CMakeLists.txt" in patch
